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

grug#818: when Elder produced no review at all, four surfaces used to say so
in four voices ("eyes cloudy - read for self", "Elder skipped - <reason>",
"could not see the diff", "Review degraded - no usable findings"). They now
share the words below, which say plainly that no review happened, that the
fault is Grug's rather than the change's, and that an operator was alerted -
and `alert_operator_could_not_review` is the alert that makes the last part
true. The durable lane retries a transient failure before any of these
surfaces is written (`review_lifecycle.ALL_FAILED_AUTO_RERUN_DELIVERIES`).
"""
from __future__ import annotations

import logging
import os
from typing import Literal

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer")

# The board's verdict sentence for a pass with no review behind it. It is the
# email subject when it creates the board, so it states the fact and the
# operator page and leaves out any instruction to the author.
COULD_NOT_REVIEW_VERDICT = (
    "Grug could not review this pass. **An operator has been alerted.**"
)

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


def could_not_review_title(reason: str) -> str:
    """The check title for a pass with no review behind it."""
    return f"Grug could not review this pass ({reason or 'unknown'}) - operator alerted"


def could_not_review_sentence(reason: str) -> str:
    """The sentence every no-review surface (check summary, board body) uses."""
    return (
        f"Grug could not review this pass (`{reason or 'unknown'}`). The fault "
        "is in Grug, not in this change, and an operator has been alerted. "
        "Nothing here is a verdict on the code."
    )


def alert_operator_could_not_review(reason: str, *, pr: str, surface: str) -> None:
    """Page an operator: an author is about to read that Grug did not review.

    `elder_could_not_review` is an ERROR token on purpose, the same contract
    as `llm_backend_unusable`: every surface that writes the sentences above
    calls this first, so "an operator has been alerted" is never a claim with
    no alert behind it. `surface` names the writer, so the runbook can tell a
    dispatch blackout from a durable-lane fail-open."""
    log.error(
        "elder_could_not_review",
        extra={"reason": reason or "unknown", "pr": pr, "surface": surface},
    )


def gate_sentence(blocking: bool | None) -> str:
    """One sentence saying what this degraded check does to the merge."""
    if degraded_conclusion(blocking) == "failure":
        return (
            "Elder is blocking on this repo, so this check fails until a "
            "full review lands."
        )
    return "This check stays advisory - merge not blocked."
