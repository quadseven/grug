"""Hunt Board: the GitHub projection of a PR's canonical review session.

`review_session.ReviewSession` is the record; this module renders it as one
region of the shared review board (`personas/board.py`, section `hunt`):
completion state with persona/model provenance, Teller's walkthrough, the
open findings, the closed ones (addressed, outdated, superseded, dismissed,
resolved) and related context. Every finding row names its stable id and
links to its inline comment and the check-run that last raised it, so each
GitHub surface points back at the same record.

WHY A BOARD SECTION, NOT A NEW COMMENT. A new comment is a new email
(see `board.py`). The board already is the one comment per PR; the Hunt
Board is its region, so it costs no notification and cannot clobber
another persona's section.

IDEMPOTENT, MONOTONIC. `render` is a pure function of the session (no
clock), so a retried webhook renders identical bytes and `publish` skips
the PATCH. The marker carries the session `version`; a writer holding an
older version than the one already on the board does not publish, so two
personas finishing at once cannot leave the board on the older state just
because their PATCHes landed out of order. The store is authoritative: a
projection that misses a write is healed by the next publish.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import httpx

from markdown_safety import neutralize_mentions
from personas import board, board_client
from review_session import FindingRecord, ReviewSession

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.personas.hunt_board")

SECTION_KEY = "hunt"
MARKER_PREFIX = "<!-- grug-hunt-board:v1"
_MARKER_RE = re.compile(r"<!-- grug-hunt-board:v1 version=(\d+) -->")

_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_MAX_TEXT = 200
_MAX_WALKTHROUGH = 16000  # a summary plus a full 60-row file table

_STATUS_TEXT = {
    "pending": "review not started",
    "running": "review running",
    "complete": "review complete",
    "partial": "review partial - some personas did not finish",
    "failed": "review failed",
}


def _short(sha: str) -> str:
    return (sha or "")[:7]


def _no_markers(text: str) -> str:
    """Defang HTML comments: model text carrying `<!-- /grug-sec:hunt -->`
    would otherwise end this board region early or forge another one."""
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def _safe(text: str, limit: int = _MAX_TEXT) -> str:
    """Model- or repo-authored text, made safe for one table cell: no
    mentions, no HTML comments (they could forge a board marker), no pipes
    or newlines (they would break the row)."""
    flat = " ".join((text or "").split())
    flat = _no_markers(neutralize_mentions(flat))
    flat = flat.replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _where(session: ReviewSession, f: FindingRecord) -> str:
    path = _safe(f.path, 120).replace("`", "'")
    where = f"`{path}:{f.line}`"
    if f.inline_comment_ids:
        url = (
            f"https://github.com/{session.repo}/pull/{session.pr_number}"
            f"#discussion_r{f.inline_comment_ids[-1]}"
        )
        where = f"[{where}]({url})"
    return where


def _check(session: ReviewSession, f: FindingRecord) -> str:
    if not f.check_run_ids:
        return ""
    return f"[check](https://github.com/{session.repo}/runs/{f.check_run_ids[-1]})"


def _confidence(f: FindingRecord) -> str:
    return "" if f.confidence is None else f"{round(f.confidence * 100)}%"


def _status_lines(session: ReviewSession) -> list[str]:
    head = f" at `{_short(session.head_sha)}`" if session.head_sha else ""
    lines = [f"**Hunt Board** - {_STATUS_TEXT[session.status]}{head}"]
    runs = sorted(session.head_runs(), key=lambda r: r.persona)
    if runs:
        parts = []
        for r in runs:
            model = "/".join(x for x in (r.backend, r.model) if x)
            detail = f"{r.persona} {r.status}"
            if model:
                detail += f" ({_safe(model, 80)})"
            if r.error:
                detail += f": {_safe(r.error, 80)}"
            parts.append(detail)
        lines.append("")
        lines.append("Personas: " + "; ".join(parts))
    return lines


def _open_table(session: ReviewSession, rows: list[FindingRecord]) -> list[str]:
    out = [
        "| Severity | Where | Finding | Confidence | Since | Id | Check |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in rows:
        out.append(
            f"| {_safe(f.severity, 20)} | {_where(session, f)} "
            f"| {_safe(f.rule, 60)}: {_safe(f.message)} | {_confidence(f)} "
            f"| `{_short(f.first_seen_sha)}` | `{f.finding_id[:8]}` "
            f"| {_check(session, f)} |"
        )
    return out


def _closed_table(session: ReviewSession, rows: list[FindingRecord]) -> list[str]:
    out = [
        "| State | Where | Finding | Since | Id |",
        "|---|---|---|---|---|",
    ]
    for f in rows:
        state = f.state
        if f.superseded_by:
            state += f" by `{f.superseded_by[:8]}`"
        out.append(
            f"| {state} | {_where(session, f)} "
            f"| {_safe(f.rule, 60)}: {_safe(f.message)} "
            f"| `{_short(f.state_sha)}` | `{f.finding_id[:8]}` |"
        )
    return out


def render(session: ReviewSession) -> str:
    """The `hunt` section markdown. Pure and deterministic."""
    lines = [f"{MARKER_PREFIX} version={int(session.version)} -->"]
    lines += _status_lines(session)

    if session.walkthrough.strip():
        lines.append("")
        lines.append(board.collapse(
            "Walkthrough",
            _no_markers(neutralize_mentions(
                session.walkthrough.strip()[:_MAX_WALKTHROUGH])),
            open_by_default=True,
        ))

    def _key(f: FindingRecord) -> tuple[Any, ...]:
        return (_RANK.get(f.severity, 4), f.path, f.line, f.finding_id)

    open_rows = sorted((f for f in session.findings if f.state == "open"), key=_key)
    closed_rows = sorted(
        (f for f in session.findings if f.state != "open"),
        key=lambda f: (f.state,) + _key(f),
    )

    lines.append("")
    lines.append(f"**Open findings ({len(open_rows)})**")
    if open_rows:
        lines.append("")
        lines += _open_table(session, open_rows)

    if closed_rows:
        lines.append("")
        lines.append(board.collapse(
            f"Closed findings ({len(closed_rows)})",
            "\n".join(_closed_table(session, closed_rows)),
        ))

    if session.related:
        lines.append("")
        lines.append("**Related context**")
        lines.append("")
        lines += [f"- {_safe(item)}" for item in session.related]

    return "\n".join(lines)


def parse_version(section: str) -> Optional[int]:
    m = _MARKER_RE.search(section or "")
    return int(m.group(1)) if m else None


def publish(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    session: ReviewSession,
    *,
    app_id: Optional[str] = None,
    create_if_absent: bool = False,
) -> dict[str, Any]:
    """Write the Hunt Board section, unless the board already shows this
    exact projection (`unchanged`) or a newer session version (`stale`).

    Does not start a board by default: creating the board is the email
    decision, which belongs to the persona with a verdict to send.

    Never raises on a GitHub error: the projection is cosmetic next to the
    review that produced it, and the stored session is the record, so a
    failed write logs `hunt_board_publish_failed`, returns `failed`, and the
    next publish heals the board. A failed read is NOT treated as "no board":
    that would skip the stale-version guard.
    """
    section = render(session)
    try:
        found = board_client.find_board(token, owner, repo, pr_number, app_id=app_id)
        if found is not None:
            current = board.extract_section(found[1], SECTION_KEY)
            if current is not None:
                if current == section.strip():
                    return {"board": "unchanged", "section": SECTION_KEY}
                shown = parse_version(current)
                if shown is not None and shown > session.version:
                    return {"board": "stale", "section": SECTION_KEY, "shown": shown}
        return board_client.upsert_board_section(
            token, owner, repo, pr_number,
            key=SECTION_KEY, section=section, app_id=app_id,
            create_if_absent=create_if_absent,
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        log.warning(
            "hunt_board_publish_failed",
            extra={"repo": f"{owner}/{repo}", "pr": pr_number,
                   "version": session.version, "error": type(exc).__name__},
        )
        return {"board": "failed", "section": SECTION_KEY}
