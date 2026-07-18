"""Tests for github_app_auth — JWT signing + install-token exchange.

Coverage gap: with_install_token_retry was tested in
test_install_token_retry, but the underlying get_app_jwt + cache logic
in get_install_token had no direct coverage. Forging an RS256 token
needs a real RSA key — uses cryptography to mint one per test.
"""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def _rsa_pem() -> tuple[str, str]:
    """Mint a fresh RSA keypair per test (PEM strings)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test sees a fresh cache (module-scope state would leak)."""
    import github_app_auth as gh
    from ports.token_cache import InMemoryTokenCache
    original = gh._cache
    gh._cache = InMemoryTokenCache()
    yield
    gh._cache = original


@pytest.fixture
def _stub_secrets(monkeypatch, _rsa_pem):
    private_pem, public_pem = _rsa_pem
    import github_app_auth as gh
    monkeypatch.setattr(gh, "_app_id", lambda: "12345")
    monkeypatch.setattr(gh, "_app_private_key", lambda: private_pem)
    return private_pem, public_pem


def test_get_app_jwt_cache_miss_signs_rs256(_stub_secrets):
    """First call signs a fresh JWT with iat (60s back) + exp + iss claims."""
    import github_app_auth as gh
    private_pem, public_pem = _stub_secrets
    token = gh.get_app_jwt()
    decoded = jwt.decode(
        token, public_pem, algorithms=["RS256"],
        options={"verify_aud": False},
    )
    assert decoded["iss"] == "12345"
    now = int(time.time())
    # iat is 60s back (clock skew compensation per GH docs)
    assert decoded["iat"] <= now - 50
    assert decoded["iat"] >= now - 70
    # exp is ~9min ahead
    assert decoded["exp"] >= now + 8 * 60
    assert decoded["exp"] <= now + 10 * 60


def test_get_app_jwt_cache_hit_skips_resign(_stub_secrets, monkeypatch):
    """Second call within TTL returns cached value — does NOT re-sign."""
    import github_app_auth as gh
    t1 = gh.get_app_jwt()
    encode_called = []
    real_encode = jwt.encode

    def trap(*args, **kwargs):
        encode_called.append(1)
        return real_encode(*args, **kwargs)

    monkeypatch.setattr(jwt, "encode", trap)
    t2 = gh.get_app_jwt()
    assert t2 == t1
    assert encode_called == [], "should NOT re-sign within TTL window"


def test_get_install_token_cache_hit_skips_http(_stub_secrets):
    """Repeat call within 55min returns cached, no HTTP."""
    import github_app_auth as gh
    fake = MagicMock()
    fake.raise_for_status = MagicMock()
    fake.json = MagicMock(return_value={"token": "TOKEN-XYZ"})

    with patch("httpx.post", return_value=fake) as mock_post:
        t1 = gh.get_install_token(123)
        t2 = gh.get_install_token(123)

    assert t1 == t2 == "TOKEN-XYZ"
    assert mock_post.call_count == 1


def test_get_install_token_force_refresh_invalidates_then_refetches(_stub_secrets):
    """force_refresh=True drops the cached entry + hits HTTP again."""
    import github_app_auth as gh
    responses = []
    for tok in ("TOK-1", "TOK-2"):
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value={"token": tok})
        responses.append(r)

    call_idx = [0]

    def _post(*args, **kwargs):
        r = responses[call_idx[0]]
        call_idx[0] += 1
        return r

    with patch("httpx.post", side_effect=_post):
        t1 = gh.get_install_token(123)
        t2 = gh.get_install_token(123, force_refresh=True)

    assert t1 == "TOK-1"
    assert t2 == "TOK-2"


def test_get_install_token_url_uses_installation_id(_stub_secrets):
    import github_app_auth as gh
    fake = MagicMock()
    fake.raise_for_status = MagicMock()
    fake.json = MagicMock(return_value={"token": "x"})

    with patch("httpx.post", return_value=fake) as mock_post:
        gh.get_install_token(99999)

    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.github.com/app/installations/99999/access_tokens"


