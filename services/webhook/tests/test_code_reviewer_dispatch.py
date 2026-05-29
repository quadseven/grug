"""Tests for personas/code_reviewer/dispatch.dispatch_code_review.

The dispatch function orchestrates: fetch PR diff via GH API → parse →
LLM → evaluate → publish check-run + inline review. Mocks every
downstream call; no real network or DDB."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch


_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def _payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "installation": {"id": 11},
        "repository": {
            "id": 22,
            "name": "myrepo",
            "owner": {"login": "myorg"},
        },
        "pull_request": {
            "number": 7,
            "head": {"sha": "abcd1234efgh"},
        },
    }


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    """Skip the real with_install_token_retry by stubbing it to call
    the wrapped function directly with a fake token."""
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )


def _diff_response(diff: str = _DIFF):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = diff
    return r


def test_dispatch_advisory_mode_posts_neutral_check_and_comment_review(monkeypatch):
    """Default mode (`code_reviewer_blocking=False`): check-run
    conclusion=neutral, review event=COMMENT. Both clients called with
    the right payload."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="catches Exception silently",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        model_name="laguna",
    )
    posted_check = []
    posted_review = []

    def _fake_review_diff(hunks, installation_id):
        return llm

    def _fake_post_check_run(install_token, owner, repo, result, external_id=None):
        posted_check.append({"owner": owner, "repo": repo, "result": result})
        return {"id": 1}

    def _fake_post_review(install_token, owner, repo, *, pull_number, result):
        posted_review.append({"pull_number": pull_number, "result": result})
        return {"id": 2}

    monkeypatch.setattr(cr_dispatch, "review_diff", _fake_review_diff)
    monkeypatch.setattr(cr_dispatch, "post_check_run", _fake_post_check_run)
    monkeypatch.setattr(cr_dispatch, "post_review", _fake_post_review)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(
            _payload(), blocking=False,
        )

    assert out["status"] == "dispatched"
    assert out["persona"] == "code_reviewer"
    assert len(posted_check) == 1
    assert posted_check[0]["result"].conclusion == "neutral"  # advisory
    assert len(posted_review) == 1
    assert posted_review[0]["result"].event == "COMMENT"  # advisory
    # Inline comments include the finding.
    inline = posted_review[0]["result"].comments
    assert len(inline) == 1
    assert inline[0].path == "src/x.py"
    assert inline[0].line == 2


def test_dispatch_blocking_mode_uses_failure_conclusion_and_request_changes(monkeypatch):
    """`code_reviewer_blocking=True` + a high/critical finding flips
    check-run conclusion=failure and review event=REQUEST_CHANGES."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="secret-leak",
            severity="critical", message="API key in log",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    posted_check, posted_review = [], []

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    assert posted_check[0].conclusion == "failure"
    assert posted_review[0].event == "REQUEST_CHANGES"


def test_dispatch_no_findings_posts_clean_pass(monkeypatch):
    """LLM returned reviewed + empty findings → check conclusion=success
    (or neutral in advisory mode), review event=COMMENT, zero inline
    comments. Don't post a noisy "looks good!" inline comment when
    there's nothing to flag."""
    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
    )
    posted_check, posted_review = [], []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # Clean pass — check posts but with no findings table.
    assert posted_check[0].conclusion == "neutral"  # advisory always neutral
    # No inline review when there's nothing to comment on.
    assert posted_review == []


def test_dispatch_llm_outage_does_not_block_pr(monkeypatch):
    """LLM all_failed → advisory check-run posted with conclusion=neutral
    and degraded_reason message. No inline review (nothing to comment
    on). Elder must not 500 the dispatcher on LLM outages."""
    llm = LlmReviewResponse(kind="all_failed", error="poolside: timeout")
    posted_check, posted_review = [], []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    # Even in blocking mode, an LLM outage degrades to neutral —
    # advisory-first contract.
    assert posted_check[0].conclusion == "neutral"
    assert posted_review == []
    assert out["status"] == "dispatched"
    assert out["result"] == "skipped"


def test_dispatch_unparseable_diff_yields_neutral(monkeypatch):
    """parse_diff raises DiffParseError on a malformed @@ header. Caller
    must catch and degrade to advisory neutral — Elder cannot fail a
    PR on parser issues."""
    bad_diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ garbled @@\n+foo\n"
    posted_check = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )

    with patch("httpx.get", return_value=_diff_response(bad_diff)):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert posted_check[0].conclusion == "neutral"
    assert out["result"] == "skipped"


def test_dispatch_fetches_diff_with_diff_accept_header(monkeypatch):
    """Confirms the GH API call uses `Accept: application/vnd.github.diff`
    so we get the unified-diff body rather than the JSON metadata."""
    captured = []
    monkeypatch.setattr(
        cr_dispatch, "review_diff",
        lambda *a, **kw: LlmReviewResponse(kind="no_diff"),
    )
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    def capture_get(url, *, headers, timeout):
        captured.append({"url": url, "headers": headers, "timeout": timeout})
        return _diff_response()

    with patch("httpx.get", side_effect=capture_get):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert len(captured) == 1
    assert captured[0]["headers"]["Accept"] == "application/vnd.github.diff"
    assert "myorg/myrepo" in captured[0]["url"]
    assert "/pulls/7" in captured[0]["url"]
    assert captured[0]["timeout"] == 30
