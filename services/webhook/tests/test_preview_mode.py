"""#500: preview_mode is namespace-gated - it CANNOT engage in prod."""

from __future__ import annotations

from preview_mode import preview_mode


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("GRUG_PREVIEW", raising=False)
    monkeypatch.delenv("POD_NAMESPACE", raising=False)
    assert preview_mode() is False


def test_flag_alone_does_not_engage_in_prod(monkeypatch):
    """The load-bearing guard: GRUG_PREVIEW set in the PROD namespace must
    NOT turn on preview mode (else RA/SSM hardening silently disables)."""
    monkeypatch.setenv("GRUG_PREVIEW", "1")
    monkeypatch.setenv("POD_NAMESPACE", "grug")
    assert preview_mode() is False


def test_engages_only_in_preview_namespace(monkeypatch):
    monkeypatch.setenv("GRUG_PREVIEW", "1")
    monkeypatch.setenv("POD_NAMESPACE", "grug-pr-42")
    assert preview_mode() is True


def test_namespace_alone_does_not_engage(monkeypatch):
    monkeypatch.delenv("GRUG_PREVIEW", raising=False)
    monkeypatch.setenv("POD_NAMESPACE", "grug-pr-42")
    assert preview_mode() is False


def test_readiness_skips_ssm_in_preview(monkeypatch):
    monkeypatch.setenv("GRUG_PREVIEW", "1")
    monkeypatch.setenv("POD_NAMESPACE", "grug-pr-7")
    monkeypatch.setenv("GRUG_DATABASE_URL", "postgresql://x/y")
    import readiness
    calls = []
    monkeypatch.setattr(readiness, "_check_ssm_kms", lambda: calls.append("ssm"))
    monkeypatch.setattr(readiness, "_check_postgres", lambda: calls.append("pg"))
    readiness._reset_cache()
    readiness.check_readiness()
    assert "pg" in calls and "ssm" not in calls


def test_webhook_secret_from_env_in_preview(monkeypatch):
    monkeypatch.setenv("GRUG_PREVIEW", "1")
    monkeypatch.setenv("POD_NAMESPACE", "grug-pr-7")
    monkeypatch.setenv("GRUG_PREVIEW_WEBHOOK_SECRET", "fake-preview-secret")
    import secrets_loader
    assert secrets_loader.get_webhook_secret() == "fake-preview-secret"
