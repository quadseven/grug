"""Merged sibling PRs as evidence for a ticket's open criteria (grug#1530).

A ticket worked across several PRs is closed by the last one, and that PR's
diff cannot show what the earlier ones delivered. Chief and Elder read the
merged PRs that link the ticket and treat a criterion one of them delivered
as met. With no merged sibling nothing changes, so a first or only PR is held
to the same bar as before.

The evidence is one PR's own report as merged, never the sum of several: a
body edited after its merge counts for nothing (`issue_fetcher`), since a
merged PR stays editable and this decides a gate. Left out of
that report, because they do not say a thing was delivered: out-of-scope and
follow-up sections, unticked boxes, quotes, lines marked as not done, and lines
copied from the ticket (a lane PR restates the ticket's text, delivered or
not). A criterion needs `MIN_WORDS` distinctive words, and `MIN_SHARE` of them
in that report.

Some criteria no report can show, so they stay open until a person ticks them:
one about the whole ticket ("all remaining routes"), a negative ("no handler
reads ..."), and a `Live:` one, which a running system proves.

Deterministic and pure: no model call, no IO. `issue_fetcher` does the read.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from personas.tpm.ticket_compliance import _tokens

MIN_WORDS = 3
MIN_SHARE = 0.6


@dataclass(frozen=True, slots=True)
class SiblingPr:
    """A merged pull request that links the ticket, with its body as merged."""

    number: int
    body: str


SiblingFetcher = Callable[[int], tuple[SiblingPr, ...]]

_UNPROVABLE_RE = re.compile(
    r"^\W*live\b"
    r"|\b(?:all|every|each|any|remaining|rest|no|not|never|none|nothing|without"
    r"|removed?|deleted?|gone)\b",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+|\*\*[^*]+\*\*\s*:?\s*$)")
_NOT_DELIVERED_HEADING_RE = re.compile(
    r"out of scope|non.?goals?|follow.?ups?|not (?:done|included|built)|deferred|remaining",
    re.IGNORECASE,
)
_NOT_DELIVERED_LINE_RE = re.compile(
    r"\bnot built\b|\bdeferred\b|\bleft for\b|\bfollow.?up\b|\bout of scope\b",
    re.IGNORECASE,
)
_OPEN_BOX_RE = re.compile(r"^\s*[-*]\s*\[ \]\s")
_QUOTE_RE = re.compile(r"^\s*>")
# Words any report or criterion may use, whatever it delivered.
_GENERIC = frozenset(
    "verify confirm check ensure count document currently implement first new "
    "existing test tests fix line lines suite pass passes fail fails about time "
    "times".split()
)


def _stem(word: str) -> str:
    for suffix in ("ings", "ing", "ies", "ied", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


_GENERIC_STEMS = frozenset(_stem(w) for w in _GENERIC)


def _words(text: str) -> set[str]:
    return {_stem(t) for t in _tokens(text)} - _GENERIC_STEMS


def _normal(line: str) -> str:
    return " ".join(line.lower().split())


def _report(pr: SiblingPr, ticket_lines: frozenset[str]) -> str:
    """The parts of the PR that report what it delivered."""
    kept: list[str] = []
    skipping = False
    for line in (pr.body or "").splitlines():
        if _HEADING_RE.match(line):
            skipping = bool(_NOT_DELIVERED_HEADING_RE.search(line))
            continue
        if (
            skipping
            or _OPEN_BOX_RE.match(line)
            or _QUOTE_RE.match(line)
            or _NOT_DELIVERED_LINE_RE.search(line)
            or _normal(line) in ticket_lines
        ):
            continue
        kept.append(line)
    return "\n".join(kept)


def delivered_criteria(
    criteria: Iterable[str], siblings: Iterable[SiblingPr], ticket_body: str = "",
) -> dict[str, int]:
    """`{criterion: number of the merged PR that delivered it}`."""
    ticket_lines = frozenset(_normal(line) for line in (ticket_body or "").splitlines())
    reports = [(pr.number, _words(_report(pr, ticket_lines))) for pr in siblings]
    met: dict[str, int] = {}
    for criterion in criteria:
        wanted = _words(criterion)
        if len(wanted) < MIN_WORDS or _UNPROVABLE_RE.search(criterion):
            continue
        best_share, best_pr = 0.0, None
        for number, words in reports:
            hits = len(wanted & words)
            share = hits / len(wanted)
            if hits >= MIN_WORDS and share >= MIN_SHARE and share > best_share:
                best_share, best_pr = share, number
        if best_pr is not None:
            met[criterion] = best_pr
    return met
