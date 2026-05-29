"""Tests for github_reviews_client.post_review.

Covers the request shape (URL, auth header, API version, body fields),
event validation, inline-comment payload, and 401-propagation. Mocks
httpx.post — no real GH API calls.

Mirrors test_github_checks_client.py patterns per ADR-0001.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import httpx
import pytest

from github_reviews_client import (
    InlineComment,
    ReviewResult,
    post_review,
)


def _ok_response(json_body=None):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body or {"id": 999, "state": "COMMENTED"})
    return r


def test_post_review_url_and_auth():
    """URL = /repos/{owner}/{repo}/pulls/{n}/reviews + Bearer auth +
    GitHub Accept + API version headers (mirrors checks client)."""
    result = ReviewResult(
        commit_id="abc123def456",
        event="COMMENT",
        body="Grug review: 1 finding",
        comments=(),
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        out = post_review("tok-123", "myorg", "myrepo", pull_number=42, result=result)

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.github.com/repos/myorg/myrepo/pulls/42/reviews"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert kwargs["timeout"] == 10
    assert out == {"id": 999, "state": "COMMENTED"}


def test_post_review_body_comment_event_no_findings():
    """Advisory mode: event=COMMENT, body summary, empty comments list
    when nothing was found."""
    result = ReviewResult(
        commit_id="abc",
        event="COMMENT",
        body="No findings",
        comments=(),
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        post_review("tok", "o", "r", pull_number=1, result=result)

    body = mock_post.call_args.kwargs["json"]
    assert body["commit_id"] == "abc"
    assert body["event"] == "COMMENT"
    assert body["body"] == "No findings"
    # `asdict` preserves tuple → tuple; JSON-serializes to `[]`. The
    # wire payload is identical to a list-of-dicts.
    assert list(body["comments"]) == []


def test_post_review_body_request_changes_with_inline_comments():
    """Blocking mode: event=REQUEST_CHANGES, inline comments serialized
    into the GH-required {path, line, body} shape."""
    result = ReviewResult(
        commit_id="abc",
        event="REQUEST_CHANGES",
        body="2 critical findings",
        comments=(
            InlineComment(path="src/x.py", line=10, body="Critical: silent-exception"),
            InlineComment(path="src/y.py", line=42, body="Critical: secret-in-log"),
        ),
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        post_review("tok", "o", "r", pull_number=42, result=result)

    body = mock_post.call_args.kwargs["json"]
    assert body["event"] == "REQUEST_CHANGES"
    assert len(body["comments"]) == 2
    assert body["comments"][0] == {
        "path": "src/x.py", "line": 10, "body": "Critical: silent-exception",
    }
    assert body["comments"][1] == {
        "path": "src/y.py", "line": 42, "body": "Critical: secret-in-log",
    }


def test_review_result_rejects_invalid_event():
    """type-design: only COMMENT and REQUEST_CHANGES are permitted.
    APPROVE would be wrong shape for an automated reviewer that does
    not actually approve PRs; PENDING leaves the review unsubmitted
    (a draft) which the persona does not use. Reject at construction."""
    with pytest.raises((TypeError, ValueError)):
        ReviewResult(  # type: ignore[arg-type]
            commit_id="abc",
            event="APPROVE",
            body="ok",
            comments=(),
        )


def test_review_result_rejects_zero_line_in_inline_comment():
    """GitHub's PR Reviews API 422s on line=0. Catch at construction
    so the bad payload never leaves the process. Uses ValueError (not
    AssertionError) so `python -O` cannot strip the guard."""
    with pytest.raises(ValueError, match="line must be >= 1"):
        InlineComment(path="x.py", line=0, body="msg")


def test_review_result_rejects_empty_commit_id():
    with pytest.raises(ValueError, match="commit_id must be non-empty"):
        ReviewResult(commit_id="", event="COMMENT", body="x", comments=())


def test_review_result_rejects_empty_path_in_inline_comment():
    with pytest.raises(ValueError, match="path must be non-empty"):
        InlineComment(path="", line=1, body="msg")


def test_review_result_rejects_body_over_github_limit():
    """GitHub 422s on review body > 65536 chars. A verbose-mode prompt
    could spill over in a future slice; guard at construction so the
    bad payload never crosses the wire."""
    big = "x" * 65537
    with pytest.raises(ValueError, match="exceeds GitHub's"):
        ReviewResult(commit_id="abc", event="COMMENT", body=big, comments=())


def test_review_result_accepts_body_at_github_limit():
    """The boundary case — exactly 65536 chars must NOT raise."""
    body = "x" * 65536
    r = ReviewResult(commit_id="abc", event="COMMENT", body=body, comments=())
    assert len(r.body) == 65536


def test_inline_comment_is_frozen():
    import dataclasses
    c = InlineComment(path="x.py", line=1, body="msg")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.path = "other.py"  # type: ignore[misc]


def test_review_result_is_frozen():
    import dataclasses
    r = ReviewResult(commit_id="abc", event="COMMENT", body="", comments=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.event = "REQUEST_CHANGES"  # type: ignore[misc]


def test_post_review_401_propagates_for_retry_helper(mock_transport_client):
    """post_review does NOT swallow 401 — `with_install_token_retry`
    is responsible for invalidating the cache + retrying. Mirrors
    `test_post_check_run_401_propagates_for_retry_helper`."""
    result = ReviewResult(
        commit_id="abc", event="COMMENT", body="x", comments=(),
    )
    client = mock_transport_client(status_codes=[401])

    with patch("httpx.post", side_effect=lambda *a, **kw: client.post(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            post_review("stale-tok", "o", "r", pull_number=1, result=result)
    assert exc.value.response.status_code == 401


def test_post_review_500_propagates_unwrapped(mock_transport_client):
    result = ReviewResult(
        commit_id="abc", event="COMMENT", body="x", comments=(),
    )
    client = mock_transport_client(status_codes=[500])
    with patch("httpx.post", side_effect=lambda *a, **kw: client.post(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            post_review("tok", "o", "r", pull_number=1, result=result)
    assert exc.value.response.status_code == 500


def test_post_review_connect_error_propagates(mock_transport_client):
    """Transport-level ConnectError must propagate (not be caught by a
    too-narrow `httpx.HTTPStatusError` handler). Mirrors the gap from
    issue #105 / async-blocker-hunter F-01."""
    result = ReviewResult(
        commit_id="abc", event="COMMENT", body="x", comments=(),
    )
    client = mock_transport_client(raise_exc=httpx.ConnectError("dns down"))
    with patch("httpx.post", side_effect=lambda *a, **kw: client.post(*a, **kw)):
        with pytest.raises(httpx.ConnectError):
            post_review("tok", "o", "r", pull_number=1, result=result)


