"""Tests for github_checks_client.post_check_run.

Covers the request shape (URL, auth header, API version, body fields)
and 401-propagation. Mocks httpx.post — no real GH API calls.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import httpx
import pytest

from github_checks_client import CheckRunResult, post_check_run


def _ok_response(json_body=None):
    """Mimic httpx.Response.raise_for_status + .json()."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = 201
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body or {"id": 12345, "status": "completed"})
    return r


def test_post_check_run_url_and_auth():
    result = CheckRunResult(
        name="Grug — Definition of Ready",
        head_sha="abc123",
        status="completed",
        conclusion="success",
        title="all 5 pass",
        summary="...",
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        out = post_check_run("tok-123", "myorg", "myrepo", result)

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.github.com/repos/myorg/myrepo/check-runs"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert kwargs["timeout"] == 10
    assert out == {"id": 12345, "status": "completed"}


def test_post_check_run_body_with_conclusion():
    result = CheckRunResult(
        name="dor",
        head_sha="abc",
        status="completed",
        conclusion="failure",
        title="fail",
        summary="2 blocking",
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        post_check_run("tok", "o", "r", result)

    body = mock_post.call_args.kwargs["json"]
    assert body["name"] == "dor"
    assert body["head_sha"] == "abc"
    assert body["status"] == "completed"
    assert body["conclusion"] == "failure"
    assert body["output"]["title"] == "fail"
    assert body["output"]["summary"] == "2 blocking"
    assert "text" not in body["output"]  # not provided
    assert "external_id" not in body


def test_post_check_run_body_omits_conclusion_when_none():
    result = CheckRunResult(
        name="dor", head_sha="abc", status="in_progress",
        conclusion=None, title="running", summary="...",
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        post_check_run("tok", "o", "r", result)

    body = mock_post.call_args.kwargs["json"]
    assert "conclusion" not in body
    assert body["status"] == "in_progress"


def test_post_check_run_includes_text_when_provided():
    result = CheckRunResult(
        name="dor", head_sha="abc", status="completed",
        conclusion="success", title="ok", summary="x",
        text="full markdown report here",
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        post_check_run("tok", "o", "r", result)

    body = mock_post.call_args.kwargs["json"]
    assert body["output"]["text"] == "full markdown report here"


def test_post_check_run_includes_external_id_when_provided():
    result = CheckRunResult(
        name="dor", head_sha="abc", status="completed",
        conclusion="success", title="ok", summary="x",
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        post_check_run("tok", "o", "r", result, external_id="trace-id-7")

    body = mock_post.call_args.kwargs["json"]
    assert body["external_id"] == "trace-id-7"


def test_post_check_run_401_propagates_for_retry_helper():
    """post_check_run does NOT swallow 401 — the with_install_token_retry
    wrapper at the call site is responsible for invalidating the cache
    + retrying. Closes #50."""
    result = CheckRunResult(
        name="dor", head_sha="abc", status="completed",
        conclusion="success", title="x", summary="y",
    )

    def _raise_401():
        raise httpx.HTTPStatusError(
            "401",
            request=httpx.Request("POST", "https://api.github.com/..."),
            response=httpx.Response(401),
        )

    bad = _ok_response()
    bad.raise_for_status = _raise_401

    with patch("httpx.post", return_value=bad):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            post_check_run("stale-tok", "o", "r", result)
    assert exc.value.response.status_code == 401


def test_check_run_result_rejects_completed_without_conclusion():
    """type-design-analyzer: GitHub 422s status=completed + conclusion=None.
    Reject at construction instead."""
    with pytest.raises(ValueError, match="iff conclusion"):
        CheckRunResult(
            name="x", head_sha="abc", status="completed",
            conclusion=None, title="t", summary="s",
        )


def test_check_run_result_rejects_in_progress_with_conclusion():
    """Inverse: status=queued + conclusion=success is also a 422 from GH."""
    with pytest.raises(ValueError, match="iff conclusion"):
        CheckRunResult(
            name="x", head_sha="abc", status="queued",
            conclusion="success", title="t", summary="s",
        )


def test_post_check_run_500_propagates_unwrapped():
    result = CheckRunResult(
        name="dor", head_sha="abc", status="completed",
        conclusion="success", title="x", summary="y",
    )

    def _raise_500():
        raise httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "https://api.github.com/..."),
            response=httpx.Response(500),
        )

    bad = _ok_response()
    bad.raise_for_status = _raise_500

    with patch("httpx.post", return_value=bad):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            post_check_run("tok", "o", "r", result)
    assert exc.value.response.status_code == 500
