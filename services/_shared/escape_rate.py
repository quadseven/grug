"""Weekly escape rate per 100 merged pull requests (#1568 slice 5, box 4).

Pure arithmetic over records the caller supplies. An escape is counted
against the author kind of the PR that introduced the defect (the escaped
PR a trace resolved), not the author of the fix. Fetching merged pulls and
traced escapes is the sweep's job; nothing here does I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

AGENT = "agent"
HUMAN = "human"
AUTHOR_KINDS = (AGENT, HUMAN)


@dataclass(frozen=True)
class MergedPull:
    number: int
    author_kind: str


@dataclass(frozen=True)
class EscapeRate:
    author_kind: str
    merged: int
    escapes: int
    # None when nothing merged, so "no data" is not shown as zero escapes.
    per_100: float | None


def escape_rates(
    merged: Iterable[MergedPull], escaped_prs: Iterable[int]
) -> dict[str, EscapeRate]:
    """Escapes per 100 merged PRs, split agent-authored vs human-authored.

    Each escaped PR number counts once, however many fixes correct it, and
    only when it is in `merged`. An unknown author kind is rejected so a
    typo cannot silently drop a PR from both rows.
    """
    by_number: dict[int, str] = {}
    for pull in merged:
        if pull.author_kind not in AUTHOR_KINDS:
            raise ValueError(f"unknown author kind: {pull.author_kind!r}")
        by_number[pull.number] = pull.author_kind
    totals = {kind: 0 for kind in AUTHOR_KINDS}
    for kind in by_number.values():
        totals[kind] += 1
    escapes = {kind: 0 for kind in AUTHOR_KINDS}
    for number in set(escaped_prs):
        kind = by_number.get(number)
        if kind is not None:
            escapes[kind] += 1
    return {
        kind: EscapeRate(
            kind,
            totals[kind],
            escapes[kind],
            round(100 * escapes[kind] / totals[kind], 2) if totals[kind] else None,
        )
        for kind in AUTHOR_KINDS
    }
