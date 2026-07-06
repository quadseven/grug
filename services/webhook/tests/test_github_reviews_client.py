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
        "side": "RIGHT",
    }
    assert body["comments"][1] == {
        "path": "src/y.py", "line": 42, "body": "Critical: secret-in-log",
        "side": "RIGHT",
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


def test_inline_comment_rejects_negative_line():
    """A buggy diff-parser could emit a negative line. Guard is
    `line < 1`, so only `line=0` would have hit it in the prior test
    set — a future refactor to `line == 0` would regress silently."""
    with pytest.raises(ValueError, match="line must be >= 1"):
        InlineComment(path="x.py", line=-5, body="msg")


def test_review_result_rejects_pending_event():
    """PENDING is explicitly called out in the module docstring as
    rejected (creates a draft review that never publishes — the persona
    has no use for it). Only APPROVE was previously tested; this covers
    the second documented rejection."""
    with pytest.raises(ValueError, match="event must be one of"):
        ReviewResult(  # type: ignore[arg-type]
            commit_id="abc", event="PENDING", body="x", comments=(),
        )


def test_post_review_pull_number_flows_into_url_distinctly():
    """`pull_number` must end up as the int path segment in
    /pulls/{pull_number}/reviews — distinct from `repo`. A bug that
    swapped them would build `/pulls/myrepo/reviews` and 404. Guard
    against an accidental f-string field-swap regression."""
    result = ReviewResult(
        commit_id="abc", event="COMMENT", body="", comments=(),
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        post_review("tok", "myorg", "myrepo", pull_number=42, result=result)

    url = mock_post.call_args.args[0]
    assert "/pulls/42/" in url
    assert "/myrepo/" in url
    # And critically, the segment after `/pulls/` is the int, not the repo:
    assert "/pulls/myrepo" not in url


def test_post_review_422_propagates_unwrapped(mock_transport_client):
    """422 is GitHub's "payload accepted but rejected by validation"
    response — e.g. unknown commit_id, line outside the diff. The
    caller may want to distinguish 422 from 401 (no token-refresh
    helps) and 5xx (transient). Confirm the unwrapped-propagation
    contract holds for the status code that's most actionable."""
    result = ReviewResult(
        commit_id="abc", event="COMMENT", body="x", comments=(),
    )
    client = mock_transport_client(status_codes=[422])
    with patch("httpx.post", side_effect=lambda *a, **kw: client.post(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            post_review("tok", "o", "r", pull_number=1, result=result)
    assert exc.value.response.status_code == 422


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
    too-narrow `httpx.HTTPStatusError` handler)."""
    result = ReviewResult(
        commit_id="abc", event="COMMENT", body="x", comments=(),
    )
    client = mock_transport_client(raise_exc=httpx.ConnectError("dns down"))
    with patch("httpx.post", side_effect=lambda *a, **kw: client.post(*a, **kw)):
        with pytest.raises(httpx.ConnectError):
            post_review("tok", "o", "r", pull_number=1, result=result)




# --- get_review_comments (#247a — capture inline-comment IDs) ---

def _list_response(items):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=items)
    return r


def test_get_review_comments_url_and_auth():
    """GET /repos/{o}/{r}/pulls/{n}/reviews/{review_id}/comments + Bearer.
    The create-review response doesn't surface per-comment IDs, so reaction
    capture fetches them here."""
    from github_reviews_client import get_review_comments
    body = [{"id": 1, "path": "a.py", "line": 2}, {"id": 5, "path": "b.py", "line": 9}]
    with patch("httpx.get", return_value=_list_response(body)) as mg:
        out = get_review_comments("tok-x", "o", "r", pull_number=42, review_id=999)
    args, kwargs = mg.call_args
    assert args[0] == "https://api.github.com/repos/o/r/pulls/42/reviews/999/comments"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-x"
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert out == body


def test_get_review_comments_propagates_401():
    """Like post_review/post_check_run, 401 is NOT swallowed — the
    with_install_token_retry wrapper owns cache-invalidate + retry."""
    from github_reviews_client import get_review_comments
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 401
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=resp)
    )
    with patch("httpx.get", return_value=resp):
        with pytest.raises(httpx.HTTPStatusError):
            get_review_comments("tok", "o", "r", pull_number=1, review_id=2)


def test_get_review_comments_paginates_until_short_page():
    """>100 comments are NOT silently truncated — paginate until a short
    page (the #189 lesson)."""
    from github_reviews_client import get_review_comments
    page1 = [{"id": i, "path": "a.py", "line": i} for i in range(100)]
    page2 = [{"id": 999, "path": "b.py", "line": 1}]
    seen_pages = []

    def _staged(url, **kw):
        pg = kw["params"]["page"]
        seen_pages.append(pg)
        return _list_response(page1 if pg == 1 else page2)

    with patch("httpx.get", side_effect=_staged):
        out = get_review_comments("tok", "o", "r", pull_number=1, review_id=2)
    assert len(out) == 101
    assert seen_pages == [1, 2]   # stopped after the short final page


def test_get_review_comments_non_list_body_breaks():
    """A non-list 200 body (error envelope that passed raise_for_status)
    breaks the loop rather than raising on .extend — returns what accrued."""
    from github_reviews_client import get_review_comments
    with patch("httpx.get", return_value=_list_response({"message": "boom"})):
        out = get_review_comments("tok", "o", "r", pull_number=1, review_id=2)
    assert out == []


def test_get_review_comments_page_cap_logs(caplog):
    """10 consecutive full pages hit the cap → warning fires (silent-
    truncation guard, #189 lesson) and exactly _MAX_REVIEW_COMMENT_PAGES
    requests are made."""
    import logging as _logging
    from github_reviews_client import get_review_comments, _MAX_REVIEW_COMMENT_PAGES
    full = [{"id": i, "path": "a.py", "line": 1} for i in range(100)]
    calls = []

    def _full(url, **kw):
        calls.append(kw["params"]["page"])
        return _list_response(full)

    with patch("httpx.get", side_effect=_full), caplog.at_level(_logging.WARNING):
        out = get_review_comments("tok", "o", "r", pull_number=1, review_id=2)
    assert len(calls) == _MAX_REVIEW_COMMENT_PAGES
    assert len(out) == 100 * _MAX_REVIEW_COMMENT_PAGES
    assert any(r.msg == "get_review_comments_page_cap_hit" for r in caplog.records)
