"""The shared review-publish seam (#1185): one derivation of "did it degrade /
did publish fail" for the check-run, the persona result and the activity feed.

Guard, Smasher and Elder all import from `personas.review_publish`; no
persona reaches into another persona's underscore-private names.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from personas import review_publish as rp
from personas.code_reviewer.persona import CodeReviewEvaluation

_PERSONAS = pathlib.Path(rp.__file__).parent


def _eval(conclusion="success", degraded_reason=None, findings=()):
    return CodeReviewEvaluation(
        findings=findings, conclusion=conclusion, degraded_reason=degraded_reason,
    )


def test_public_surface_is_explicit():
    assert set(rp.__all__) >= {
        "PublishOutcome", "plan_publish", "fetch_pr_diff", "fetch_file_contents",
        "prior_finding_keys", "ReviewMode", "PersonaResultStr",
    }
    assert all(not name.startswith("_") for name in rp.__all__)
    assert all(hasattr(rp, name) for name in rp.__all__)


@pytest.mark.parametrize("mode,fails_closed,reason,want", [
    ("blocking", False, "all_failed", ("neutral", "COMMENT")),
    ("blocking", True, "all_failed", ("failure", "COMMENT")),
    ("advisory", True, "all_failed", ("neutral", "COMMENT")),
    ("blocking", True, "no_diff", ("neutral", "COMMENT")),
])
def test_plan_publish_gate(mode, fails_closed, reason, want):
    plan = rp.plan_publish(
        _eval("neutral", reason), mode=mode, degraded_fails_closed=fails_closed,
    )
    assert (plan.conclusion, plan.event) == want


def test_plan_publish_blocking_failure_requests_changes():
    plan = rp.plan_publish(_eval("failure"), mode="blocking")
    assert (plan.conclusion, plan.event) == ("failure", "REQUEST_CHANGES")
    assert rp.plan_publish(_eval("failure"), mode="advisory").conclusion == "neutral"


def test_unsettled_outcome_reads_the_evaluation_verdict():
    assert rp.plan_publish(_eval("success"), mode="advisory").result == "pass"
    assert rp.plan_publish(_eval("failure"), mode="advisory").result == "fail"
    assert rp.plan_publish(
        _eval("neutral", "all_failed"), mode="advisory",
    ).result == "skipped"


def test_publish_failure_outranks_everything_and_feeds_the_activity_row():
    plan = rp.plan_publish(_eval("neutral", "all_failed"), mode="advisory")
    check_down = plan.settled(check_publish_failed=True)
    assert check_down.result == "publish_failed"
    # The eval's own degradation keeps its label; a clean eval whose check
    # never landed is recorded as a publish failure, never a fake verdict.
    assert check_down.feed_degraded_reason == "all_failed"
    clean_down = rp.plan_publish(_eval(), mode="advisory").settled(
        check_publish_failed=True,
    )
    assert clean_down.feed_degraded_reason == "check_publish_failed"
    assert rp.plan_publish(_eval(), mode="advisory").feed_degraded_reason is None


def test_review_publish_failure_is_publish_failed_but_not_a_feed_error():
    plan = rp.plan_publish(_eval(), mode="advisory").settled(review_publish_failed=True)
    assert plan.result == "publish_failed"
    assert plan.feed_degraded_reason is None


def test_rejected_review_ranks_below_publish_failure_and_degradation():
    clean = rp.plan_publish(_eval(), mode="advisory")
    assert clean.settled(review_publish_rejected=True).result == "review_rejected"
    assert clean.settled(
        review_publish_rejected=True, review_publish_failed=True,
    ).result == "publish_failed"
    degraded = rp.plan_publish(_eval("neutral", "all_failed"), mode="advisory")
    assert degraded.settled(review_publish_rejected=True).result == "skipped"


def test_no_module_imports_a_private_name_from_the_code_reviewer_dispatch():
    """The cross-persona seam is `review_publish`; Elder's dispatch module is
    not a library for Guard and Smasher (the ticket's grep-clean criterion)."""
    offenders = []
    for path in _PERSONAS.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "personas.code_reviewer.dispatch"
            ):
                offenders += [
                    f"{path.name}: {a.name}" for a in node.names
                    if a.name.startswith("_")
                ]
    assert offenders == []


def test_no_persona_imports_a_private_name_from_another_persona():
    """The issue's first criterion in full (#511): a persona may share code
    with another persona only through public names, so a rename inside one
    persona cannot silently break another."""
    offenders = []
    for path in _PERSONAS.rglob("*.py"):
        rel = path.relative_to(_PERSONAS).parts
        own = rel[0] if len(rel) > 1 else None
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("personas.")
            ):
                continue
            other = node.module.split(".")[1]
            if other == own:
                continue
            offenders += [
                f"{'/'.join(rel)}: {node.module}.{a.name}"
                for a in node.names if a.name.startswith("_")
            ]
    assert offenders == []
