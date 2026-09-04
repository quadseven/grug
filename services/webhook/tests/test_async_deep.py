"""Async deep append after Tier-1 publish (#646)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch
from review_pipeline import ReviewCoverage


_DIFF = """diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
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
        "repository": {
            "id": 22,
            "name": "myrepo",
            "owner": {"login": "myorg"},
        },
        "pull_request": {
            "number": 7,
            "head": {"sha": "abcd1234efgh"},
            "base": {"sha": "base5678ijkl"},
            "title": "auth change",
            "body": "deep-review please",
            "user": {"login": "evan"},
        },
    }


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "tiered")
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "1")
    monkeypatch.setenv("GRUG_DEEP_SAMPLE_RATE", "0")
    monkeypatch.setenv("GRUG_DEEP_DIFF_LINES", "1")


def _diff_response(diff: str = _DIFF):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = diff
    return r


def test_async_deep_appends_after_tier1_publish(monkeypatch):
    """Required path publishes first; reasoner appends when escalation fires."""
    tier1 = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/auth.py", line=2, rule="silent-failure",
            severity="medium", message="tier1",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.CAVE,
        model_name="coder",
        backends_used=(Backend.CAVE,),
        models_used=("coder",),
    )
    deep = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/auth.py", line=3, rule="null-deref",
            severity="high", message="deep",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.CAVE_REASONER,
        model_name="reasoner",
        backends_used=(Backend.CAVE_REASONER,),
        models_used=("reasoner",),
    )
    posted_checks: list = []
    posted_reviews: list = []
    call_order: list[str] = []

    def _fake_review_diff(*a, **kw):
        call_order.append("tier1")
        return tier1

    def _fake_reasoner(*a, **kw):
        call_order.append("deep")
        return deep

    def _fake_post_check(install_token, owner, repo, result, external_id=None):
        posted_checks.append({
            "title": result.title,
            "external_id": external_id,
            "conclusion": result.conclusion,
        })
        call_order.append("check")
        return {"id": len(posted_checks)}

    def _fake_post_review(install_token, owner, repo, *, pull_number, result):
        posted_reviews.append(result)
        call_order.append("review")
        return {"id": len(posted_reviews)}

    monkeypatch.setattr(cr_dispatch, "review_diff", _fake_review_diff)
    monkeypatch.setattr(cr_dispatch, "review_reasoner_diff", _fake_reasoner)
    monkeypatch.setattr(cr_dispatch, "post_check_run", _fake_post_check)
    monkeypatch.setattr(cr_dispatch, "post_review", _fake_post_review)
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch(
        "adapters.install_store.put_elder_last_reviewed",
        lambda **k: None,
    ), patch(
        "adapters.install_store.get_elder_last_reviewed",
        return_value=None,
    ):
        with patch("httpx.get", return_value=_diff_response()):
            out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "pass"
    # Tier-1 check before deep arm; deep check after.
    assert call_order.index("tier1") < call_order.index("check")
    assert call_order.index("check") < call_order.index("deep")
    assert any("deep" in (c.get("title") or "") for c in posted_checks)
    assert any(
        (c.get("external_id") or "").startswith("grug-cr-deep:")
        for c in posted_checks
    )
    # Two reviews: tier1 findings + deep findings (different lines).
    assert len(posted_reviews) >= 2


def test_async_deep_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "0")
    tier1 = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE, model_name="coder",
        backends_used=(Backend.CAVE,), models_used=("coder",),
    )
    reasoner_calls = []

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: tier1)
    monkeypatch.setattr(
        cr_dispatch, "review_reasoner_diff",
        lambda *a, **kw: reasoner_calls.append(1) or tier1,
    )
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **k: {"id": 1},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **k: {"id": 2},
    )
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None), \
         patch("adapters.install_store.get_elder_last_reviewed", return_value=None), \
         patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert reasoner_calls == []


