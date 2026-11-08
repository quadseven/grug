"""grug#1631: no config read returns a token, key or secret value."""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient

    HAS_FASTAPI = True
except Exception:  # pragma: no cover
    HAS_FASTAPI = False

if HAS_FASTAPI:
    try:
        import uvicorn  # noqa: F401
    except Exception:  # pragma: no cover
        import types

        sys.modules["uvicorn"] = types.SimpleNamespace()
    from src.grugthink.api.redaction import REDACTED, redact_secrets
    from src.grugthink.api.server import create_app
    from src.grugthink.bot_manager import BotManager
    from src.grugthink.config.manager import ConfigManager

EXAMPLE = Path(__file__).resolve().parent.parent / "grugthink_config.yaml.example"

# Every secret-bearing value in the example config.
SECRET_VALUES = (
    "YOUR_DISCORD_BOT_TOKEN_HERE",
    "YOUR_SECOND_BOT_TOKEN_HERE",
    "YOUR_THIRD_BOT_TOKEN_HERE",
    "YOUR_GEMINI_API_KEY_HERE",
    "YOUR_GOOGLE_API_KEY_HERE",
    "YOUR_GOOGLE_CSE_ID_HERE",
    "YOUR_DISCORD_APPLICATION_CLIENT_SECRET",
    "your-random-session-secret-key",
)

pytestmark = pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "grugthink_config.yaml"
        shutil.copy(EXAMPLE, cfg)
        monkeypatch.setenv("GRUGTHINK_CONFIG_PATH", str(cfg))
        cm = ConfigManager()
        cm.set_env_var("DISABLE_OAUTH", "true")
        app = create_app(BotManager(config_manager=cm), cm)
        yield TestClient(app), cm


def test_example_config_carries_every_listed_secret(client):
    _, cm = client
    text = repr(cm.get_config())
    for secret in SECRET_VALUES:
        assert secret in text, secret


@pytest.mark.parametrize("path", ["/api/config", "/api/admin/settings"])
@pytest.mark.parametrize("secret", SECRET_VALUES)
def test_config_reads_never_return_a_secret(client, path, secret):
    http, _ = client
    r = http.get(path)
    assert r.status_code == 200
    assert secret not in r.text


def test_config_read_keeps_descriptive_fields_and_marks_secrets(client):
    http, _ = client
    data = http.get("/api/config").json()
    first = data["api_keys"]["discord"]["tokens"][0]
    assert first["token"] == REDACTED
    assert first["id"] == "1" and first["name"]
    assert data["api_keys"]["gemini"]["secondary"] == ""
    assert data["environment"]["DISCORD_CLIENT_SECRET"] == REDACTED
    assert data["environment"]["SESSION_SECRET"] == REDACTED
    assert data["environment"]["DISCORD_CLIENT_ID"] != REDACTED


def test_admin_settings_round_trip_keeps_the_stored_secret(client):
    http, cm = client
    settings = http.get("/api/admin/settings").json()
    assert settings["DISCORD_CLIENT_SECRET"] == REDACTED
    assert http.put("/api/admin/settings", json=settings).status_code == 200
    assert cm.get_env_var("DISCORD_CLIENT_SECRET") == "YOUR_DISCORD_APPLICATION_CLIENT_SECRET"
    assert cm.get_env_var("SESSION_SECRET") == "your-random-session-secret-key"


def test_unauthenticated_test_routes_are_gone(client):
    http, _ = client
    app_paths = {getattr(r, "path", None) for r in http.app.routes}
    assert "/simple-admin" not in app_paths
    # Any `/api/debug/routes` left is admin-gated (routers/system.py), never the bare one.
    debug = [r for r in http.app.routes if getattr(r, "path", None) == "/api/debug/routes"]
    assert all(r.dependant.dependencies for r in debug)


def test_redact_secrets_does_not_modify_its_input():
    src = {"api_keys": {"gemini": {"primary": "k"}}, "bots": {"b": {"discord_token_id": "1"}}}
    out = redact_secrets(src)
    assert src["api_keys"]["gemini"]["primary"] == "k"
    assert out["api_keys"]["gemini"]["primary"] == REDACTED
    assert out["bots"]["b"]["discord_token_id"] == "1"
