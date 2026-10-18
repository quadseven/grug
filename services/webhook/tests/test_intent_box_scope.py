"""Tests for the #1218 intent-mismatch box scope.

Recorded shape: a PR that only adds a network access grant links its issue
with `Refs #N`. Criteria 1 and 2 of that issue shipped in two earlier PRs.
Elder raised a blocking `intent-mismatch` for criterion 1; once that box was
ticked on the issue, a re-run raised a second blocking one for criterion 2.
Each round blocked a correct, reviewed change.

The scope holds:
- a finding about a box already ticked on a Refs-only issue is dropped;
- a Refs-only PR whose body claims no box gets no BLOCKING intent-mismatch
  for that issue's criteria (lowered to advisory medium);
- a box the PR body claims, and any box of an issue the PR closes, keep
  the finding as it was;
- the finding text names the box and whether it is ticked on the issue.
"""

from __future__ import annotations

from personas.code_reviewer.intent_filter import scope_intent_mismatch_to_claimed_boxes
from personas.code_reviewer.linked_issue import (
    render_linked_issue_block,
    rendered_criteria_with_state,
)
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

_ISSUE = 77
_C1 = "The worker pool registers its nodes with the scheduler on boot"
_C2 = "The scheduler routes queued jobs to a registered worker within one minute"
_C3 = "Workers on the private network can reach the scheduler port"
_PR_SENTENCE = "adds the network grant so the worker can reach the scheduler"
_BODY_REFS_ONLY = f"Adds the network grant.\n\nRefs #{_ISSUE}"


def _block(states: tuple[str, str, str]) -> str:
    return render_linked_issue_block(
        {_ISSUE: list(zip(states, (_C1, _C2, _C3), strict=True))},
    )


def _boxes(states: tuple[str, str, str]):
    return rendered_criteria_with_state(_block(states))


def _mismatch(criterion_no: int, text: str, severity: str = "high") -> Finding:
    """The recorded finding shape: names the box and quotes both texts."""
    return Finding(
        file="policy/grants.json",
        line=12,
        severity=severity,  # type: ignore[arg-type]
        rule_name="intent-mismatch",
        message=(
            f'Grug see #{_ISSUE} criterion {criterion_no} demand "{text}", yet '
            f'the change only says "{_PR_SENTENCE}". No hunk does this. '
            "So speaks Grug."
        ),
        suggestion=None,
    )


def _scope(findings, states, body=_BODY_REFS_ONLY, closed=()):
    return scope_intent_mismatch_to_claimed_boxes(
        findings, boxes=_boxes(states), closed_issues=closed, pr_body=body,
    )


def test_rendered_criteria_with_state_round_trips_the_block():
    assert _boxes(("ticked", "ticked", "open")) == [
        (_ISSUE, 1, "ticked", _C1),
        (_ISSUE, 2, "ticked", _C2),
        (_ISSUE, 3, "open", _C3),
    ]


def test_finding_on_a_ticked_box_of_a_refs_only_issue_is_dropped():
    """The recorded second round: criterion 2 is ticked on the issue."""
    kept, dropped, lowered = _scope(
        (_mismatch(1, _C1), _mismatch(2, _C2)), ("ticked", "ticked", "open"),
    )
    assert kept == ()
    assert (dropped, lowered) == (2, 0)


def test_ticked_box_matched_by_quote_alone_is_dropped():
    """A finding that quotes the criterion but never names its number."""
    finding = Finding(
        file="policy/grants.json", line=12, severity="high",
        rule_name="intent-mismatch",
        message=f'The issue asks "{_C2.lower()}." but the PR says "{_PR_SENTENCE}".',
        suggestion=None,
    )
    kept, dropped, _ = _scope((finding,), ("ticked", "ticked", "open"))
    assert kept == ()
    assert dropped == 1


def test_refs_only_pr_with_no_claimed_box_gets_no_blocking_mismatch():
    """The recorded first round: criterion 1 still open, body claims nothing."""
    kept, dropped, lowered = _scope((_mismatch(1, _C1),), ("open", "open", "open"))
    assert (dropped, lowered) == (0, 1)
    assert [f.severity for f in kept] == ["medium"]
    assert f"#{_ISSUE} criterion 1 (open on the issue)" in kept[0].message
    assert "Refs, not a closing keyword" in kept[0].message