def test_async_deep_skipped_after_staged_tier1(monkeypatch):
    tier1 = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE, model_name="coder",
        backends_used=(Backend.CAVE,), models_used=("coder",),
    )
    reasoner_calls = []

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: tier1)
    monkeypatch.setattr(cr_dispatch, "review_is_staged", lambda hunks: True)
    monkeypatch.setattr(
        cr_dispatch, "review_reasoner_diff",
        lambda *a, **kw: reasoner_calls.append(1) or tier1,
    )
    monkeypatch.setattr(
        cr_dispatch, "post_check_run", lambda *a, **k: {"id": 1},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review", lambda *a, **k: {"id": 2},
    )
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None), \
         patch("adapters.install_store.get_elder_last_reviewed", return_value=None), \
         patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert reasoner_calls == []


def test_async_deep_partial_coverage_never_publishes_success(monkeypatch):
    """Reproduces the PR #127 shape (grug#813/#707): Tier-1 is clean, but
    the deep (reasoner) arm exceeds ITS OWN cohort budget and comes back
    `partial_review` with no novel findings (everything it did see was
    already covered by Tier-1). Before the fix, the merge that builds
    `combined` for the deep check-run only ever looked at Tier-1's
    `degraded_reason` - the deep arm's own partial coverage was silently
    dropped, and the deep check-run published `success` over ground the
    deep arm never finished walking. This is the exact mismatch reported
    live: a board comment saying "some ground not walked" next to a
    check-run conclusion of `success`.

    MUST fail on main (deep check conclusion == "success") and pass once
    `with_degradation` folds the deep arm's coverage into `combined`.
    """
    tier1 = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE, model_name="coder",
        backends_used=(Backend.CAVE,), models_used=("coder",),
    )
    deep = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE_REASONER, model_name="reasoner",
        backends_used=(Backend.CAVE_REASONER,), models_used=("reasoner",),
        # The deep arm's own cohort plan ran out of budget - #813's shape,
        # measured on byte-identical copies eating the budget before the
        # novel logic. `findings=()` means nothing NEW survives dedup
        # against Tier-1, which is exactly the case that used to erase the
        # degradation entirely.
        error="partial review: cohorts [2] failed",
        coverage=ReviewCoverage(
            total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
            cohort_labels=("a", "b"),
        ),
    )
    posted_checks: list = []

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: tier1)
    monkeypatch.setattr(cr_dispatch, "review_reasoner_diff", lambda *a, **kw: deep)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted_checks.append(
                {"external_id": external_id, "conclusion": result.conclusion}
            )
            or {"id": len(posted_checks)}
        ),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None), \
         patch("adapters.install_store.get_elder_last_reviewed", return_value=None), \
         patch("httpx.get", return_value=_diff_response()):
        # `blocking=True` -> mode="blocking": `_publish_shape` only forces
        # `neutral` from `evaluation.degraded_reason`, not from mode itself.
        # advisory mode (`blocking=False`) forces `neutral` unconditionally
        # and would pass this test even with the bug present - it has to be
        # blocking mode to actually exercise the coverage-honesty check.
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    deep_checks = [
        c for c in posted_checks
        if (c["external_id"] or "").startswith("grug-cr-deep:")
    ]
    assert deep_checks, "deep check-run was never posted"
    assert deep_checks[-1]["conclusion"] != "success", (
        "deep arm hit partial coverage but its check-run reported success - "
        "coverage honesty regression"
    )


def _clean_tier1() -> LlmReviewResponse:
    return LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE, model_name="coder",
        backends_used=(Backend.CAVE,), models_used=("coder",),
    )


def _run_escalated_review(monkeypatch, *, deep: LlmReviewResponse) -> list[dict]:
    """Drive one escalated tiered review (clean Tier-1, `deep` from the
    reasoner) in blocking mode and return every posted check-run.

    `blocking=True` on purpose: advisory mode forces `neutral` unconditionally
    and would hide a Tier-1 `success` left standing by a failed deep arm."""
    posted_checks: list[dict] = []

    def _fake_post_check(token, owner, repo, result, external_id=None):
        posted_checks.append({
            "external_id": external_id,
            "conclusion": result.conclusion,
            "title": result.title,
            "summary": result.summary,
        })
        return {"id": len(posted_checks)}

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _clean_tier1())
    monkeypatch.setattr(cr_dispatch, "review_reasoner_diff", lambda *a, **kw: deep)
    monkeypatch.setattr(cr_dispatch, "post_check_run", _fake_post_check)
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None), \
         patch("adapters.install_store.get_elder_last_reviewed", return_value=None), \
         patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)
    return posted_checks


