"""Coverage for the shared github_rest paginate-all helper.

Pins the cap / no-cap behaviour that used to be three diverged copies in
installations.py, plus the malformed-payload guard (never silently return
an empty list for an upstream breakage).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import github_rest


def _user() -> None:
    return None


def _ok_resp(json_body):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body)
    return r


def test_paginate_all_stops_on_short_page():
    """A short page (< per_page) ends the scan; accumulated items returned."""
    page1 = _ok_resp({"repositories": [{"id": i} for i in range(1, 101)]})
    page2 = _ok_resp({"repositories": [{"id": 101}, {"id": 102}]})

    with patch("github_rest.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.get.side_effect = [page1, page2]
        items = github_rest.paginate_all(
            "tok", "https://api.github.com/installation/repositories",
            item_key="repositories",
        )

    assert [i["id"] for i in items] == list(range(1, 103))
    assert client.get.call_count == 2


def test_paginate_all_caps_at_max_pages(caplog):
    """max_pages=10 stops after 1000 repos and logs the cap warning."""
    full_page = _ok_resp({"repositories": [{"id": i} for i in range(100)]})

    with patch("github_rest.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.get.return_value = full_page
        with caplog.at_level("WARNING"):
            items = github_rest.paginate_all(
                "tok", "https://api.github.com/installation/repositories",
                item_key="repositories", max_pages=10,
            )

    assert len(items) == 1000
    assert client.get.call_count == 10
    assert any("github_rest_pagination_cap" in r.getMessage() for r in caplog.records)


def test_paginate_all_uncapped_scans_all_pages():
    """max_pages=None scans every page with no ceiling."""
    pages = [
        _ok_resp({"repositories": [{"id": i} for i in range(1, 101)]}),
        _ok_resp({"repositories": [{"id": 101}]}),
    ]
    calls = [0]

    def _get(*args, **kwargs):
        resp = pages[calls[0]]
        calls[0] += 1
        return resp

    with patch("github_rest.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.get.side_effect = _get
        items = github_rest.paginate_all(
            "tok", "https://api.github.com/installation/repositories",
            item_key="repositories", max_pages=None,
        )

    assert [i["id"] for i in items] == list(range(1, 102))
    assert calls[0] == 2


def test_paginate_all_missing_item_key_raises():
    """Malformed page (missing the item key) raises instead of silently
    returning an empty list (silent-failure-hunter P1 #3)."""
    bad = _ok_resp({"unexpected": "shape"})
    with patch("github_rest.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.get.return_value = bad
        with pytest.raises(github_rest.GitHubMalformedPayload):
            github_rest.paginate_all(
                "tok", "https://api.github.com/installation/repositories",
                item_key="repositories",
            )


def test_paginate_all_list_body_when_item_key_none():
    """item_key=None means the body itself is a list (reviews / comments)."""
    page1 = _ok_resp([{"id": i} for i in range(1, 101)])  # full page -> continue
    page2 = _ok_resp([{"id": 101}])  # short page -> stop
    with patch("github_rest.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.get.side_effect = [page1, page2]
        items = github_rest.paginate_all(
            "tok", "https://api.github.com/x/reviews",
            item_key=None,
        )
    assert [i["id"] for i in items] == list(range(1, 102))
    assert client.get.call_count == 2


def test_github_headers_schemes():
    tok_headers = github_rest.github_headers("sekret")
    assert tok_headers["Authorization"] == "token sekret"
    bear_headers = github_rest.github_headers("sekret", bearer=True)
    assert bear_headers["Authorization"] == "Bearer sekret"
    for h in (tok_headers, bear_headers):
        assert h["Accept"] == "application/vnd.github+json"
        assert h["X-GitHub-Api-Version"] == "2022-11-28"


def test_github_request_propagates_http_errors():
    """raise_for_status failures propagate (caller owns 401 retry), never
    swallowed; rate-limit responses are logged."""
    import httpx

    req = httpx.Request("GET", "https://api.github.com/x")
    resp = httpx.Response(403, request=req, headers={"x-ratelimit-remaining": "0"})
    exc = httpx.HTTPStatusError("limited", request=req, response=resp)

    with patch("github_rest.httpx.request", side_effect=exc):
        with pytest.raises(httpx.HTTPStatusError):
            github_rest.github_request("GET", "https://api.github.com/x", "tok")
