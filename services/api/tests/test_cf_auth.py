"""Tests for the CF→AWS auth-boundary middleware.

The middleware reads `GRUG_CF_SHARED_SECRET_SSM` env var at first use,
loads the SSM SecureString value (cached per warm container), and
validates the `X-Grug-CF-Secret` header on every non-`/livez` request.

Behaviors covered:
- /livez always passes (DD synthetics + smoke tests need un-authenticated access)
- Env var unset: fail-open (operator hasn't deployed Pulumi yet)
- SSM returns empty: fail-open (impossible-by-accident config; logged + permissive)
- SSM throws ParameterNotFound: fail-open (Worker/middleware deploy race)
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
def _reset_throttle_state():
    """`_last_unconfigured_log_at` is module-scope and would persist
    across tests, masking the throttled-log signal. Clear before each
    test so the WARN-on-first-event semantics are honored.
    """
    cf_auth._last_unconfigured_log_at.clear()


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


def test_unconfigured_env_var_fail_open() -> None:
    """Operator hasn't deployed Pulumi yet — env var absent, middleware
    must NOT block requests."""
    def raise_unconfigured():
        raise LookupError("GRUG_CF_SHARED_SECRET_SSM env var unset")

    app = _build_app(secret_loader=raise_unconfigured)
    client = TestClient(app)
    r = client.get("/protected", headers={"X-Grug-CF-Secret": "anything"})
    assert r.status_code == 200


def test_empty_ssm_value_fail_open() -> None:
    """SSM returned an empty string — impossible-by-accident, but if it
    happens the rollout property still holds (fail-open + log error)."""
    app = _build_app(secret_loader=lambda: "")
    client = TestClient(app)
    r = client.get("/protected")
    assert r.status_code == 200


def test_ssm_not_found_fail_open() -> None:
    """Lambda env var points at a SSM param that doesn't exist (Pulumi
    drift or rollout race). Fail-open — Workers' header injection still
    works in the meantime. Uses botocore ClientError to mimic the real
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
    assert r.status_code == 200


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
        from cf_auth import _default_secret_loader
        # First call hits SSM
        val = _default_secret_loader()
        assert val == "ssm-value-here"
        mock_get.assert_called_once_with(
            Name="/grug/cf-shared-secret", WithDecryption=True,
        )


def test_default_secret_loader_raises_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("GRUG_CF_SHARED_SECRET_SSM", raising=False)
    from cf_auth import _default_secret_loader, _ssm_cache_clear
    _ssm_cache_clear()  # purge any leakage from prior tests
    with pytest.raises(LookupError):
        _default_secret_loader()