def _deep_checks(posted_checks: list[dict]) -> list[dict]:
    return [
        c for c in posted_checks
        if (c["external_id"] or "").startswith("grug-cr-deep:")
    ]


@pytest.mark.parametrize(
    "failed_deep",
    [
        LlmReviewResponse(
            kind="all_failed", error="cave-reasoner: ConnectError",
            backend_used=Backend.CAVE_REASONER,
        ),
        LlmReviewResponse(
            kind="parse_failed", error="content was prose, not JSON",
            backend_used=Backend.CAVE_REASONER, model_name="reasoner",
        ),
    ],
    ids=["all_failed", "parse_failed"],
)
def test_async_deep_arm_failure_never_leaves_tier1_success_unqualified(
    monkeypatch, caplog, failed_deep,
):
    """grug#848: Tier-1 is clean and publishes `success` with the promise
    "reasoner may append later if escalated". Escalation fires, and the
    reasoner arm then fails outright (transport error, or a reply that
    never parsed). Before the fix `_async_deep_append_if_needed` logged
    `elder_async_deep_arm_empty` and returned - no check-run, no note - so
    the Tier-1 `success` stood unqualified and the author could not tell
    "the reasoner looked and agreed" from "the reasoner never looked".

    MUST fail on main (no `grug-cr-deep:` check-run is ever posted) and pass
    once the failed arm folds into Tier-1 through `with_degradation` /
    `_derive_conclusion` and completes the deep check-run `neutral`."""
    with caplog.at_level("INFO"):
        posted = _run_escalated_review(monkeypatch, deep=failed_deep)

    # The branch is exercised (issue acceptance: the log gains a test) ...
    empty = [r for r in caplog.records if r.message == "elder_async_deep_arm_empty"]
    assert len(empty) == 1
    assert empty[0].kind == failed_deep.kind  # type: ignore[attr-defined]
    # ... and it is no longer silent.
    deep = _deep_checks(posted)
    assert deep, (
        "deep arm failed outright but no deep check-run was posted - the "
        "Tier-1 success stands unqualified"
    )
    assert deep[-1]["conclusion"] == "neutral"
    assert "did not run" in deep[-1]["title"]
    assert failed_deep.kind in deep[-1]["title"]
    # The Tier-1 verdict is not disowned: this is "the second look never
    # happened", not "Grug could not see the diff".
    assert "could not see the diff" not in deep[-1]["summary"]
    assert "did not run" in deep[-1]["summary"]
    # Raw backend/parse error text never reaches the author-facing surface
    # (a parse failure's text can be model prose) - the log carries it.
    assert failed_deep.error not in deep[-1]["summary"]


def test_async_deep_arm_ran_clean_still_publishes_success(monkeypatch):
    """The other half of the #848 distinction: a reasoner that RAN and found
    nothing is a legitimately clean deep pass. The failure handling must not
    turn every escalated-but-clean review into a neutral - `success`, with a
    summary that says the arm ran, not that it was skipped."""
    ran_clean = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE_REASONER, model_name="reasoner",
        backends_used=(Backend.CAVE_REASONER,), models_used=("reasoner",),
    )

    posted = _run_escalated_review(monkeypatch, deep=ran_clean)

    deep = _deep_checks(posted)
    assert deep, "deep check-run was never posted"
    assert deep[-1]["conclusion"] == "success"
    assert "did not run" not in deep[-1]["title"]
    assert "did not run" not in deep[-1]["summary"]
    assert "appended after Tier-1 completed" in deep[-1]["summary"]


def test_async_deep_arm_no_diff_is_not_a_failure(monkeypatch, caplog):
    """`no_diff` from the reasoner means there was nothing for it to look at
    (BENIGN_DEGRADATIONS) - not a failure to look. It must not manufacture a
    degraded deep check-run over a clean Tier-1."""
    with caplog.at_level("INFO"):
        posted = _run_escalated_review(
            monkeypatch, deep=LlmReviewResponse(kind="no_diff"),
        )

    assert [r for r in caplog.records if r.message == "elder_async_deep_arm_empty"]
    assert _deep_checks(posted) == []
