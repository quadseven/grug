"""Dashboard links in Discord messages come from PUBLIC_BASE_URL, never localhost,
and the admin settings view reports the effective data directory (grug#1689)."""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "fake_token")

from src.grugthink import config_legacy  # noqa: E402
from src.grugthink.bot import commands as bot_commands  # noqa: E402

ADMIN_ID = 777


@pytest.fixture(autouse=True)
def admin_ids(monkeypatch):
    monkeypatch.setattr(bot_commands.config, "TRUSTED_USER_IDS", [ADMIN_ID])
    monkeypatch.setenv("TRUSTED_MEMORY_IDS", "42")


@pytest.fixture
def interaction():
    inter = AsyncMock()
    inter.guild = MagicMock(id="g")
    inter.guild_id = "g"
    inter.user = MagicMock(id=ADMIN_ID)
    return inter


@pytest.fixture
def bot():
    b = MagicMock()
    b.personality_engine.get_personality.return_value = MagicMock(chosen_name="Grug", response_style="default")
    b.get_server_db.return_value.get_all_facts.return_value = ["a fact"]
    b.client.fetch_user = AsyncMock(return_value=MagicMock(display_name="Someone"))
    return b


def _sent_embed(mock):
    return mock.call_args.kwargs["embed"]


def test_config_reads_public_base_url_at_access_time(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert config_legacy.PUBLIC_BASE_URL is None
    monkeypatch.setenv("PUBLIC_BASE_URL", " https://grug.example.com/ ")
    assert config_legacy.PUBLIC_BASE_URL == "https://grug.example.com"


@pytest.mark.asyncio
async def test_what_know_link_uses_setting(monkeypatch, interaction, bot):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://grug.example.com")
    await bot_commands.what_know(interaction, bot)
    desc = _sent_embed(interaction.followup.send).description
    assert "https://grug.example.com" in desc
    assert "localhost" not in desc


@pytest.mark.asyncio
async def test_what_know_link_omitted_when_unset(monkeypatch, interaction, bot):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    await bot_commands.what_know(interaction, bot)
    desc = _sent_embed(interaction.followup.send).description
    assert "localhost" not in desc
    assert "http" not in desc


@pytest.mark.asyncio
async def test_managers_link_uses_setting_or_is_omitted(monkeypatch, interaction, bot):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://grug.example.com")
    await bot_commands.list_memory_managers(interaction, bot)
    values = [f.value for f in _sent_embed(interaction.response.send_message).fields]
    assert any("https://grug.example.com" in v for v in values)

    monkeypatch.delenv("PUBLIC_BASE_URL")
    await bot_commands.list_memory_managers(interaction, bot)
    embed = _sent_embed(interaction.response.send_message)
    assert not any("Web Interface" == f.name for f in embed.fields)
    assert not any("localhost" in f.value for f in embed.fields)


def test_admin_settings_reports_effective_data_dir(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from src.grugthink.api.server import create_app
    from src.grugthink.bot_manager import BotManager
    from src.grugthink.config.manager import ConfigManager

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("GRUGTHINK_CONFIG_PATH", str(Path(td) / "grugthink_config.yaml"))
        monkeypatch.setenv("GRUGBOT_DATA_DIR", "/srv/grug-state")
        cm = ConfigManager()
        cm.set_env_var("DISABLE_OAUTH", "true")
        client = TestClient(create_app(BotManager(config_manager=cm), cm))
        data = client.get("/api/admin/settings").json()
        assert data["GRUGBOT_DATA_DIR"] == "/srv/grug-state"
