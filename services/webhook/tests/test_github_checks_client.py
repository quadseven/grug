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
        name="Grug - Chief",
        head_sha="abc123",
        status="completed",
        conclusion="success",
        title="all 5 pass",
        summary="...",
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        out = post_check_run("tok-123", "myorg", "myrepo", result)

    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["json"]["name"] == "Grug - Chief"

    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.github.com/repos/myorg/myrepo/check-runs"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert kwargs["timeout"] == 10
    assert out == {"id": 12345, "status": "completed"}


def test_post_check_run_never_dual_posts_a_legacy_alias():
    """The em-dash legacy-title mirror was retired: every githumps repo's
    required-check context is now the canonical ASCII title (infra#1829),
    so no ruleset anywhere still needs the alias. One POST per check-run,
    named exactly what the caller asked for."""
    result = CheckRunResult(
        name="Grug - Elder",
        head_sha="abc123",
        status="completed",
        conclusion="success",
        title="Elder done",
        summary="...",
    )
    with patch("httpx.post", return_value=_ok_response()) as mock_post:
        post_check_run("tok", "myorg", "myrepo", result)

    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["json"]["name"] == "Grug - Elder"


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


def test_post_check_run_401_propagates_for_retry_helper(mock_transport_client):
    """post_check_run does NOT swallow 401 — the with_install_token_retry
    wrapper at the call site is responsible for invalidating the cache
    + retrying. Closes #50.

    Real-transport-backed (issue #105): exception comes from
    `resp.raise_for_status()` on a real `httpx.Response(401)`, not from
    a hand-built `HTTPStatusError`.
    """
    result = CheckRunResult(
        name="dor", head_sha="abc", status="completed",
        conclusion="success", title="x", summary="y",
    )
    client = mock_transport_client(status_codes=[401])

    with patch("httpx.post", side_effect=lambda *a, **kw: client.post(*a, **kw)):
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


def test_post_check_run_500_propagates_unwrapped(mock_transport_client):
    """Real-transport-backed (issue #105) — 500 raised via raise_for_status."""
    result = CheckRunResult(
        name="dor", head_sha="abc", status="completed",
        conclusion="success", title="x", summary="y",
    )
    client = mock_transport_client(status_codes=[500])

    with patch("httpx.post", side_effect=lambda *a, **kw: client.post(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            post_check_run("tok", "o", "r", result)
    assert exc.value.response.status_code == 500


def test_post_check_run_connect_error_propagates(mock_transport_client):
    """Transport-level ConnectError must propagate (not get caught by an
    httpx.HTTPStatusError-only handler). Closes mock-vs-real gap from
    issue #105 / async-blocker-hunter F-01.
    """
    result = CheckRunResult(
        name="dor", head_sha="abc", status="completed",
        conclusion="success", title="x", summary="y",
    )
    client = mock_transport_client(raise_exc=httpx.ConnectError("dns down"))

    with patch("httpx.post", side_effect=lambda *a, **kw: client.post(*a, **kw)):
        with pytest.raises(httpx.ConnectError):
            post_check_run("tok", "o", "r", result)


def test_post_check_run_truncates_oversize_summary():
    """#553 audit stage 8: a >65535-char summary 422s and vanishes the
    whole check-run - the client truncates visibly at the choke point."""
    import httpx
    from unittest.mock import MagicMock, patch

    from github_checks_client import CheckRunResult, post_check_run

    captured = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured["body"] = kwargs["json"]
        r = MagicMock(spec=httpx.Response)
        r.status_code = 201
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value={"id": 1})
        return r

    # multi-byte content: a byte-length truncation bug (vs char-length)
    # would only surface with non-ASCII input.
    result = CheckRunResult(
        name="Grug - Elder", head_sha="a" * 40, status="completed",
        conclusion="neutral", title="t", summary="日" * 70000,
    )
    with patch("httpx.post", side_effect=fake_post):
        post_check_run("tok", "o", "r", result)
    sent = captured["body"]["output"]["summary"]
    assert len(sent) <= 65100
    assert sent.endswith("(summary truncated)")
