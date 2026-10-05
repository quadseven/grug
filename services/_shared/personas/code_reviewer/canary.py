"""Elder review-quality canary.

A scheduled end-to-end check that Elder's LIVE model chain still catches a
planted bug and still passes a clean diff. Two fixed fixtures run through the
real `review_diff` (the same chain production reviews use). Each case emits
one dense `grug.elder.canary` gauge per run, so a monitor sees a steady
series instead of an absence.

Never touches GitHub: the review context is a fixed fake repo with no
installation id and no head sha, so no token is fetched and nothing is posted.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

from llm_client import Hunk, LlmReviewResponse, review_diff
from observability import emit_gauge

log = logging.getLogger("grug.elder_canary")

CANARY_REPO = "grug-canary/elder-canary"
CANARY_PATH = "canary/user_lookup.py"
METRIC = "grug.elder.canary"

Verdict = Literal["caught", "missed", "pass", "false_positive", "error"]

_HEADER = '''"""User lookup helpers for the account service."""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

PAGE_SIZE = 50


@dataclass(frozen=True)
class User:
    id: int
    name: str
    email: str


def _row_to_user(row: tuple) -> User:
    return User(id=int(row[0]), name=str(row[1]), email=str(row[2]))


def normalize_name(name: str) -> str:
    return " ".join(name.split()).strip()


def clamp_page(page: int) -> int:
    return max(0, page)


def page_offset(page: int) -> int:
    return clamp_page(page) * PAGE_SIZE


def find_user_by_name(db, name: str) -> User | None:
    name = normalize_name(name)
'''

_QUERY_PLANTED = '''    rows = db.execute("SELECT id, name, email FROM users WHERE name = '" + name + "'").fetchall()'''
_QUERY_CLEAN = '''    rows = db.execute("SELECT id, name, email FROM users WHERE name = %s", (name,)).fetchall()'''

_FOOTER = '''    if not rows:
        log.info("user_not_found")
        return None
    return _row_to_user(rows[0])


def list_users(db, page: int = 0) -> list[User]:
    rows = db.execute(
        "SELECT id, name, email FROM users ORDER BY id LIMIT %s OFFSET %s",
        (PAGE_SIZE, page_offset(page)),
    ).fetchall()
    return [_row_to_user(r) for r in rows]


def count_users(db) -> int:
    row = db.execute("SELECT count(*) FROM users").fetchone()
    return int(row[0]) if row else 0


def email_domain(user: User) -> str:
    return user.email.rpartition("@")[2].lower()
'''


def _build(query: str) -> tuple[list[Hunk], int]:
    """A new-file hunk (`@@ -0,0 +1,N @@`, every line added) plus the 1-based
    file line of the query, which is the line a finding must land on."""
    lines = (_HEADER + query + "\n" + _FOOTER).splitlines()
    body = "\n".join([f"@@ -0,0 +1,{len(lines)} @@"] + [f"+{ln}" for ln in lines])
    return [Hunk(path=CANARY_PATH, body=body)], len(_HEADER.splitlines()) + 1


PLANTED_HUNKS, PLANTED_LINE = _build(_QUERY_PLANTED)
CLEAN_HUNKS, _ = _build(_QUERY_CLEAN)

_BLOCKING = frozenset({"high", "critical"})
_LINE_SLOP = 2


def _is_partial(resp: LlmReviewResponse) -> bool:
    return resp.kind != "reviewed" or bool(resp.error)


def planted_verdict(resp: LlmReviewResponse) -> Verdict:
    if resp.kind == "reviewed":
        for f in resp.findings:
            text = f"{f.rule} {f.message}".lower()
            if (
                f.severity in _BLOCKING
                and abs(f.line - PLANTED_LINE) <= _LINE_SLOP
                and ("sql" in text or "inject" in text)
            ):
                return "caught"
    return "error" if _is_partial(resp) else "missed"


def clean_verdict(resp: LlmReviewResponse) -> Verdict:
    if resp.kind == "reviewed" and any(f.severity in _BLOCKING for f in resp.findings):
        return "false_positive"
    return "error" if _is_partial(resp) else "pass"


@dataclass(frozen=True)
class CanaryResult:
    case: str
    outcome: Verdict
    backend: str | None
    model: str | None
    findings_count: int
    elapsed_s: float


_CASES: tuple[tuple[str, list[Hunk], Callable[[LlmReviewResponse], Verdict]], ...] = (
    ("planted", PLANTED_HUNKS, planted_verdict),
    ("clean", CLEAN_HUNKS, clean_verdict),
)


def _answerer(resp: LlmReviewResponse | None) -> tuple[str | None, str | None]:
    if resp is None:
        return None, None
    backend = str(getattr(resp.backend_used, "value", resp.backend_used) or "") or None
    return backend, resp.model_name or None


def _emit(result: CanaryResult) -> None:
    tags = {"case": result.case, "outcome": result.outcome}
    if result.backend:
        tags["backend"] = result.backend
    if result.model:
        tags["model"] = result.model
    ok = result.outcome in ("caught", "pass")
    emit_gauge(METRIC, 1.0 if ok else 0.0, tags)
    log.info(
        "elder_canary_result",
        extra={
            "case": result.case, "outcome": result.outcome,
            "backend": result.backend or "unknown", "model": result.model or "unknown",
            "findings_count": result.findings_count, "elapsed_s": result.elapsed_s,
        },
    )


def run_canary(timeout_s: float = 240.0) -> list[CanaryResult]:
    """Run both cases concurrently under one shared deadline and emit one
    gauge per case. A case that raises or outlives the deadline reports
    `error` (and its review is cancelled); the other case is unaffected."""
    cancel = threading.Event()
    slots: dict[str, dict] = {name: {} for name, _, _ in _CASES}
    started = time.monotonic()

    def work(name: str, hunks: list[Hunk]) -> None:
        slot = slots[name]
        try:
            slot["resp"] = review_diff(
                hunks, 0,
                pr_context={"repo": CANARY_REPO, "pr_number": 0},
                cancel_event=cancel,
            )
        except Exception as e:  # noqa: BLE001 - reported as an `error` outcome
            slot["kind"] = type(e).__name__
        slot["elapsed"] = time.monotonic() - started

    threads = [
        threading.Thread(target=work, args=(n, h), daemon=True, name=f"elder-canary-{n}")
        for n, h, _ in _CASES
    ]
    for t in threads:
        t.start()
    deadline = started + timeout_s
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    if any(t.is_alive() for t in threads):
        cancel.set()

    results: list[CanaryResult] = []
    for (name, _, judge), t in zip(_CASES, threads):
        slot = slots[name]
        resp: LlmReviewResponse | None = None if t.is_alive() else slot.get("resp")
        outcome: Verdict = judge(resp) if resp is not None else "error"
        backend, model = _answerer(resp)
        results.append(
            CanaryResult(
                case=name, outcome=outcome, backend=backend, model=model,
                findings_count=len(resp.findings) if resp is not None else 0,
                elapsed_s=round(slot.get("elapsed", time.monotonic() - started), 2),
            )
        )
    for r in results:
        _emit(r)
    return results