def test_get_install_token_uses_bearer_app_jwt(_stub_secrets):
    """Authorization header must be 'Bearer <App JWT>' — not 'token <...>'."""
    import github_app_auth as gh
    fake = MagicMock()
    fake.raise_for_status = MagicMock()
    fake.json = MagicMock(return_value={"token": "x"})

    with patch("httpx.post", return_value=fake) as mock_post:
        gh.get_install_token(1)

    auth = mock_post.call_args.kwargs["headers"]["Authorization"]
    assert auth.startswith("Bearer ")
    # Decode the JWT to confirm it's our App JWT (not the install token)
    import jwt as _jwt
    private_pem, public_pem = _stub_secrets
    decoded = _jwt.decode(
        auth.removeprefix("Bearer "), public_pem,
        algorithms=["RS256"], options={"verify_aud": False},
    )
    assert decoded["iss"] == "12345"


def test_get_install_token_propagates_401(_stub_secrets):
    """401 propagates so with_install_token_retry can catch + refresh."""
    import github_app_auth as gh
    bad = MagicMock()
    bad.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "401",
        request=httpx.Request("POST", "https://api.github.com/x"),
        response=httpx.Response(401),
    ))

    with patch("httpx.post", return_value=bad):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            gh.get_install_token(1)
    assert exc.value.response.status_code == 401


def test_get_install_token_missing_token_key_raises_typed_error(_stub_secrets, caplog):
    """A 200 whose body lacks `token` raises a clear typed error (not a bare
    KeyError) and emits a structured warning DD can alert on (#341)."""
    import logging

    import github_app_auth as gh
    fake = MagicMock()
    fake.raise_for_status = MagicMock()
    fake.json = MagicMock(return_value={"expires_at": "2026-01-01T00:00:00Z"})

    with patch("httpx.post", return_value=fake):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError) as exc:
                gh.get_install_token(4242)

    # Typed, descriptive, and does NOT leak a response body.
    assert "4242" in str(exc.value)
    assert not isinstance(exc.value, KeyError)
    assert "install_token_exchange_malformed_response" in caplog.text


def test_get_install_token_non_json_body_raises_typed_error(_stub_secrets, caplog):
    """A 200 with an unparseable body (`resp.json()` raises) is handled the same
    actionable way rather than surfacing as an opaque 500 (#341)."""
    import logging

    import github_app_auth as gh
    fake = MagicMock()
    fake.raise_for_status = MagicMock()
    fake.json = MagicMock(side_effect=ValueError("Expecting value"))

    with patch("httpx.post", return_value=fake):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError) as exc:
                gh.get_install_token(7)

    assert "7" in str(exc.value)
    assert "install_token_exchange_malformed_response" in caplog.text


def test_get_app_id_exposes_the_private_accessor(_stub_secrets):
    """#554 peer review round 3: Teller's marker-authorship check needs
    OUR OWN app ID outside this module - get_app_id() is the public
    call-through to the existing private _app_id()."""
    import github_app_auth as gh

    assert gh.get_app_id() == "12345"


def test_app_id_strips_whitespace_from_the_raw_ssm_value(monkeypatch):
    """Qodo review, PR #694: _get_ssm_secure_string does no normalization,
    unlike several other SSM reads in secrets_loader.py. A stray trailing
    newline in the SSM parameter would make every performed_via_github_
    app.id string comparison in _find_marker_comment (#554/#560/#561)
    fail forever, silently duplicating marker comments instead of
    matching our own."""
    import github_app_auth as gh

    monkeypatch.setenv("GITHUB_APP_ID_SSM", "/githumps/grug/app_id")
    monkeypatch.setattr(
        "secrets_loader._get_ssm_secure_string", lambda name: "12345\n"
    )
    assert gh.get_app_id() == "12345"
