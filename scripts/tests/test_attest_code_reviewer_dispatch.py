"""The dispatch attester follows the staged Elder review pass (#725)."""

import ast
import runpy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ATTESTER = REPO / "infra/scripts/attest_code_reviewer_dispatch.py"
DISPATCH = REPO / "services/_shared/personas/code_reviewer/dispatch.py"
CHECK_DISPATCH = runpy.run_path(str(ATTESTER))["_check_dispatch"]


def _failures(source: str) -> list[str]:
    return CHECK_DISPATCH(ast.parse(source), DISPATCH)


def test_dispatch_attestation_accepts_the_staged_pass():
    assert _failures(DISPATCH.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    ("original", "replacement", "expected"),
    [
        (
            '    mode: ReviewMode = "blocking" if blocking else "advisory"',
            '    mode: ReviewMode = "advisory"',
            "mode` not derived",
        ),
        ("blocking=blocking, mode=mode, cancel_event", "blocking=blocking, mode=\"advisory\", cancel_event", "derived `mode`"),
        ("evaluation, mode=run.mode, degraded_fails_closed=True,", 'evaluation, mode="advisory", degraded_fails_closed=True,', "non-`run.mode`"),
        ("evaluation, mode=run.mode, degraded_fails_closed=True,", "evaluation, mode=run.mode, degraded_fails_closed=False,", "degraded_fails_closed=True"),
        ("conclusion=plan.conclusion,\n        title=plan.title,", 'conclusion="success",\n        title=plan.title,', "CheckRunResult(conclusion=)"),
        ("        event=plan.event,\n", '        event="COMMENT",\n', "FinishedPass(event=)"),
        ("        return _PostedCheck(failed=True)", "        return _PostedCheck()", "check-run except"),
        ("        return _PostedReview(failed=not permanent, rejected=permanent)", "        return _PostedReview()", "review except"),
        ("        review_publish_failed=review.failed,\n", "", "review_publish_failed"),
        ("    check = _post_tier1_check(run, plan)\n", "", "never calls `_post_tier1_check`"),
        ("return _review_pass(", "return _other_pass(", "return _review_pass"),
        (
            "    finally:\n        _withdraw_other_commit_approvals(payload)\n",
            "    finally:\n        pass\n",
            "finally withdraws",
        ),
        ("payload, blocking=blocking, cancel_event=cancel_event,\n            rerun_pending", "payload, blocking=False, cancel_event=cancel_event,\n            rerun_pending", "forward blocking"),
    ],
)
def test_dispatch_attestation_rejects_a_broken_stage(original, replacement, expected):
    source = DISPATCH.read_text(encoding="utf-8")
    assert source.count(original) == 1, original
    failures = _failures(source.replace(original, replacement))
    assert failures, "mutation was not detected"
    assert any(expected in failure for failure in failures), failures
