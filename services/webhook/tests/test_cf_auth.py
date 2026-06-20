"""Tests for the CF→AWS auth-boundary middleware.

The middleware reads `GRUG_CF_SHARED_SECRET_SSM` env var at first use,
loads the SSM SecureString value (cached per warm container), and
validates the `X-Grug-CF-Secret` header on every non-`/livez` request.

Behaviors covered:
- /livez always passes (DD synthetics + smoke tests need un-authenticated access)
- Env var unset: fail-CLOSED 503 by default (audit #4); fail-open only
  when GRUG_CF_AUTH_FAIL_OPEN is set (initial bring-up)
- SSM returns empty: fail-CLOSED 503 (fail-open under the bring-up flag)
- SSM throws ParameterNotFound: fail-CLOSED 503 (fail-open under the flag)
- Strict mode + missing header: 401
- Strict mode + mismatched header: 401
- Strict mode + matching header (constant-time compare): pass-through
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import cf_auth
from cf_auth import CfAuthMiddleware


@pytest.fixture(autouse=True)
def _reset_throttle_state(monkeypatch):
    """`_last_unconfigured_log_at` is module-scope and would persist
    across tests, masking the throttled-log signal. Clear before each
    test so the WARN-on-first-event semantics are honored.

    Also force fail-CLOSED by default (delete the bring-up escape-hatch
    flag) so a stray GRUG_CF_AUTH_FAIL_OPEN in the dev shell can't mask
    the default-deny contract; the fail-open tests opt back in explicitly.
    """
    cf_auth._last_unconfigured_log_at.clear()
    monkeypatch.delenv("GRUG_CF_AUTH_FAIL_OPEN", raising=False)


def _build_app(*, secret_loader=None):
    """Build a minimal FastAPI app with the middleware installed.

    The `secret_loader` arg is a zero-arg callable returning the SSM
    secret value; passing it directly bypasses both the env-var lookup
    AND the SSM round-trip, which keeps tests hermetic.
    """
    app = FastAPI()
    if secret_loader is None:
        app.add_middleware(CfAuthMiddleware)
    else:
        app.add_middleware(CfAuthMiddleware, secret_loader=secret_loader)

    @app.get("/readyz")
    def readyz():
        return {"status": "ready"}

    @app.get("/livez")
    def livez():
        return {"status": "ok"}

    @app.get("/protected")
    def protected():
        return {"status": "reached"}

    return app


def test_livez_always_bypasses_check_when_strict() -> None:
    """DD synthetic uptime + smoke tests must reach /livez without the header."""
    app = _build_app(secret_loader=lambda: "real-secret")
    client = TestClient(app)
    r = client.get("/livez")
    assert r.status_code == 200


def test_readyz_always_bypasses_check_when_strict() -> None:
    """DD synthetic uptime + smoke tests must reach /readyz without the header."""
    app = _build_app(secret_loader=lambda: "real-secret")
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 200


def test_livez_bypasses_even_when_strict_and_header_mismatch() -> None:
    """A bogus header on /livez still 200s — exempt is exempt."""
    app = _build_app(secret_loader=lambda: "real-secret")
    client = TestClient(app)
    r = client.get("/livez", headers={"X-Grug-CF-Secret": "bogus"})
    assert r.status_code == 200


def test_strict_missing_header_returns_401() -> None:
    app = _build_app(secret_loader=lambda: "real-secret")
    client = TestClient(app)
    r = client.get("/protected")
    assert r.status_code == 401


def test_strict_mismatched_header_returns_401() -> None:
    app = _build_app(secret_loader=lambda: "real-secret")
    client = TestClient(app)
    r = client.get("/protected", headers={"X-Grug-CF-Secret": "wrong-value"})
    assert r.status_code == 401


def test_strict_matching_header_passes_through() -> None:
    app = _build_app(secret_loader=lambda: "real-secret")
    client = TestClient(app)
    r = client.get("/protected", headers={"X-Grug-CF-Secret": "real-secret"})
    assert r.status_code == 200
    assert r.json() == {"status": "reached"}


def test_unconfigured_env_var_fail_closed() -> None:
    """Default (audit #4): env var absent -> deny 503 rather than silently
    disable the origin-auth boundary."""
    def raise_unconfigured():
        raise LookupError("GRUG_CF_SHARED_SECRET_SSM env var unset")

    app = _build_app(secret_loader=raise_unconfigured)
    client = TestClient(app)
    r = client.get("/protected", headers={"X-Grug-CF-Secret": "anything"})
    assert r.status_code == 503


def test_unconfigured_env_var_fail_open_when_flag_set(monkeypatch) -> None:
    """Bring-up escape hatch: GRUG_CF_AUTH_FAIL_OPEN=1 restores fail-open
    so the first Pulumi->Worker->service rollout isn't 503'd."""
    monkeypatch.setenv("GRUG_CF_AUTH_FAIL_OPEN", "1")

    def raise_unconfigured():
        raise LookupError("GRUG_CF_SHARED_SECRET_SSM env var unset")

    app = _build_app(secret_loader=raise_unconfigured)
    client = TestClient(app)
    r = client.get("/protected", headers={"X-Grug-CF-Secret": "anything"})
    assert r.status_code == 200


def test_empty_ssm_value_fail_closed() -> None:
    """SSM returned an empty string — deny 503 by default (audit #4)."""
    app = _build_app(secret_loader=lambda: "")
    client = TestClient(app)
    r = client.get("/protected")
    assert r.status_code == 503


def test_empty_ssm_value_fail_open_when_flag_set(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CF_AUTH_FAIL_OPEN", "true")
    app = _build_app(secret_loader=lambda: "")
    client = TestClient(app)
    r = client.get("/protected")
    assert r.status_code == 200


def test_ssm_not_found_fail_closed() -> None:
    """SSM param doesn't exist (Pulumi drift / rollout race). Default
    deny 503 (audit #4). Uses botocore ClientError to mimic the real
    boto3 ParameterNotFound shape."""
    from botocore.exceptions import ClientError

    def raise_not_found():
        raise ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameter",
        )

    app = _build_app(secret_loader=raise_not_found)
    client = TestClient(app)
    r = client.get("/protected")
    assert r.status_code == 503


def test_programmer_bug_propagates_as_500() -> None:
    """`except Exception` would have masked an `AttributeError` from a
    typo refactor — auth boundary silently disables. Verify that bug
    classes outside the fail-open whitelist surface as a 500 so the
    operator sees them in DD instead of being quietly bypassed.
    """
    def raise_attribute_error():
        raise AttributeError("ssm has no attribute 'get_pramaeter'")

    app = _build_app(secret_loader=raise_attribute_error)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/protected")
    assert r.status_code == 500


def test_livez_with_trailing_slash_bypasses() -> None:
    """`/livez/` (trailing slash) must also be exempt — FastAPI's
    auto-redirect would otherwise interact badly with the strict
    middleware path check."""
    app = _build_app(secret_loader=lambda: "real-secret")
    client = TestClient(app, follow_redirects=False)
    r = client.get("/livez/")
    # The route handler will 307 to /livez, but the middleware must
    # have already let the request through to reach that redirect.
    assert r.status_code in (200, 307)


def test_livez_case_insensitive_bypass() -> None:
    """`/LIVEZ` (mixed case) must bypass — guards against a synthetic
    or curl typo turning into auth-boundary noise."""
    app = _build_app(secret_loader=lambda: "real-secret")
    client = TestClient(app, follow_redirects=False)
    # The handler is registered at /livez exact, so this will 404,
    # but the middleware must not 401 it first.
    r = client.get("/LIVEZ")
    assert r.status_code in (200, 404)


def test_compare_is_constant_time() -> None:
    """Cannot directly test wall-clock timing in pytest, but assert the
    middleware uses `hmac.compare_digest` by patching it and verifying
    the call. Regression guard against a future refactor swapping in `==`.
    """
    import hmac as _hmac
    spy_calls: list = []
    original = _hmac.compare_digest

    def spy(a, b):
        spy_calls.append((a, b))
        return original(a, b)

    with patch("cf_auth.hmac.compare_digest", side_effect=spy) as patched:
        app = _build_app(secret_loader=lambda: "real-secret")
        client = TestClient(app)
        r = client.get("/protected", headers={"X-Grug-CF-Secret": "wrong"})
        assert r.status_code == 401
    assert patched.called, "middleware did not use hmac.compare_digest"


def test_default_secret_loader_reads_env_then_ssm(monkeypatch) -> None:
    """Default loader: env var -> SSM lookup. Verified by patching the
    boto3 ssm client at the loader level."""
    monkeypatch.setenv("GRUG_CF_SHARED_SECRET_SSM", "/grug/cf-shared-secret")
    fake_response = {"Parameter": {"Value": "ssm-value-here"}}

    with patch("cf_auth._ssm.get_parameter", return_value=fake_response) as mock_get:
        from cf_auth import _default_secret_loader, _ssm_cache_clear
        _ssm_cache_clear()
        # First call hits SSM
        val = _default_secret_loader()
        assert val == "ssm-value-here"
        mock_get.assert_called_once_with(
            Name="/grug/cf-shared-secret", WithDecryption=True,
        )


def test_default_secret_loader_caches_warm_path(monkeypatch) -> None:
    """lru_cache must memoize the successful SSM read — guards against
    a refactor that drops the @lru_cache decorator or breaks
    memoization, which would silently re-hit SSM on every request and
    blow through the SSM quota at production concurrency.
    """
    monkeypatch.setenv("GRUG_CF_SHARED_SECRET_SSM", "/grug/cf-shared-secret")
    fake_response = {"Parameter": {"Value": "ssm-value-here"}}

    with patch("cf_auth._ssm.get_parameter", return_value=fake_response) as mock_get:
        from cf_auth import _default_secret_loader, _ssm_cache_clear
        _ssm_cache_clear()
        v1 = _default_secret_loader()
        v2 = _default_secret_loader()
        v3 = _default_secret_loader()

    assert v1 == v2 == v3 == "ssm-value-here"
    assert mock_get.call_count == 1, (
        f"lru_cache regressed — SSM hit {mock_get.call_count} times"
    )


def test_header_is_case_insensitive() -> None:
    """Starlette Headers is case-insensitive per HTTP spec, but CF
    Workers emit lowercase headers under HTTP/2. If a future refactor
    swaps `request.headers.get(...)` for `request.scope['headers']`
    iteration (a common perf optimization), case sensitivity silently
    regresses and every CF→Lambda request 401s.
    """
    app = _build_app(secret_loader=lambda: "real-secret")
    client = TestClient(app)

    for header_name in ("X-Grug-CF-Secret", "x-grug-cf-secret", "X-GRUG-CF-SECRET"):
        r = client.get("/protected", headers={header_name: "real-secret"})
        assert r.status_code == 200, f"case '{header_name}' failed"


def test_default_secret_loader_raises_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("GRUG_CF_SHARED_SECRET_SSM", raising=False)
    from cf_auth import _default_secret_loader, _ssm_cache_clear
    _ssm_cache_clear()  # purge any leakage from prior tests
    with pytest.raises(LookupError):
        _default_secret_loader()


def test_unconfigured_warning_throttled_within_window() -> None:
    """Load-bearing property of the throttle: two back-to-back fail-open
    requests must emit ONE WARN log, not two. Without this guard a
    refactor that swaps `time.monotonic()` for `time.time()` or flips
    the sign comparison would silently regress to per-request flooding
    — the exact bug the throttle was introduced to prevent.
    """
    # Module-global throttle state: clear it so this test is
    # order-independent (a fail-open warning from ANY earlier test
    # within the 60s window otherwise suppresses ours - surfaced as a
    # branch-dependent flake on PR #358).
    cf_auth._last_unconfigured_log_at.clear()
    def raise_unconfigured():
        raise LookupError("env var unset")

    app = _build_app(secret_loader=raise_unconfigured)
    client = TestClient(app)

    # Patch the WHOLE module logger, not `.warning` on it: under ddtrace
    # log-injection the logger's bound methods can be rebound, so patching the
    # attribute can race on slower runners (the #359 suspect). Swapping the
    # whole `cf_auth.log` object is immune to that.
    with patch.object(cf_auth, "log") as mock_log:
        # First request: should log. (Default fail-CLOSED -> 503.)
        r1 = client.get("/protected")
        assert r1.status_code == 503
        # Second request immediately after: throttle suppresses the log.
        r2 = client.get("/protected")
        assert r2.status_code == 503

    assert mock_log.warning.call_count == 1, (
        f"throttle did not suppress duplicate log: {mock_log.warning.call_count} calls"
    )


def test_throttle_distinguishes_reasons() -> None:
    """Different fail-open reasons throttle independently — an
    unconfigured-env-var WARN and an empty-ssm-value WARN are separate
    signals, each deserving its own first log.
    """
    # Module-global throttle state: clear it so this test is
    # order-independent (a fail-open warning from ANY earlier test
    # within the 60s window otherwise suppresses ours - surfaced as a
    # branch-dependent flake on PR #358).
    cf_auth._last_unconfigured_log_at.clear()
    # Custom loader: first call raises LookupError, second returns "".
    state = {"calls": 0}

    def loader_two_reasons():
        state["calls"] += 1
        if state["calls"] == 1:
            raise LookupError("env var unset")
        return ""

    app = _build_app(secret_loader=loader_two_reasons)
    client = TestClient(app)

    # Patch the WHOLE module logger, not `.warning` on it: under ddtrace
    # log-injection the logger's bound methods can be rebound, so patching the
    # attribute can race on slower runners (the #359 suspect). Swapping the
    # whole `cf_auth.log` object is immune to that.
    with patch.object(cf_auth, "log") as mock_log:
        r1 = client.get("/protected")
        r2 = client.get("/protected")
        # Default fail-CLOSED -> 503 for both reasons.
        assert r1.status_code == 503
        assert r2.status_code == 503

    # Two distinct reasons -> two distinct first-logs.
    assert mock_log.warning.call_count == 2