def test_unattributed_mismatch_on_refs_only_pr_is_not_blocking():
    finding = Finding(
        file="policy/grants.json", line=12, severity="high",
        rule_name="intent-mismatch",
        message='The issue wants "something nobody wrote down" but "the PR says otherwise".',
        suggestion=None,
    )
    kept, _, lowered = _scope((finding,), ("open", "open", "open"))
    assert lowered == 1
    assert kept[0].severity == "medium"


def test_box_the_body_claims_keeps_a_blocking_finding_and_names_it():
    body = f"Delivers #{_ISSUE} criterion 3.\n\nRefs #{_ISSUE}"
    kept, dropped, lowered = _scope((_mismatch(3, _C3),), ("ticked", "ticked", "open"), body)
    assert (dropped, lowered) == (0, 0)
    assert kept[0].severity == "high"
    assert kept[0].message.endswith(f"Box: #{_ISSUE} criterion 3 (open on the issue).")


def test_body_repeating_the_criterion_text_claims_it():
    body = f"- [x] {_C3}\n\nRefs #{_ISSUE}"
    kept, _, lowered = _scope((_mismatch(3, _C3),), ("open", "open", "open"), body)
    assert lowered == 0
    assert kept[0].severity == "high"


def test_closing_pr_is_out_of_scope_but_names_the_box():
    body = f"Closes #{_ISSUE}"
    kept, dropped, lowered = _scope(
        (_mismatch(2, _C2),), ("ticked", "ticked", "open"), body, closed=(_ISSUE,),
    )
    assert (dropped, lowered) == (0, 0)
    assert kept[0].severity == "high"
    assert f"#{_ISSUE} criterion 2 (ticked on the issue)" in kept[0].message


def test_other_rules_and_no_linked_boxes_untouched():
    other = Finding(
        file="a.py", line=1, severity="high", rule_name="missing-error-handling",
        message="x", suggestion=None,
    )
    mismatch = _mismatch(1, _C1)
    kept, dropped, lowered = _scope((other,), ("ticked", "ticked", "open"))
    assert kept == (other,)
    kept, dropped, lowered = scope_intent_mismatch_to_claimed_boxes(
        (mismatch,), boxes=(), closed_issues=(), pr_body=_BODY_REFS_ONLY,
    )
    assert kept == (mismatch,)
    assert (dropped, lowered) == (0, 0)


# --- dispatch wiring ---------------------------------------------------------


def _evaluation(findings) -> CodeReviewEvaluation:
    from personas.code_reviewer.persona import with_findings

    base = CodeReviewEvaluation(
        findings=(), conclusion="success", dropped_hallucinations=0,
        degraded_reason=None,
    )
    return with_findings(base, tuple(findings))


def _filtered(findings, states, body=_BODY_REFS_ONLY):
    from personas.code_reviewer import dispatch

    return dispatch._filter_intent_mismatches(
        _evaluation(findings), installation_id=1, owner="o", repo_name="r",
        pull_number=5, pr_context={"body": body, "linked_issue_context": _block(states)},
    )


def test_dispatch_refs_pr_whose_unmet_boxes_are_all_ticked_passes():
    """Acceptance 1: no intent-mismatch, and the review is not blocked."""
    before = _evaluation((_mismatch(1, _C1), _mismatch(2, _C2)))
    assert before.conclusion == "failure"
    after = _filtered((_mismatch(1, _C1), _mismatch(2, _C2)), ("ticked", "ticked", "open"))
    assert [f.rule_name for f in after.findings] == []
    assert after.conclusion == "success"


def test_dispatch_refs_only_pr_with_no_claimed_box_is_not_blocked():
    """Acceptance 2: the open-box finding stays visible but cannot block."""
    after = _filtered((_mismatch(1, _C1),), ("open", "open", "open"))
    assert [f.severity for f in after.findings] == ["medium"]
    assert after.conclusion == "success"


def test_dispatch_closing_pr_still_blocks():
    after = _filtered(
        (_mismatch(2, _C2),), ("ticked", "ticked", "open"), body=f"Closes #{_ISSUE}",
    )
    assert after.conclusion == "failure"
