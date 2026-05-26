"""Tests for github_rulesets_client — create/list/delete + enforcement detection.

Covers request shape (URL, auth header, API version, body fields),
enforcement detection across Rulesets API + legacy branch protection,
and 401-propagation. Mocks httpx — no real GH API calls.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock, call

import httpx
import pytest

from github_rulesets_client import (
    EnforcementState,
    create_ruleset,
    delete_ruleset,
    list_rulesets,
    detect_enforcement,
    GRUG_RULESET_PREFIX,
)


# ── helpers ──────────────────────────────────────────────────────────

def _ok_response(json_body=None, status_code=200):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body if json_body is not None else {})
    return r


# ── create_ruleset ───────────────────────────────────────────────────

def test_create_ruleset_url_and_auth():
    with patch("httpx.post", return_value=_ok_response({"id": 99}, 201)) as mock_post:
        out = create_ruleset("tok-1", "myorg", "myrepo", "Grug — DoR", ["Grug — Definition of Ready"])

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.github.com/repos/myorg/myrepo/rulesets"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-1"
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert kwargs["timeout"] == 10
    assert out == {"id": 99}


def test_create_ruleset_body_shape():
    with patch("httpx.post", return_value=_ok_response({"id": 1}, 201)) as mock_post:
        create_ruleset("tok", "o", "r", "Grug — DoR", ["Grug — Definition of Ready"])

    body = mock_post.call_args.kwargs["json"]
    assert body["name"] == "Grug — DoR"
    assert body["target"] == "branch"
    assert body["enforcement"] == "active"
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert body["conditions"]["ref_name"]["exclude"] == []
    rules = body["rules"]
    assert len(rules) == 1
    assert rules[0]["type"] == "required_status_checks"
    checks = rules[0]["parameters"]["required_status_checks"]
    assert len(checks) == 1
    assert checks[0]["context"] == "Grug — Definition of Ready"


def test_create_ruleset_multiple_contexts():
    with patch("httpx.post", return_value=_ok_response({"id": 2}, 201)) as mock_post:
        create_ruleset("tok", "o", "r", "Grug — All Checks", ["check-a", "check-b"])

    body = mock_post.call_args.kwargs["json"]
    checks = body["rules"][0]["parameters"]["required_status_checks"]
    assert len(checks) == 2
    assert checks[0]["context"] == "check-a"
    assert checks[1]["context"] == "check-b"


def test_create_ruleset_401_propagates(mock_transport_client):
    client = mock_transport_client(status_codes=[401])
    with patch("httpx.post", side_effect=lambda *a, **kw: client.post(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            create_ruleset("stale", "o", "r", "Grug — DoR", ["ctx"])
    assert exc.value.response.status_code == 401


# ── delete_ruleset ───────────────────────────────────────────────────

def test_delete_ruleset_url_and_auth():
    with patch("httpx.delete", return_value=_ok_response(status_code=204)) as mock_del:
        delete_ruleset("tok-2", "myorg", "myrepo", 42)

    mock_del.assert_called_once()
    args, kwargs = mock_del.call_args
    assert args[0] == "https://api.github.com/repos/myorg/myrepo/rulesets/42"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-2"
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"


def test_delete_ruleset_401_propagates(mock_transport_client):
    client = mock_transport_client(status_codes=[401])
    with patch("httpx.delete", side_effect=lambda *a, **kw: client.delete(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            delete_ruleset("stale", "o", "r", 1)
    assert exc.value.response.status_code == 401


# ── list_rulesets ────────────────────────────────────────────────────

def test_list_rulesets_url_and_auth():
    rulesets = [{"id": 1, "name": "Grug — DoR"}, {"id": 2, "name": "CI Gate"}]
    with patch("httpx.get", return_value=_ok_response(rulesets)) as mock_get:
        out = list_rulesets("tok-3", "myorg", "myrepo")

    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert args[0] == "https://api.github.com/repos/myorg/myrepo/rulesets"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-3"
    assert out == rulesets


def test_list_rulesets_empty():
    with patch("httpx.get", return_value=_ok_response(json_body=[])):
        out = list_rulesets("tok", "o", "r")
    assert out == []


def test_list_rulesets_401_propagates(mock_transport_client):
    client = mock_transport_client(status_codes=[401])
    with patch("httpx.get", side_effect=lambda *a, **kw: client.get(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            list_rulesets("stale", "o", "r")
    assert exc.value.response.status_code == 401


# ── detect_enforcement ───────────────────────────────────────────────

def test_detect_grug_managed_via_rulesets():
    """Grug-prefixed ruleset with matching check → grug_managed."""
    rulesets = [
        {
            "id": 10,
            "name": "Grug — DoR Enforcement",
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": "Grug — Definition of Ready"},
                        ],
                    },
                },
            ],
        },
    ]
    rulesets_resp = _ok_response(rulesets)
    with patch("httpx.get", return_value=rulesets_resp):
        result = detect_enforcement("tok", "o", "r", "main", "Grug — Definition of Ready")

    assert result == "grug_managed"


def test_detect_external_via_rulesets():
    """Non-Grug ruleset enforcing the check → external."""
    rulesets = [
        {
            "id": 20,
            "name": "CI Required Checks",
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": "Grug — Definition of Ready"},
                        ],
                    },
                },
            ],
        },
    ]
    rulesets_resp = _ok_response(rulesets)
    with patch("httpx.get", return_value=rulesets_resp):
        result = detect_enforcement("tok", "o", "r", "main", "Grug — Definition of Ready")

    assert result == "external"


def test_detect_external_via_legacy_branch_protection():
    """No rulesets match, but legacy branch protection enforces the check → external."""
    rulesets_resp = _ok_response([])
    legacy_resp = _ok_response({"contexts": ["Grug — Definition of Ready", "ci/build"]})

    responses = [rulesets_resp, legacy_resp]
    with patch("httpx.get", side_effect=responses):
        result = detect_enforcement("tok", "o", "r", "main", "Grug — Definition of Ready")

    assert result == "external"


def test_detect_none_when_nothing_enforces():
    """No rulesets, no legacy protection → none."""
    rulesets_resp = _ok_response([])
    legacy_resp = _ok_response({"contexts": ["ci/build"]})

    responses = [rulesets_resp, legacy_resp]
    with patch("httpx.get", side_effect=responses):
        result = detect_enforcement("tok", "o", "r", "main", "Grug — Definition of Ready")

    assert result == "none"


def test_detect_none_when_legacy_404s():
    """No rulesets, legacy endpoint 404s (no branch protection at all) → none."""
    rulesets_resp = _ok_response([])
    legacy_404 = MagicMock(spec=httpx.Response)
    legacy_404.status_code = 404
    legacy_404.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("not found", request=MagicMock(), response=legacy_404)
    )

    responses = [rulesets_resp, legacy_404]
    with patch("httpx.get", side_effect=responses):
        result = detect_enforcement("tok", "o", "r", "main", "Grug — Definition of Ready")

    assert result == "none"


def test_detect_grug_managed_takes_priority_over_external():
    """If both a Grug-managed AND external ruleset match, grug_managed wins."""
    rulesets = [
        {
            "id": 10,
            "name": "Grug — DoR",
            "rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Grug — Definition of Ready"}]}}],
        },
        {
            "id": 20,
            "name": "CI Gate",
            "rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Grug — Definition of Ready"}]}}],
        },
    ]
    with patch("httpx.get", return_value=_ok_response(rulesets)):
        result = detect_enforcement("tok", "o", "r", "main", "Grug — Definition of Ready")
    assert result == "grug_managed"


def test_detect_skips_rulesets_without_status_check_rules():
    """Rulesets with no required_status_checks rules are ignored."""
    rulesets = [
        {
            "id": 30,
            "name": "Grug — Approvals",
            "rules": [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}],
        },
    ]
    rulesets_resp = _ok_response(rulesets)
    legacy_resp = _ok_response({"contexts": []})

    with patch("httpx.get", side_effect=[rulesets_resp, legacy_resp]):
        result = detect_enforcement("tok", "o", "r", "main", "Grug — Definition of Ready")
    assert result == "none"


def test_detect_non_401_error_propagates(mock_transport_client):
    """500 from rulesets API must propagate."""
    client = mock_transport_client(status_codes=[500])
    with patch("httpx.get", side_effect=lambda *a, **kw: client.get(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            detect_enforcement("tok", "o", "r", "main", "check")
    assert exc.value.response.status_code == 500


def test_grug_ruleset_prefix_value():
    assert GRUG_RULESET_PREFIX == "Grug — "


def test_detect_connect_error_propagates(mock_transport_client):
    """Transport-level ConnectError on rulesets API must propagate."""
    client = mock_transport_client(raise_exc=httpx.ConnectError("dns down"))
    with patch("httpx.get", side_effect=lambda *a, **kw: client.get(*a, **kw)):
        with pytest.raises(httpx.ConnectError):
            detect_enforcement("tok", "o", "r", "main", "check")


def test_detect_external_via_legacy_checks_array():
    """Legacy endpoint returns newer 'checks' array format instead of 'contexts'."""
    rulesets_resp = _ok_response([])
    legacy_resp = _ok_response({
        "contexts": [],
        "checks": [
            {"context": "Grug — Definition of Ready", "app_id": None},
        ],
    })

    with patch("httpx.get", side_effect=[rulesets_resp, legacy_resp]):
        result = detect_enforcement("tok", "o", "r", "main", "Grug — Definition of Ready")

    assert result == "external"


def test_detect_legacy_transport_error_returns_none():
    """Transport failure on legacy endpoint (after rulesets returned nothing)
    logs a warning and returns 'none' — does not crash (F-01 pattern)."""
    rulesets_resp = _ok_response([])
    call_count = {"n": 0}

    def side_effect(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return rulesets_resp
        raise httpx.ConnectError("legacy endpoint unreachable")

    with patch("httpx.get", side_effect=side_effect):
        result = detect_enforcement("tok", "o", "r", "main", "check")

    assert result == "none"


def test_detect_legacy_url_encodes_branch_with_slash():
    """Branch names like 'feat/foo' must be URL-encoded in the path segment."""
    rulesets_resp = _ok_response([])
    legacy_resp = _ok_response({"contexts": ["check-a"]})

    calls = []
    def capture_get(*a, **kw):
        calls.append(a[0])
        if len(calls) == 1:
            return rulesets_resp
        return legacy_resp

    with patch("httpx.get", side_effect=capture_get):
        detect_enforcement("tok", "o", "r", "feat/my-branch", "check-a")

    assert "feat%2Fmy-branch" in calls[1]


def test_detect_enforcement_401_propagates(mock_transport_client):
    """401 from rulesets list_rulesets must propagate so
    with_install_token_retry can invalidate + refresh."""
    client = mock_transport_client(status_codes=[401])
    with patch("httpx.get", side_effect=lambda *a, **kw: client.get(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            detect_enforcement("stale", "o", "r", "main", "check")
    assert exc.value.response.status_code == 401


def test_detect_legacy_non_404_error_propagates(mock_transport_client):
    """500 from the legacy branch protection endpoint must re-raise."""
    rulesets_resp = _ok_response([])
    legacy_client = mock_transport_client(status_codes=[500])
    call_count = {"n": 0}

    def side_effect(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return rulesets_resp
        return legacy_client.get(*a, **kw)

    with patch("httpx.get", side_effect=side_effect):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            detect_enforcement("tok", "o", "r", "main", "check")
    assert exc.value.response.status_code == 500
