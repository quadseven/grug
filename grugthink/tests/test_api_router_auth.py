"""Auth-gated success and failure for the memories, config and personalities routers (#1691).

OAuth stays enabled here. The session user is injected through the real
get_current_user dependency, so admin_required and memory_manager_required run
unmodified: no session is 401, an untrusted user is 403, a trusted user succeeds.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.grugthink.api.dependencies import get_current_user
from src.grugthink.api.server import create_app
from src.grugthink.bot_manager import BotManager
from src.grugthink.config.manager import ConfigManager

ADMIN = {"id": "admin-1", "username": "admin"}
MEMORY_USER = {"id": "mem-1", "username": "memory"}
STRANGER = {"id": "nobody-9", "username": "stranger"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("GRUGTHINK_CONFIG_PATH", str(tmp_path / "grugthink_config.yaml"))
    monkeypatch.setenv("GRUGBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.delenv("DISABLE_OAUTH", raising=False)
    monkeypatch.setattr("src.grugthink.config.watcher.create_observer_and_handler", lambda _cm: (None, None))
    monkeypatch.setattr("src.grugthink.config.watcher.start_watching", lambda _cm: None)
    monkeypatch.setattr("src.grugthink.config.watcher.stop_watching", lambda _cm: None)
    cm = ConfigManager()
    cm.set_env_var("DISABLE_OAUTH", "false")
    cm.set_env_var("TRUSTED_USER_IDS", ADMIN["id"])
    cm.set_env_var("TRUSTED_MEMORY_IDS", MEMORY_USER["id"])
    bm = BotManager(config_manager=cm)
    app = create_app(bm, cm)
    client = TestClient(app)

    def login(user):
        app.dependency_overrides[get_current_user] = lambda: user

    yield SimpleNamespace(client=client, bm=bm, cm=cm, login=login)
    cm.stop()


def _bot_with_memory_db(api):
    token_id = api.cm.add_discord_token("t", "discord-test-token")
    bot_id = api.bm.create_bot("memory-bot", token_id)
    server_db = MagicMock()
    server_db.add_fact.return_value = True
    server_db.delete_fact.return_value = True
    api.bm.bots[bot_id].server_manager = MagicMock(get_server_db=MagicMock(return_value=server_db))
    return bot_id, server_db


# --- config router -------------------------------------------------------


def test_config_requires_login(api):
    assert api.client.get("/api/config").status_code == 401


def test_config_rejects_untrusted_user(api):
    api.login(STRANGER)
    assert api.client.get("/api/config").status_code == 403
    assert api.client.put("/api/config", json={"key": "k", "value": "v"}).status_code == 403


def test_config_rejects_memory_only_user(api):
    api.login(MEMORY_USER)
    assert api.client.get("/api/config").status_code == 403


def test_config_admin_reads_and_writes(api):
    api.login(ADMIN)
    assert api.client.get("/api/config").status_code == 200
    r = api.client.put("/api/config", json={"key": "log_level", "value": "INFO"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success"


# --- personalities router ------------------------------------------------


def test_personalities_require_login(api):
    assert api.client.get("/api/personalities").status_code == 401


def test_personalities_reject_untrusted_user(api):
    api.login(STRANGER)
    assert api.client.get("/api/personalities").status_code == 403
    assert api.client.post("/api/personalities/p1", json={"name": "P"}).status_code == 403
    assert api.client.delete("/api/personalities/p1").status_code == 403
    assert api.cm.get_personality("p1") is None


def test_personalities_admin_create_read_delete(api):
    api.login(ADMIN)
    created = api.client.post("/api/personalities/p1", json={"name": "P", "description": "d"})
    assert created.status_code == 200, created.text
    assert api.client.get("/api/personalities/p1").status_code == 200
    assert "p1" in api.client.get("/api/personalities").json()["personalities"]
    assert api.client.post("/api/personalities/p1", json={"name": "P"}).status_code == 409
    assert api.client.delete("/api/personalities/p1").status_code == 200
    assert api.client.get("/api/personalities/p1").status_code == 404


def test_personalities_admin_unsafe_id_is_400(api):
    api.login(ADMIN)
    assert api.client.post("/api/personalities/bad.id", json={"name": "P"}).status_code == 400


# --- memories router -----------------------------------------------------


def test_memories_require_login(api):
    bot_id, _ = _bot_with_memory_db(api)
    assert api.client.post(f"/api/bots/{bot_id}/memories", json={"content": "x"}).status_code == 401


def test_memories_reject_untrusted_user(api):
    bot_id, server_db = _bot_with_memory_db(api)
    api.login(STRANGER)
    assert api.client.post(f"/api/bots/{bot_id}/memories", json={"content": "x"}).status_code == 403
    assert api.client.request("DELETE", f"/api/bots/{bot_id}/memories", json={"content": "x"}).status_code == 403
    server_db.add_fact.assert_not_called()
    server_db.delete_fact.assert_not_called()


@pytest.mark.parametrize("user", [ADMIN, MEMORY_USER])
def test_memories_add_and_delete_for_permitted_users(api, user):
    bot_id, server_db = _bot_with_memory_db(api)
    api.login(user)
    added = api.client.post(f"/api/bots/{bot_id}/memories", json={"content": "grug likes rocks"})
    assert added.status_code == 200, added.text
    server_db.add_fact.assert_called_once_with("grug likes rocks")
    removed = api.client.request("DELETE", f"/api/bots/{bot_id}/memories", json={"content": "grug likes rocks"})
    assert removed.status_code == 200, removed.text
    server_db.delete_fact.assert_called_once_with("grug likes rocks")


def test_memories_permitted_user_errors(api):
    bot_id, _ = _bot_with_memory_db(api)
    api.login(MEMORY_USER)
    assert api.client.post(f"/api/bots/{bot_id}/memories", json={"content": " "}).status_code == 400
    assert api.client.post("/api/bots/missing/memories", json={"content": "x"}).status_code == 404
