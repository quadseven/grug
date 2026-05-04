"""Regression test for #50 — `with_install_token_retry` must invalidate
the cache + re-fetch on httpx 401, then retry once.
"""

from __future__ import annotations

import httpx
import pytest

import github_app_auth as gh_auth


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    @property
    def text(self) -> str:
        return ""


@pytest.fixture(autouse=True)
def _stub_token(monkeypatch: pytest.MonkeyPatch):
    """Avoid SSM + JWT signing — return a sentinel token per call."""
    counter = {"n": 0}

    def fake_get(installation_id: int, *, force_refresh: bool = False) -> str:
        counter["n"] += 1
        return f"token-{counter['n']}-refresh={force_refresh}"

    monkeypatch.setattr(gh_auth, "get_install_token", fake_get)
    return counter


def test_retry_on_401_invalidates_and_refetches(_stub_token, mock_transport_client):
    """First call: 401 from real httpx machinery. Second call: 200.

    Closes mock-vs-real gap from async-blocker-hunter F-01 (issue #105) —
    direct construction of `httpx.HTTPStatusError(...)` keeps tests green
    even if production `except` clause narrows to a sub-class. With
    MockTransport, the exception comes from `resp.raise_for_status()`.
    """
    client = mock_transport_client(status_codes=[401, 200], json_bodies=[{}, {"ok": True}])
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        resp = client.get("https://api.github.com/repos")
        resp.raise_for_status()
        return resp.json()["ok"]

    result = gh_auth.with_install_token_retry(123, fn)

    assert result is True
    assert len(calls) == 2, "fn must be called twice (once + retry)"
    assert calls[0] == "token-1-refresh=False", "first call uses cached token"
    assert calls[1] == "token-2-refresh=True", \
        "retry must force_refresh — otherwise cache returns same bad token"


def test_non_401_status_propagates_without_retry(_stub_token, mock_transport_client):
    client = mock_transport_client(status_codes=[500])
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        resp = client.get("https://api.github.com/repos")
        resp.raise_for_status()
        return None

    with pytest.raises(httpx.HTTPStatusError) as ei:
        gh_auth.with_install_token_retry(123, fn)
    assert ei.value.response.status_code == 500
    assert len(calls) == 1, "non-401 must NOT retry"


def test_success_first_try_does_not_refresh(_stub_token):
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        return "ok"

    assert gh_auth.with_install_token_retry(123, fn) == "ok"
    assert len(calls) == 1
    assert calls[0] == "token-1-refresh=False"
