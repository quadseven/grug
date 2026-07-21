"""Async deep append after Tier-1 publish (#646)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch


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
