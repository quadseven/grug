"""Bots router: request field names reach BotConfig on create and update (#1633).

The dashboard and API models name the per-bot overrides gemini_api_key,
ollama_urls and so on, while BotConfig stores them as override_*. These tests
drive the real router through FastAPI's TestClient and read the stored
BotConfig back, so a mismatch shows up as a 500 or as an unchanged field.
"""

import pytest
from fastapi.testclient import TestClient

from src.grugthink.api.server import create_app
from src.grugthink.bot_manager import BotManager
from src.grugthink.bot_manager.lifecycle import create_bot_environment
from src.grugthink.config.manager import ConfigManager


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("GRUGTHINK_CONFIG_PATH", str(tmp_path / "grugthink_config.yaml"))
    monkeypatch.setenv("GRUGBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DISABLE_OAUTH", "true")
    # No file watcher: a hot reload racing the test's own writes makes it flaky.
    monkeypatch.setattr("src.grugthink.config.watcher.create_observer_and_handler", lambda _cm: (None, None))
    monkeypatch.setattr("src.grugthink.config.watcher.start_watching", lambda _cm: None)
    monkeypatch.setattr("src.grugthink.config.watcher.stop_watching", lambda _cm: None)
    cm = ConfigManager()
    cm.set_env_var("DISABLE_OAUTH", "true")
    token_id = cm.add_discord_token("test", "discord-test-token")
    bm = BotManager(config_manager=cm)
    client = TestClient(create_app(bm, cm))
    yield client, bm, cm, token_id
    cm.stop()


def test_create_with_ollama_url_stores_overrides(api):
    client, bm, cm, token_id = api
    r = client.post(
        "/api/bots",
        json={
            "name": "ollama-bot",
            "template_id": "ollama_bot",
            "discord_token_id": token_id,
            "ollama_urls": "http://ollama.example.com:11434",
            "ollama_models": "llama3.2:3b",
            "gemini_api_key": "gem-key",
            "google_api_key": "g-key",
            "google_cse_id": "cse-id",
            "trusted_user_ids": "111,222",
            "custom_env": {"EXTRA_FLAG": "on"},
        },
    )
    assert r.status_code == 200, r.text
    bot_id = r.json()["bot_id"]

    config = bm.bots[bot_id].config
    assert config.override_ollama_urls == "http://ollama.example.com:11434"
    assert config.override_ollama_models == "llama3.2:3b"
    assert config.override_gemini_key == "gem-key"
    assert config.override_google_api_key == "g-key"
    assert config.override_google_cse_id == "cse-id"
    assert config.trusted_user_ids == "111,222"
    assert config.custom_env == {"EXTRA_FLAG": "on"}

    # The stored config persists and drives the environment the bot starts with.
    stored = cm.get_bot_config(bot_id)
    assert stored["override_ollama_urls"] == "http://ollama.example.com:11434"
    env = create_bot_environment(bm, config)
    assert env["OLLAMA_URLS"] == "http://ollama.example.com:11434"
    assert env["EXTRA_FLAG"] == "on"
    assert env["TRUSTED_USER_IDS"] == "111,222"


def test_create_with_unknown_template_is_400(api):
    client, _bm, _cm, token_id = api
    r = client.post(
        "/api/bots",
        json={"name": "x", "template_id": "no_such_template", "discord_token_id": token_id},
    )
    assert r.status_code == 400


def test_update_maps_dashboard_fields(api):
    client, bm, _cm, token_id = api
    bot_id = bm.create_bot(name="b", discord_token_id=token_id, template_id="ollama_bot")

    r = client.put(
        f"/api/bots/{bot_id}",
        json={
            "name": "renamed",
            "ollama_urls": "http://other.example.com:11434",
            "gemini_api_key": "new-gem",
            "trusted_user_ids": "333",
        },
    )
    assert r.status_code == 200, r.text

    config = bm.bots[bot_id].config
    assert config.name == "renamed"
    assert config.override_ollama_urls == "http://other.example.com:11434"
    assert config.override_gemini_key == "new-gem"
    assert config.trusted_user_ids == "333"


def test_update_with_unsupported_field_is_400_not_silent(api):
    client, bm, _cm, token_id = api
    bot_id = bm.create_bot(name="b", discord_token_id=token_id, template_id="ollama_bot")

    r = client.put(f"/api/bots/{bot_id}", json={"discord_token": "raw-token"})
    assert r.status_code == 400
    assert "discord_token" in r.json()["detail"]


def test_update_unknown_bot_is_404(api):
    client, _bm, _cm, _token_id = api
    r = client.put("/api/bots/does-not-exist", json={"name": "x"})
    assert r.status_code == 404


@pytest.mark.parametrize("path", ["/admin", "/admin.html"])
def test_admin_paths_redirect_to_the_dashboard(api, path):
    client, _bm, _cm, _token_id = api
    r = client.get(path, follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_dashboard_serves_the_modern_page(api):
    client, _bm, _cm, _token_id = api
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
