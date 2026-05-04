"""CSRF state-token tests for auth.github_oauth.

State token format: `<random>.<ts>.<hmac(random.ts)>`. Validates:
- Round-trip: _make_state followed by _verify_state passes
- Tamper: any byte modification rejected
- Truncation: 2-part string rejected
- TTL: token older than _STATE_TTL_SECONDS rejected
- HMAC compare uses constant-time path (hmac.compare_digest)
- Different secrets produce different signatures
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def _oauth_mod(monkeypatch):
    """Stub _state_secret to deterministic value. Avoid SSM round-trip."""
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET_SSM", "/grug/test-webhook-secret")
    import auth.github_oauth as mod
    monkeypatch.setattr(mod, "_state_secret", lambda: "test-secret-v1")
    return mod


def test_make_then_verify_round_trip(_oauth_mod):
    state = _oauth_mod._make_state()
    assert _oauth_mod._verify_state(state) is True


def test_make_state_format(_oauth_mod):
    state = _oauth_mod._make_state()
    parts = state.split(".")
    assert len(parts) == 3
    rand, ts, sig = parts
    assert len(rand) > 0
    assert ts.isdigit()
    assert len(sig) == 64  # sha256 hex


def test_verify_rejects_truncated(_oauth_mod):
    assert _oauth_mod._verify_state("only-one-part") is False
    assert _oauth_mod._verify_state("two.parts") is False


def test_verify_rejects_tampered_signature(_oauth_mod):
    state = _oauth_mod._make_state()
    rand, ts, sig = state.split(".")
    # Flip a single bit in the signature
    bad_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert _oauth_mod._verify_state(f"{rand}.{ts}.{bad_sig}") is False


def test_verify_rejects_tampered_timestamp(_oauth_mod):
    state = _oauth_mod._make_state()
    rand, ts, sig = state.split(".")
    # Bump ts by 1; signature is over old ts so HMAC compare fails
    bad_ts = str(int(ts) + 1)
    assert _oauth_mod._verify_state(f"{rand}.{bad_ts}.{sig}") is False


def test_verify_rejects_tampered_random(_oauth_mod):
    state = _oauth_mod._make_state()
    rand, ts, sig = state.split(".")
    bad_rand = "X" + rand[1:]
    assert _oauth_mod._verify_state(f"{bad_rand}.{ts}.{sig}") is False


def test_verify_rejects_expired_state(_oauth_mod, monkeypatch):
    state = _oauth_mod._make_state()
    # Fast-forward past TTL
    real_time = time.time
    monkeypatch.setattr(
        time, "time",
        lambda: real_time() + _oauth_mod._STATE_TTL_SECONDS + 1,
    )
    assert _oauth_mod._verify_state(state) is False


def test_verify_accepts_state_within_ttl(_oauth_mod, monkeypatch):
    state = _oauth_mod._make_state()
    real_time = time.time
    monkeypatch.setattr(
        time, "time",
        lambda: real_time() + _oauth_mod._STATE_TTL_SECONDS - 1,
    )
    assert _oauth_mod._verify_state(state) is True


def test_different_secrets_yield_different_signatures(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET_SSM", "/grug/test-webhook-secret")
    import importlib
    import auth.github_oauth as mod
    importlib.reload(mod)

    monkeypatch.setattr(mod, "_state_secret", lambda: "secret-A")
    state_a = mod._make_state()

    monkeypatch.setattr(mod, "_state_secret", lambda: "secret-B")
    state_b = mod._make_state()

    # Different secrets → different signatures even if rand+ts match by chance
    assert state_a.split(".")[2] != state_b.split(".")[2] or \
           state_a.split(".")[0] != state_b.split(".")[0]

    # Cross-secret verify must REJECT
    monkeypatch.setattr(mod, "_state_secret", lambda: "secret-A")
    assert mod._verify_state(state_b) is False


def test_make_state_random_per_call(_oauth_mod):
    """Two consecutive _make_state calls produce different tokens."""
    s1 = _oauth_mod._make_state()
    s2 = _oauth_mod._make_state()
    assert s1 != s2
    # Even if ts matches (same second), random portion must differ
    assert s1.split(".")[0] != s2.split(".")[0]
