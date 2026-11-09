"""Bot start, stop and status through BotManager (#1691).

The Discord client and the component builders are replaced; the manager,
lifecycle and status code run for real.
"""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "fake_token")

from src.grugthink.bot_manager import BotManager, lifecycle  # noqa: E402


@pytest.fixture
def bm(tmp_path, monkeypatch):
    monkeypatch.setenv("GRUGBOT_DATA_DIR", str(tmp_path / "data"))
    return BotManager(config_file=str(tmp_path / "bot_configs.json"))


@pytest.fixture
def stub_start(monkeypatch):
    """Skip env and Discord construction; mark the bot started with a fake client."""
    calls = []

    async def fake_start_with_settings(instance, bot_id, config, bot_env):
        calls.append(bot_id)
        instance.client = MagicMock()
        instance.client.is_ready.return_value = False
        instance.client.guilds = []

    monkeypatch.setattr(lifecycle, "create_bot_environment", lambda *_a, **_k: {"DISCORD_TOKEN": "t"})
    monkeypatch.setattr(lifecycle, "_start_with_settings", fake_start_with_settings)
    return calls


def _make(bm, **kwargs):
    return bm.create_bot("bot-a", "token-id", **kwargs)


def test_new_bot_status_is_stopped(bm):
    bot_id = _make(bm)
    status = bm.get_bot_status(bot_id)
    assert status["status"] == "stopped"
    assert status["name"] == "bot-a"
    assert status["enabled"] is True
    assert bm.list_bots()[0]["bot_id"] == bot_id


def test_status_of_unknown_bot_is_none(bm):
    assert bm.get_bot_status("nope") is None


@pytest.mark.asyncio
async def test_start_marks_running_and_stop_marks_stopped(bm, stub_start):
    bot_id = _make(bm)
    assert await bm.start_bot(bot_id) is True
    assert stub_start == [bot_id]
    status = bm.get_bot_status(bot_id)
    assert status["status"] == "running"
    assert status["last_heartbeat"] is not None

    client = bm.bots[bot_id].client
    client.close = AsyncMock()
    assert await bm.stop_bot(bot_id) is True
    client.close.assert_awaited_once()
    instance = bm.bots[bot_id]
    assert instance.runtime_status == "stopped"
    assert instance.client is None
    assert instance.last_heartbeat is None


@pytest.mark.asyncio
async def test_start_twice_does_not_rebuild(bm, stub_start):
    bot_id = _make(bm)
    await bm.start_bot(bot_id)
    assert await bm.start_bot(bot_id) is True
    assert stub_start == [bot_id]


@pytest.mark.asyncio
async def test_start_disabled_bot_is_refused(bm, stub_start):
    bot_id = _make(bm, enabled=False)
    assert await bm.start_bot(bot_id) is False
    assert stub_start == []
    assert bm.bots[bot_id].runtime_status == "stopped"


@pytest.mark.asyncio
async def test_start_unknown_bot_is_false(bm):
    assert await bm.start_bot("nope") is False


@pytest.mark.asyncio
async def test_start_failure_sets_error_and_counts(bm, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("no token")

    monkeypatch.setattr(lifecycle, "create_bot_environment", boom)
    bot_id = _make(bm)
    assert await bm.start_bot(bot_id) is False
    status = bm.get_bot_status(bot_id)
    assert status["status"] == "error"
    assert status["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_stop_when_not_running_is_a_noop(bm):
    bot_id = _make(bm)
    assert await bm.stop_bot(bot_id) is True
    assert bm.bots[bot_id].runtime_status == "stopped"
    assert await bm.stop_bot("nope") is False


@pytest.mark.asyncio
async def test_stop_failure_sets_error(bm, stub_start):
    bot_id = _make(bm)
    await bm.start_bot(bot_id)
    bm.bots[bot_id].client.close = AsyncMock(side_effect=RuntimeError("close failed"))
    assert await bm.stop_bot(bot_id) is False
    assert bm.bots[bot_id].runtime_status == "error"


@pytest.mark.asyncio
async def test_stop_all_stops_only_running_bots(bm, stub_start):
    running = _make(bm)
    idle = bm.create_bot("bot-b", "token-id")
    await bm.start_bot(running)
    bm.bots[running].client.close = AsyncMock()
    await bm.stop_all_bots()
    assert bm.bots[running].runtime_status == "stopped"
    assert bm.bots[idle].runtime_status == "stopped"
    assert bm.running is False


@pytest.mark.asyncio
async def test_delete_running_bot_stops_and_removes_it(bm, stub_start):
    bot_id = _make(bm)
    await bm.start_bot(bot_id)
    bm.bots[bot_id].client.close = AsyncMock()
    assert await bm.delete_bot(bot_id) is True
    assert bot_id not in bm.bots
    assert await bm.delete_bot(bot_id) is False
