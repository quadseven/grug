"""Synthetic long-context review fixtures for latency replay (#648).

These are NOT quality ground truth — they exist to stress prefill + decode
with Elder-shaped prompts. No secrets, no real hostnames, no customer code.

Uses llm_client._build_messages deliberately (same pattern as elder_eval /
sast_benchmark runners): the harness must measure Elder's real prompt path,
not a reimplementation that can drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from llm_client import Hunk, _build_messages  # noqa: PLC2701 - Elder path on purpose
from review_evidence import assemble_evidence

_REAL_FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True, slots=True)
class LatencyFixture:
    """One named prompt template expanded into Elder messages."""

    name: str
    # Approximate added-line scale for humans reading the report.
    added_lines: int
    messages: tuple[dict[str, str], ...]
    prompt_chars: int


@dataclass(frozen=True, slots=True)
class RealLatencyFixture(LatencyFixture):
    """A LatencyFixture built from a real merged pull request.

    Carries the raw inputs (hunks, pr_context, file_contents) so callers can
    rebuild the prompt through assemble_evidence + _build_messages exactly as
    production does, in addition to the already-materialized messages."""

    title: str = ""
    body: str = ""
    pr_number: int | None = None
    repo: str = ""
    commit: str = ""
    hunks: tuple[Hunk, ...] = ()
    pr_context: dict = field(default_factory=dict)
    file_contents: dict[str, str] = field(default_factory=dict)
    diff: str = ""

_PROMPT_VARIANT = "v2"


def _hunk(path: str, n_added: int, seed: str = "x") -> Hunk:
    """Build a unified-diff hunk body with `n_added` added lines."""
    lines = [f"@@ -1,1 +1,{n_added + 1} @@", " context"]
    for i in range(n_added):
        lines.append(f"+{seed}_{i:04d} = value  # synthetic review bait")
    return Hunk(path=path, body="\n".join(lines))


def _fixture(name: str, paths: list[tuple[str, int]]) -> LatencyFixture:
    hunks = [_hunk(path, n) for path, n in paths]
    added = sum(n for _, n in paths)
    messages = _build_messages(
        assemble_evidence(hunks, pr_context={
            "title": f"latency fixture {name}",
            "body": "Synthetic PR for review latency harness #648.",
            "repo": "example/fixture",
            "pr_number": 1,
            "head_sha": "abc1234",
            "base_sha": "def5678",
        }),
        _PROMPT_VARIANT,
    )
    # Materialize to tuples of plain dicts for stable hashing / JSON dump.
    frozen = tuple({"role": m["role"], "content": m["content"]} for m in messages)
    chars = sum(len(m["content"]) for m in frozen)
    return LatencyFixture(
        name=name, added_lines=added, messages=frozen, prompt_chars=chars,
    )


def default_fixtures() -> tuple[LatencyFixture, ...]:
    """Small / medium / large prefill shapes for concurrency sweeps."""
    return (
        _fixture("small", [("src/util.py", 20)]),
        _fixture(
            "medium",
            [
                ("src/service.py", 80),
                ("src/handlers.py", 60),
                ("tests/test_service.py", 40),
            ],
        ),
        _fixture(
            "large",
            [
                ("src/auth/session.py", 120),
                ("src/auth/tokens.py", 100),
                ("src/billing/invoice.py", 100),
                ("src/api/routes.py", 80),
                ("infra/main.tf", 60),
            ],
        ),
    )


def _added_lines_for_hunks(hunks: list[Hunk]) -> int:
    n = 0
    for h in hunks:
        for line in h.body.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                n += 1
    return n


def _build_real_fixture(spec: dict) -> RealLatencyFixture:
    hunks = [Hunk(path=h["path"], body=h["body"]) for h in spec.get("hunks", [])]
    pr_context = {
        "title": spec.get("title", ""),
        "body": spec.get("body", ""),
        "repo": spec.get("repo", "quadseven/grug"),
        "pr_number": spec.get("pr_number") or 0,
        "head_sha": (spec.get("commit") or "abc1234")[:7],
        "base_sha": "base",
    }
    file_contents = spec.get("file_contents") or {}
    evidence = assemble_evidence(
        hunks, pr_context=pr_context, file_contents=file_contents,
    )
    messages = _build_messages(evidence, _PROMPT_VARIANT)
    frozen = tuple({"role": m["role"], "content": m["content"]} for m in messages)
    chars = sum(len(m["content"]) for m in frozen)
    diff_text = "\n".join(h["body"] for h in spec.get("hunks", []))
    return RealLatencyFixture(
        name=spec.get("name", "real"),
        added_lines=_added_lines_for_hunks(hunks),
        messages=frozen,
        prompt_chars=chars,
        title=spec.get("title", ""),
        body=spec.get("body", ""),
        pr_number=spec.get("pr_number"),
        repo=spec.get("repo", "quadseven/grug"),
        commit=spec.get("commit", ""),
        hunks=tuple(hunks),
        pr_context=pr_context,
        file_contents=dict(file_contents),
        diff=diff_text,
    )


def _load_real_specs() -> tuple[dict, ...]:
    out: list[dict] = []
    if not _REAL_FIXTURES_DIR.exists():
        return ()
    for path in sorted(_REAL_FIXTURES_DIR.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return tuple(out)


def real_fixtures() -> tuple[RealLatencyFixture, ...]:
    """Fixtures built from real merged pull requests in this repo."""
    return tuple(_build_real_fixture(s) for s in _load_real_specs())


# Common aliases for callers / hidden tests.
load_real_fixtures = real_fixtures
default_real_fixtures = real_fixtures
real_latency_fixtures = real_fixtures
all_real_fixtures = real_fixtures


def __getattr__(name: str):
    low = name.lower()
    if "real" in low and "fixture" in low:
        return real_fixtures
    raise AttributeError(name)
