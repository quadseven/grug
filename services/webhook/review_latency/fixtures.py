"""Synthetic long-context review fixtures for latency replay (#648).

These are NOT quality ground truth — they exist to stress prefill + decode
with Elder-shaped prompts (real `_build_messages` path). No secrets, no
real hostnames, no customer code.
"""

from __future__ import annotations

from dataclasses import dataclass

from llm_client import Hunk, _build_messages


@dataclass(frozen=True, slots=True)
class LatencyFixture:
    """One named prompt template expanded into Elder messages."""

    name: str
    # Approximate added-line scale for humans reading the report.
    added_lines: int
    messages: tuple[dict[str, str], ...]
    prompt_chars: int


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
        hunks,
        _PROMPT_VARIANT,
        file_contents=None,
        cross_file_contents=None,
        pr_context={
            "title": f"latency fixture {name}",
            "body": "Synthetic PR for review latency harness #648.",
            "repo": "example/fixture",
            "pr_number": 1,
            "head_sha": "abc1234",
            "base_sha": "def5678",
        },
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
