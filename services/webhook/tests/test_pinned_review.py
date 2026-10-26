"""grug#1429: Elder reviews pin the head, quote the check, give the budget,
and close explicitly.

Every test here asserts on the RENDERED review text - the board section and
the check-run summary a contributor actually reads - not on a prompt.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from llm_client import Backend, LlmReviewResponse
from personas import board
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer import pinned_review as pr
from personas.code_reviewer.complexity import _complexity_finding
from personas.code_reviewer.lint import _finding as ruff_finding
from personas.code_reviewer.persona import Budget, CodeReviewEvaluation, Finding

SHA = "0123456789abcdef0123456789abcdef01234567"


def _f(**kw) -> Finding:
    base = {"file": "a.py", "line": 3, "severity": "medium", "rule_name": "r",
            "message": "Something is off. More words.", "suggestion": None}
    base.update(kw)
    return Finding(**base)


def _visible(text: str) -> list[str]:
    return [line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("<!--")]


def _elder(body: str) -> list[str]:
    return _visible(board.extract_section(body, "elder") or "")


# --- the pin line ----------------------------------------------------------

def test_board_section_opens_with_full_head_sha_and_linked_issue():
    body = cr_dispatch._review_stack_body(
        CodeReviewEvaluation(findings=(), conclusion="success"),
        conclusion="success", pr_title="t", head_sha=SHA, linked_issues=(1429,),
    )
    assert _elder(body)[0] == f"Reviewed head `{SHA}` for #1429."


def test_inline_review_body_names_the_head_it_judged():
    result, _ = cr_dispatch.build_review_result(
        CodeReviewEvaluation(findings=(_f(severity="high"),), conclusion="failure"),
        head_sha=SHA, event="COMMENT",
    )
    assert result is not None
    assert f"Reviewed head `{SHA}`." in result.body


def test_no_head_means_no_invented_pin():
    assert pr.pin_line("") == ""


# --- budgets: current value, limit, what is left ---------------------------

def test_complexity_marking_renders_current_limit_and_cut():
    finding = _complexity_finding(
        "svc/x.py", "handle", 10, cyclo=19, cog=12,
        base_cyclo=None, base_cog=None, had_base=True,
        cyclo_cap=15, cog_cap=15, material=True,
    )
    body = cr_dispatch._review_stack_body(
        CodeReviewEvaluation(findings=(finding,), conclusion="success"),
        conclusion="success", head_sha=SHA,
    )
    assert "cyclomatic 19 -> at most 15 (cut 4)" in body
    # cognitive 12 is under its cap, so it is not a target.
    assert "cognitive 12" not in pr.fix_target(finding)


def test_fractional_budget_keeps_its_unit_and_decimals():
    assert pr.budget_text(Budget("comment share", 18.2, 15, "%")) == (
        "comment share 18.2% -> at most 15% (cut 3.2%)"
    )


# --- check output is quoted verbatim ---------------------------------------

def test_ruff_marking_quotes_the_check_output_line_verbatim():
    finding = ruff_finding("a.py", 3, "F401", "`os` imported but unused", None)
    line = pr.finding_line(1, finding)
    assert "Check output: `` a.py:3: F401 `os` imported but unused ``" in line


# --- outside-diff findings are follow-ups, never required ------------------

def test_outside_diff_finding_is_a_non_blocking_follow_up():
    outside = _f(file="other.py", line=9, severity="high",
                 outside_diff_reason="cross-file caller")
    tail = pr.pinned_tail(
        CodeReviewEvaluation(findings=(outside,), conclusion="success"),
    )
    assert "Follow-up issues to file (non-blocking, outside this diff)" in tail
    assert "Required fixes" not in tail
    assert tail.splitlines()[-1] == pr.CLOSURE_NOTHING


# --- explicit closure ------------------------------------------------------

@pytest.mark.parametrize(("findings", "closure"), [
    ((), pr.CLOSURE_NOTHING),
    ((_f(severity="low"),), pr.CLOSURE_ADVISORY_ONLY),
    ((_f(severity="high"), _f(line=4, severity="low")), pr.CLOSURE_COMPLETE),
])
def test_board_section_ends_with_the_closure_sentence(findings, closure):
    body = cr_dispatch._review_stack_body(
        CodeReviewEvaluation(findings=findings, conclusion="success"),
        conclusion="success", head_sha=SHA,
    )
    assert _elder(body)[-1] == closure


def test_required_fix_is_numbered_with_its_target():
    tail = pr.pinned_tail(CodeReviewEvaluation(
        findings=(_f(severity="high", suggestion="Guard the None case."),),
        conclusion="failure",
    ))
    assert "**Required fixes**" in tail
    assert "1. `a.py:3` `r` - Guard the None case" in tail


def test_degraded_pass_refuses_to_claim_nothing_else():
    ev = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="all_failed",
    )
    assert pr.closure_line(ev) == pr.CLOSURE_DEGRADED


# --- the published check-run summary ---------------------------------------

_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def _payload() -> dict:
    return {
        "action": "opened",
        "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {
            "number": 7,
            "head": {"sha": SHA},
            "base": {"sha": "base5678ijkl"},
            "title": "Preserve PR intent",
            "body": "Refs #1429",
            "user": {"login": "alice"},
        },
    }


@pytest.fixture
def posted_checks(monkeypatch):
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "0")
    posted: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 2})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())

    def run(llm: LlmReviewResponse) -> list:
        monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
        diff = MagicMock(spec=httpx.Response)
        diff.status_code = 200
        diff.raise_for_status = MagicMock()
        diff.text = _DIFF
        with patch("httpx.get", return_value=diff):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)
        return posted

    return run


def test_published_check_summary_is_pinned_and_closed(posted_checks):
    posted = posted_checks(LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
    ))
    final = posted[-1].summary
    assert final.splitlines()[0] == f"Reviewed head `{SHA}` for #1429."
    assert final.rstrip().splitlines()[-1] == pr.CLOSURE_NOTHING


def test_degraded_check_summary_leads_with_degraded_line_then_pin(posted_checks):
    posted = posted_checks(LlmReviewResponse(kind="all_failed", error="x: timeout"))
    lines = _visible(posted[-1].summary)
    assert lines[0].startswith("Degraded review (all_failed):")
    assert lines[1] == f"Reviewed head `{SHA}` for #1429."
    assert lines[-1] == pr.CLOSURE_DEGRADED
