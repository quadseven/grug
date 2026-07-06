"""Teller dispatch tests (#554): fetch, LLM-summary fallback, upsert-by-marker
(PATCH-else-POST), and the honest degrade-comment path. Network mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from personas.walkthrough import dispatch as wt_dispatch
from personas.walkthrough.render import MARKER


def _payload(pull_number: int = 7, head_sha: str = "a" * 40) -> dict:
    return {
        "action": "opened",
        "pull_request": {"number": pull_number, "head": {"sha": head_sha}},
        "repository": {"owner": {"login": "o"}, "name": "r"},
        "installation": {"id": 1},
    }


def _files_response(files: list[dict]) -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=files)
    return r


def _diff_response(text: str = "diff --git a/x.py b/x.py\n") -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = text
    return r


def _empty_comments_response() -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=[])
    return r


def _marker_comment_response(comment_id: int) -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=[{"id": comment_id, "body": MARKER}])
    return r


def test_dispatch_posts_new_comment_when_no_marker_exists(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    posted = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([
                {"filename": "x.py", "additions": 3, "deletions": 1},
            ])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    def fake_post(url, **kwargs):
        posted["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "degraded"}
    assert MARKER in posted["body"]
    assert "x.py" in posted["body"]
    mock_rcv.assert_called_once()
    assert mock_rcv.call_args.kwargs["conclusion"] == "success"


def test_dispatch_patches_existing_marker_comment_in_place(monkeypatch):
    """Acceptance criterion: updated IN PLACE on a re-dispatch (synchronize)."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    patched = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "y.py", "additions": 1, "deletions": 0}])
        if "/comments" in url:
            return _marker_comment_response(999)
        return _diff_response()

    def fake_patch(url, **kwargs):
        patched["url"] = url
        patched["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.patch", side_effect=fake_patch), \
         patch("httpx.post") as mock_post, \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        wt_dispatch.dispatch_walkthrough_review(_payload(head_sha="b" * 40), blocking=False)

    assert patched["url"].endswith("/issues/comments/999")
    assert MARKER in patched["body"]
    mock_post.assert_not_called()


def test_dispatch_uses_llm_summary_and_per_file_blurbs(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    from llm_client import WalkthroughSummary

    posted = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    def fake_post(url, **kwargs):
        posted["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    llm_summary = WalkthroughSummary(
        summary="Adds a null guard to x.py.",
        file_summaries={"x.py": "added guard clause"},
        effort="quick",
    )
    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=llm_summary):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "pass"}
    assert "Adds a null guard to x.py." in posted["body"]
    assert "added guard clause" in posted["body"]
    assert "quick" in posted["body"]


def test_dispatch_fetch_failure_returns_failed_without_posting(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    with patch("httpx.get", side_effect=httpx.ConnectError("dns down")), \
         patch("httpx.post") as mock_post, \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv:
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    mock_post.assert_not_called()
    mock_rcv.assert_not_called()


def test_dispatch_publish_failure_does_not_record_activity(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=httpx.ConnectError("dns down")), \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "publish_failed"}
    mock_rcv.assert_not_called()
