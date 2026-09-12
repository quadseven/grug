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


def test_retry_on_401_invalidates_and_refetches(_stub_token):
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        if len(calls) == 1:
            raise httpx.HTTPStatusError(
                "401",
                request=httpx.Request("GET", "https://api.github.com/repos"),
                response=httpx.Response(401),
            )
        return "ok"

    result = gh_auth.with_install_token_retry(123, fn)

    assert result == "ok"
    assert len(calls) == 2, "fn must be called twice (once + retry)"
    assert calls[0] == "token-1-refresh=False", "first call uses cached token"
    assert calls[1] == "token-2-refresh=True", \
        "retry must force_refresh — otherwise cache returns same bad token"


def test_permanent_4xx_propagates_without_retry(_stub_token):
    """A 404 is neither a stale-token 401 nor a transient 5xx/rate-limit
    (grug#946) - the identical request would fail identically forever, so
    retrying it wastes the same budget grug#770 exists to protect."""
    calls: list[str] = []

    def fn(token: str) -> None:
        calls.append(token)
        raise httpx.HTTPStatusError(
            "404",
            request=httpx.Request("GET", "https://api.github.com/repos"),
            response=httpx.Response(404),
        )

    with pytest.raises(httpx.HTTPStatusError) as ei:
        gh_auth.with_install_token_retry(123, fn)
    assert ei.value.response.status_code == 404
    assert len(calls) == 1, "a permanent 4xx must NOT retry"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch):
    """Retry tests below exercise real backoff math over several attempts -
    without this a run would actually sleep for several seconds."""
    monkeypatch.setattr(gh_auth.time, "sleep", lambda _seconds: None)


def test_5xx_retries_then_succeeds(_stub_token):
    """grug#946: the 2026-08-17 degradation aborted a review outright on
    the first 503 instead of riding out a transient blip."""
    calls: list[str] = []

    def fn(token: str) -> bool:
        calls.append(token)
        if len(calls) < 3:
            raise httpx.HTTPStatusError(
                "503",
                request=httpx.Request("GET", "https://api.github.com/repos"),
                response=httpx.Response(503),
            )
        return True

    assert gh_auth.with_install_token_retry(123, fn) is True
    assert len(calls) == 3, "two 503s then success: three attempts total"


def test_secondary_rate_limit_retries_then_succeeds(_stub_token):
    """A secondary rate limit is a 403/429 that is NOT the primary
    per-hour limit - GitHub's own docs say to back off and retry, not
    treat it as a permission denial."""
    calls: list[str] = []

    def fn(token: str) -> bool:
        calls.append(token)
        if len(calls) < 2:
            raise httpx.HTTPStatusError(
                "403",
                request=httpx.Request("GET", "https://api.github.com/repos"),
                response=httpx.Response(
                    403, json={"message": "You have exceeded a secondary rate limit"},
                ),
            )
        return True

    assert gh_auth.with_install_token_retry(123, fn) is True
    assert len(calls) == 2


def test_permanent_403_does_not_retry(_stub_token):
    """A plain permission-denied 403 (no Retry-After, no rate-limit
    wording) must NOT be mistaken for a secondary rate limit."""
    calls: list[str] = []

    def fn(token: str) -> None:
        calls.append(token)
        raise httpx.HTTPStatusError(
            "403",
            request=httpx.Request("GET", "https://api.github.com/repos"),
            response=httpx.Response(403, json={"message": "Resource not accessible"}),
        )

    with pytest.raises(httpx.HTTPStatusError):
        gh_auth.with_install_token_retry(123, fn)
    assert len(calls) == 1, "a genuine permission denial must NOT retry"


def test_retry_ceiling_stops_and_raises(_stub_token):
    """A sustained outage (every attempt 503) must still fail in bounded
    time rather than retry forever."""
    calls: list[str] = []

    def fn(token: str) -> None:
        calls.append(token)
        raise httpx.HTTPStatusError(
            "503",
            request=httpx.Request("GET", "https://api.github.com/repos"),
            response=httpx.Response(503),
        )

    with pytest.raises(httpx.HTTPStatusError) as ei:
        gh_auth.with_install_token_retry(123, fn)
    assert ei.value.response.status_code == 503
    assert len(calls) == gh_auth._RETRY_MAX_ATTEMPTS, (
        "must stop at the retry ceiling, not retry indefinitely"
    )


def test_success_first_try_does_not_refresh(_stub_token):
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        return "ok"

    assert gh_auth.with_install_token_retry(123, fn) == "ok"
    assert len(calls) == 1
    assert calls[0] == "token-1-refresh=False"
