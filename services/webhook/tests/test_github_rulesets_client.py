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

def _ok_response(json_body=None, status_code=200, headers=None):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body if json_body is not None else {})
    # Real httpx responses always carry headers; the resilient GET inspects
    # them (Retry-After / X-RateLimit-Remaining) on rate-limit statuses, so the
    # mock must too — `spec=httpx.Response` omits `headers` (an instance attr).
    r.headers = headers or {}
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
    # integration_id must be OMITTED, not null: GitHub's ruleset schema 422s
    # on `integration_id: null` ("data matches no possible input"), which
    # broke every enforcement "fix".
    assert "integration_id" not in checks[0]


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


def test_detect_grug_managed_via_stored_id_despite_mismatched_name():
    """A ruleset enforcing the check, matching stored_ruleset_id but NOT the
    Grug — name prefix (e.g. an operator or migration renamed it), is still
    grug_managed. Regression for a live repo-rename incident: a ruleset
    named "Grug TPM gate" (no em-dash, doesn't match GRUG_RULESET_PREFIX)
    was misclassified as external/none purely on the name heuristic even
    though Grug created and tracks it (infra#943 rename investigation).
    """
    rulesets = [
        {
            "id": 999,
            "name": "Grug TPM gate",
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
        result = detect_enforcement(
            "tok", "o", "r", "main", "Grug — Definition of Ready",
            stored_ruleset_id=999,
        )

    assert result == "grug_managed"


def test_detect_external_when_stored_id_does_not_match_any_ruleset():
    """stored_ruleset_id set, but no live ruleset has that ID → falls back
    to the name-prefix heuristic (here: non-Grug name → external)."""
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
        result = detect_enforcement(
            "tok", "o", "r", "main", "Grug — Definition of Ready",
            stored_ruleset_id=12345,
        )

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


def test_detect_none_when_legacy_403s():
    """No rulesets, legacy endpoint 403s (insufficient perms) → none, not crash.
    Peer-review finding: GitHub returns 403 when App lacks administration:read."""
    rulesets_resp = _ok_response([])
    legacy_403 = MagicMock(spec=httpx.Response)
    legacy_403.status_code = 403
    legacy_403.headers = {}  # permission 403, not rate-limited → no retry
    legacy_403.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("forbidden", request=MagicMock(), response=legacy_403)
    )

    responses = [rulesets_resp, legacy_403]
    with patch("httpx.get", side_effect=responses):
        result = detect_enforcement("tok", "o", "r", "main", "Grug — Definition of Ready")

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


# ── resilient GET: retry / jitter / fallback (dashboard 429 storm) ────
import github_rulesets_client as grc  # noqa: E402 — module handle for internals


def _resp(status, *, headers=None, json_body=None):
    """Real httpx.Response (request attached so raise_for_status works)."""
    req = httpx.Request("GET", "https://api.github.com/x")
    return httpx.Response(
        status, request=req, headers=headers or {},
        json=json_body if json_body is not None else [],
    )


def test_list_rulesets_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(grc, "_RETRY_SLEEP", lambda s: None)
    seq = [_resp(429, headers={"retry-after": "1"}), _resp(200, json_body=[{"name": "x"}])]
    calls = {"n": 0}

    def fake(*a, **kw):
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    with patch("httpx.get", side_effect=fake):
        out = list_rulesets("tok", "o", "r")
    assert out == [{"name": "x"}]
    assert calls["n"] == 2  # retried once, then succeeded


def test_list_rulesets_exhausts_then_raises(monkeypatch):
    slept = []
    monkeypatch.setattr(grc, "_RETRY_SLEEP", lambda s: slept.append(s))
    with patch("httpx.get", return_value=_resp(429)) as mock_get:
        with pytest.raises(httpx.HTTPStatusError) as exc:
            list_rulesets("tok", "o", "r")
    assert exc.value.response.status_code == 429
    assert mock_get.call_count == grc._GET_RETRY_ATTEMPTS       # all attempts used
    assert len(slept) == grc._GET_RETRY_ATTEMPTS - 1            # slept between, not after last


def test_permission_403_not_retried(monkeypatch):
    """A bare 403 (permission) is NOT a rate-limit → must not waste retries."""
    monkeypatch.setattr(grc, "_RETRY_SLEEP", lambda s: None)
    with patch("httpx.get", return_value=_resp(403)) as mock_get:
        with pytest.raises(httpx.HTTPStatusError):
            list_rulesets("tok", "o", "r")
    assert mock_get.call_count == 1


def test_ratelimit_403_is_retried(monkeypatch):
    """A 403 carrying a rate-limit signal (X-RateLimit-Remaining: 0) IS retried."""
    monkeypatch.setattr(grc, "_RETRY_SLEEP", lambda s: None)
    seq = [_resp(403, headers={"x-ratelimit-remaining": "0"}), _resp(200, json_body=[])]
    calls = {"n": 0}

    def fake(*a, **kw):
        r = seq[min(calls["n"], 1)]
        calls["n"] += 1
        return r

    with patch("httpx.get", side_effect=fake):
        out = list_rulesets("tok", "o", "r")
    assert out == []
    assert calls["n"] == 2


def test_transport_error_retried_then_raises(monkeypatch):
    monkeypatch.setattr(grc, "_RETRY_SLEEP", lambda s: None)
    with patch("httpx.get", side_effect=httpx.ConnectError("boom")) as mock_get:
        with pytest.raises(httpx.ConnectError):
            list_rulesets("tok", "o", "r")
    assert mock_get.call_count == grc._GET_RETRY_ATTEMPTS


def test_retry_delay_bounded_and_caps_retry_after():
    # No Retry-After header: equal-jittered backoff stays within [0, cap].
    for attempt in range(4):
        assert 0 <= grc._retry_delay(attempt, None) <= grc._GET_RETRY_MAX_DELAY
    # A huge Retry-After is capped — never block a user request that long.
    big = _resp(429, headers={"retry-after": "600"})
    assert grc._retry_delay(0, big) <= grc._GET_RETRY_MAX_DELAY


# --- #460: list_installation_repos -------------------------------------------


def test_list_installation_repos_single_page_shape_and_auth():
    from github_rulesets_client import list_installation_repos

    body = {"total_count": 2, "repositories": [
        {"id": 10, "full_name": "o/a", "default_branch": "trunk"},
        {"id": 11, "full_name": "o/b", "default_branch": None},
    ]}
    with patch("httpx.get", return_value=_ok_response(body)) as mock_get:
        out = list_installation_repos("tok")
    assert out == [
        {"id": 10, "full_name": "o/a", "default_branch": "trunk"},
        {"id": 11, "full_name": "o/b", "default_branch": "main"},  # None -> main
    ]
    assert mock_get.call_count == 1  # <100 repos on page 1 stops pagination
    url = mock_get.call_args[0][0]
    assert url == "https://api.github.com/installation/repositories?per_page=100&page=1"
    headers = mock_get.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer tok"


def test_list_installation_repos_paginates_until_short_page():
    from github_rulesets_client import list_installation_repos

    full_page = {"repositories": [
        {"id": i, "full_name": f"o/r{i}", "default_branch": "main"}
        for i in range(100)
    ]}
    short_page = {"repositories": [
        {"id": 200, "full_name": "o/last", "default_branch": "main"},
    ]}
    with patch(
        "httpx.get",
        side_effect=[_ok_response(full_page), _ok_response(short_page)],
    ) as mock_get:
        out = list_installation_repos("tok")
    assert len(out) == 101
    assert mock_get.call_count == 2
    assert "page=2" in mock_get.call_args[0][0]


def test_list_installation_repos_error_propagates(mock_transport_client):
    from github_rulesets_client import list_installation_repos

    client = mock_transport_client(status_codes=[401])
    with patch("httpx.get", side_effect=lambda *a, **kw: client.get(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError):
            list_installation_repos("bad-tok")
