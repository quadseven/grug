"""Eval fixture for the integer-column/datetime type mismatch (#1493).

A production repository's review compared a column that stores a Unix
integer with datetime values, and added a timedelta to it, while the same
file read that column with `int()` in two other places. Elder published no
blocking finding; an independent verifier found it by reading the existing
readers of the same column.

This module anchors the miss on a synthetic reviewed head shaped like the
live one. The fixture data lives under `fixtures/column_datetime/`: the
diff hunks and the head snapshot production would have sent when the file
fits the full-file budget. `review_evidence` now renders every read of
a touched column regardless of file size, so the mismatch is visible even
when the file is too large for the full-file block.

The pure core (fixture, evidence, scoring) runs in CI. `replay` makes a
backend call and is only used by the on-demand eval:

    python -m elder_eval.column_type_mismatch     # print the replay result

`make elder-eval-column` runs the same thing with the webhook dependencies
installed, the sibling of `make elder-eval`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from llm_client import Hunk, _build_messages, _parse_review
from review_evidence import ElderEvidence, assemble_evidence

from sast_benchmark.backends import BenchBackend, configured_backends
from sast_benchmark.runner import _post

from .missed_classes import (
    STRUCTURAL,
    MissedClassFixture,
    caught,
    recall,
    score,
    split_hunks,
)

log = logging.getLogger("grug.elder_eval.column_type_mismatch")

REPO = "example/bridge"
PR = 1
REVIEWED_HEAD = "0badc0d"
REFERENCE_SOURCE = "independent-verifier"

PATH = "bridge/posts.py"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "column_datetime"
BASELINE_PATH = Path(__file__).parent / "missed_classes_baseline.json"
MEASUREMENT = "production_column_datetime_type_mismatch"
_PROMPT_VARIANT = "v2"

FIXTURE = MissedClassFixture(
    name="column_datetime_type_mismatch",
    finding_class="type-mismatch",
    path=PATH,
    # The new cutoff and the comparison that uses it.
    defect_lines=((19, 20),),
    signal_terms=("created_at", "datetime", "integer", "int("),
    reference_finding=(
        "Compare `created_at` as an integer, not a datetime. The column is a "
        "Unix integer: `age_seconds()` and `created_at()` both read it with "
        '`int(row["created_at"])`, but the new `list_recent()` compares it '
        "with `since - timedelta(days=1)`, a datetime. In production the "
        "first row raises TypeError. Convert the cutoff to an integer epoch "
        "or convert the column to a datetime before comparing."
    ),
    counterexample=(
        "A row with created_at=1700000000 and "
        "since=datetime(2026, 10, 28, tzinfo=timezone.utc): cutoff is a "
        "datetime, so `1700000000 > cutoff` raises TypeError."
    ),
    reachability=STRUCTURAL,
)


class _FindingLike(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def line(self) -> int: ...
    @property
    def message(self) -> str: ...


def hunks() -> list[Hunk]:
    """The reviewed-head hunks for the one changed file."""
    return split_hunks(PATH, (FIXTURE_DIR / "posts.diff").read_text())


def head_content() -> str:
    """The file's content at the reviewed head."""
    return (FIXTURE_DIR / "posts.py.txt").read_text()


def evidence() -> ElderEvidence:
    """The evidence production assembles for the changed file."""
    return assemble_evidence(
        hunks(),
        file_contents={PATH: head_content()},
    )


def score_findings(findings: Iterable[_FindingLike]) -> dict[str, bool]:
    """{fixture name: caught} for one review's findings."""
    return score([FIXTURE], findings)


def caught_finding(findings: Iterable[_FindingLike]) -> bool:
    """True when a finding names the type mismatch on the defect lines."""
    return caught(FIXTURE, findings)


def recall_for(findings: Iterable[_FindingLike]) -> float:
    """Line-anchored recall for this one fixture."""
    return recall(score_findings(findings))


@dataclass(frozen=True)
class Replay:
    """One live replay; `errored` means the empty findings are not an answer."""

    findings: tuple[_FindingLike, ...]
    errored: bool


def replay(
    backend: BenchBackend,
    post: Callable[[BenchBackend, list[dict[str, str]]], httpx.Response] = _post,
) -> Replay:
    """Review the fixture evidence once with Elder's exact prompt and parser."""
    messages = _build_messages(evidence(), _PROMPT_VARIANT)
    try:
        resp = post(backend, messages)
    except (httpx.RequestError, httpx.TimeoutException) as e:
        log.warning(
            "column_type_mismatch_replay_failed backend=%s kind=%s",
            backend.name,
            type(e).__name__,
        )
        return Replay(findings=(), errored=True)
    parsed = _parse_review(resp)
    if parsed.error and not parsed.findings:
        log.warning(
            "column_type_mismatch_parse_failed backend=%s err=%s",
            backend.name,
            parsed.error,
        )
        return Replay(findings=(), errored=True)
    return Replay(findings=tuple(parsed.findings), errored=False)


def main(argv: list[str] | None = None) -> int:
    """Replay the fixture once per configured backend and print the verdict."""
    parser = argparse.ArgumentParser(prog="elder_eval.column_type_mismatch")
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    backends = configured_backends()
    if not backends:
        print("No backends configured (see sast_benchmark.backends).", file=sys.stderr)
        return 2
    status = 0
    for backend in backends:
        run = replay(backend)
        if run.errored:
            print(
                f"\n!! backend {backend.name}: replay errored - not a result",
                file=sys.stderr,
            )
            status = 2
            continue
        hit = caught_finding(run.findings)
        print(
            f"\n=== backend: {backend.name} === "
            f"{FIXTURE.name}: {'CAUGHT' if hit else 'missed'}"
        )
        for finding in run.findings:
            print(f"    finding {finding.path}:{finding.line} {finding.message[:120]}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
