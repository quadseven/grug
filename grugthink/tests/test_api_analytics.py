"""GET /api/analytics: admin-only aggregate of the activity the bots already track (#1690)."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.grugthink.api.server import create_app
from src.grugthink.bot_manager import BotManager
from src.grugthink.bot_manager.models import BotConfig, BotInstance
from src.grugthink.config.manager import ConfigManager


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("GRUGTHINK_CONFIG_PATH", str(tmp_path / "grugthink_config.yaml"))
    monkeypatch.setenv("GRUGBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("src.grugthink.config.watcher.create_observer_and_handler", lambda _cm: (None, None))
    monkeypatch.setattr("src.grugthink.config.watcher.start_watching", lambda _cm: None)
    monkeypatch.setattr("src.grugthink.config.watcher.stop_watching", lambda _cm: None)
    cm = ConfigManager()
    cm.set_env_var("DISABLE_OAUTH", "true")
    bm = BotManager(config_manager=cm)
    yield TestClient(create_app(bm, cm)), bm, cm
    cm.stop()


def _add_bot(bm, bot_id, status, activity):
    cog = SimpleNamespace(channel_activity=activity, chat_frequencies={})
    client = SimpleNamespace(cogs={"GrugThinkBot": cog}, is_ready=lambda: False)
    inst = BotInstance(config=BotConfig(bot_id=bot_id, name=bot_id, discord_token_id="t"), client=client)
    inst.runtime_status = status
    bm.bots[bot_id] = inst


def test_analytics_empty_when_no_bots(api):
    client, _bm, _cm = api
    r = client.get("/api/analytics")
    assert r.status_code == 200
    assert r.json() == {"total_messages": 0, "response_rate": 0.0, "active_channels": 0, "bots": []}


def test_analytics_aggregates_per_bot_activity(api):
    client, bm, _cm = api
    _add_bot(
        bm,
        "a",
        "running",
        {
            "c1": {"message_count": 10, "last_bot_message": 5.0, "last_human_message": 4.0},
            "c2": {"message_count": 4, "last_bot_message": 0, "last_human_message": 3.0},
        },
    )
    _add_bot(bm, "b", "stopped", {})
    body = client.get("/api/analytics").json()
    assert body["total_messages"] == 14
    assert body["active_channels"] == 2
    assert body["response_rate"] == 50.0
    by_id = {b["bot_id"]: b for b in body["bots"]}
    assert by_id["a"] == {
        "bot_id": "a",
        "name": "a",
        "status": "running",
        "messages": 14,
        "channels": 2,
        "replied_channels": 1,
        "last_bot_message": 5.0,
    }
    assert by_id["b"]["messages"] == 0 and by_id["b"]["last_bot_message"] is None


def test_analytics_requires_admin(api):
    client, _bm, cm = api
    cm.set_env_var("DISABLE_OAUTH", "false")
    assert client.get("/api/analytics").status_code == 401


def test_dashboard_has_no_coming_soon_stubs():
    from pathlib import Path

    web = Path(__file__).resolve().parents[1] / "web"
    js = (web / "static/js/modern-app.js").read_text()
    html = (web / "modern-index.html").read_text()
    assert "Coming Soon" not in js
    assert 'data-lucide="bell"' not in html
    for handler in (
        "manageBotSettings",
        "configureBotInstance",
        "editTemplate",
        "createCustomTemplate",
        "editToken",
        "exportTokens",
    ):
        assert handler not in js and handler not in html
