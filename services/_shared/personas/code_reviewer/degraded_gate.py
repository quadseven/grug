"""What Elder's check says when its review is degraded (grug#1212, ADR-0023).

A repo whose Elder is blocking gets `Grug - Elder` as a required status
check, and GitHub counts a required check that concludes `neutral` as
passing. So a degraded review (every model call failed, a parse failure, a
partial review, files not walked, a low-confidence answer) that concluded
`neutral` let a PR merge with no real review behind the green.

The decision: in BLOCK mode a degraded review concludes `failure`; in WARN
mode it stays `neutral`, as before. Every exit that completes the Elder
check for a degraded review - the dispatch publish, the fetch/parse
fallback, the async deep append, and the durable lane's fail-open terminal
checks - asks this module for its conclusion and its first summary line, so
the policy lives in one place.

`no_diff` is not a degraded review (there was nothing to look at) and a
superseded head is not the head that merges; neither comes through here.
"""
from __future__ import annotations

from typing import Literal

DegradedConclusion = Literal["failure", "neutral"]


def degraded_conclusion(blocking: bool | None) -> DegradedConclusion:
    """`failure` when the repo's Elder is blocking, `neutral` in WARN mode.

    `None` means the mode could not be read (a config-store or GitHub error
    on a terminal path). That fails closed: a WARN repo's Elder check is not
    required, so a red mark there holds nothing, while a `neutral` on a
    blocking repo would pass a required check with no review behind it."""
    return "neutral" if blocking is False else "failure"


def degraded_lead_line(reason: str) -> str:
    """The first line of every degraded summary, in the decision's words."""
    return (
        f"Degraded review ({reason or 'unknown'}): not a verdict. "
        "Comment `/grug improve` to re-run once the backend recovers."
    )


def lead_with_degraded_line(summary: str, reason: str) -> str:
    """`summary` with the degraded lead line as its first line."""
    return f"{degraded_lead_line(reason)}\n\n{summary}"


def gate_sentence(blocking: bool | None) -> str:
    """One sentence saying what this degraded check does to the merge."""
    if degraded_conclusion(blocking) == "failure":
        return (
            "Elder is blocking on this repo, so this check fails until a "
            "full review lands."
        )
    return "This check stays advisory - merge not blocked."
