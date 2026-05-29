"""Tests for secrets_loader (SSM SecureString fetch + cache).

Covers:
- get_webhook_secret reads from GITHUB_APP_WEBHOOK_SECRET_SSM env
- get_app_id reads from GITHUB_APP_ID_SSM env
- _get_ssm_secure_string raises on empty name (deploy-time misconfiguration)
- @lru_cache returns same value on repeat calls (warm-container reuse)
- Different names hit SSM independently
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import secrets_loader as sl


def _stub_ssm(values: dict[str, str]):
    """Build a fake _ssm.get_parameter that returns Value=values[name]."""
    def fake_get_parameter(*, Name, WithDecryption):
        if Name not in values:
            raise RuntimeError(f"ParameterNotFound: {Name}")
        return {"Parameter": {"Name": Name, "Value": values[Name]}}
    return fake_get_parameter


def _clear_cache():
    sl._get_ssm_secure_string.cache_clear()


def test_get_webhook_secret_reads_env_var(monkeypatch):
    _clear_cache()
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET_SSM", "/grug/test-webhook-secret")
    monkeypatch.setattr(
        sl._ssm, "get_parameter",
        _stub_ssm({"/grug/test-webhook-secret": "the-secret"}),
    )
    assert sl.get_webhook_secret() == "the-secret"


def test_get_app_id_reads_env_var(monkeypatch):
    _clear_cache()
    monkeypatch.setenv("GITHUB_APP_ID_SSM", "/grug/test-app-id")
    monkeypatch.setattr(
        sl._ssm, "get_parameter",
        _stub_ssm({"/grug/test-app-id": "12345"}),
    )
    assert sl.get_app_id() == "12345"


def test_empty_name_raises(monkeypatch):
    _clear_cache()
    with pytest.raises(RuntimeError, match="empty"):
        sl._get_ssm_secure_string("")


def test_lru_cache_avoids_second_ssm_call(monkeypatch):
    _clear_cache()
    call_count = {"n": 0}

    def counting_stub(*, Name, WithDecryption):
        call_count["n"] += 1
        return {"Parameter": {"Name": Name, "Value": "cached-val"}}

    monkeypatch.setattr(sl._ssm, "get_parameter", counting_stub)

    sl._get_ssm_secure_string("/grug/cached-secret")
    sl._get_ssm_secure_string("/grug/cached-secret")
    sl._get_ssm_secure_string("/grug/cached-secret")
    assert call_count["n"] == 1, "lru_cache must short-circuit repeat calls"


def test_different_names_hit_ssm_independently(monkeypatch):
    _clear_cache()
    call_count = {"n": 0}

    def counting_stub(*, Name, WithDecryption):
        call_count["n"] += 1
        return {"Parameter": {"Name": Name, "Value": f"val-{Name}"}}

    monkeypatch.setattr(sl._ssm, "get_parameter", counting_stub)

    sl._get_ssm_secure_string("/grug/a")
    sl._get_ssm_secure_string("/grug/b")
    assert call_count["n"] == 2


def test_get_openrouter_api_key_reads_env_var(monkeypatch):
    """Elder persona LLM client (#184) calls this; needs the same
    env-var-to-SSM shape as the GitHub App secrets."""
    _clear_cache()
    monkeypatch.setenv("GRUG_OPENROUTER_API_KEY_SSM", "/grug/openrouter-api-key")
    monkeypatch.setattr(
        sl._ssm, "get_parameter",
        _stub_ssm({"/grug/openrouter-api-key": "sk-or-test-value"}),
    )
    assert sl.get_openrouter_api_key() == "sk-or-test-value"


def test_with_decryption_always_true(monkeypatch):
    """SSM SecureString must always pass WithDecryption=True or the API
    returns the encrypted ciphertext string instead of the plaintext."""
    _clear_cache()
    captured = {}

    def capturing_stub(*, Name, WithDecryption):
        captured["WithDecryption"] = WithDecryption
        return {"Parameter": {"Name": Name, "Value": "x"}}

    monkeypatch.setattr(sl._ssm, "get_parameter", capturing_stub)
    sl._get_ssm_secure_string("/grug/anything")
    assert captured["WithDecryption"] is True
