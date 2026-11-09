"""Per-bot settings stay with their bot in multi-bot mode (grug#1667).

Two bots started through lifecycle.start_bot with different trusted ids and
Ollama URLs must each read their own values, in the bot's task and in an
executor thread, and start_bot must leave os.environ untouched.
"""

import asyncio
import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# runtime_config.py raises at import time without DISCORD_TOKEN (same guard as
# test_bot_trust_gates.py).
os.environ.setdefault("DISCORD_TOKEN", "fake_token")

from src.grugthink import bot_settings, llm, runtime_config  # noqa: E402
from src.grugthink.bot_manager import lifecycle  # noqa: E402

_REAL_SLEEP = asyncio.sleep

BOTS = {
    "bot_a": {"trusted": "111", "ollama": "http://ollama-a.example.com:11434", "personality": "grug"},
    "bot_b": {"trusted": "222", "ollama": "http://ollama-b.example.com:11434", "personality": "bigrob"},
}


def _config(bot_id, values, tmp_path):
    return SimpleNamespace(
        bot_id=bot_id,
        name=bot_id,
        enabled=True,
        discord_token_id="legacy",
        data_dir=str(tmp_path),
        log_level="INFO",
        load_embedder=False,
        custom_env={},
        override_gemini_key=None,
        override_google_api_key=None,
        override_google_cse_id=None,
        override_ollama_urls=values["ollama"],
        override_ollama_models=None,
        trusted_user_ids=values["trusted"],
        personality=values["personality"],
        force_personality=None,
    )


def _read_settings():
    return {
        "trusted": runtime_config.TRUSTED_USER_IDS,
        "ollama": runtime_config.OLLAMA_URLS,
        "gateway": llm.base_url(),
        "personality": bot_settings.getenv("FORCE_PERSONALITY"),
    }


def _expected(values):
    return {
        "trusted": [int(values["trusted"])],
        "ollama": [values["ollama"]],
        "gateway": values["ollama"],
        "personality": values["personality"],
    }


async def _fast_sleep(_seconds, *args, **kwargs):
    await _REAL_SLEEP(0)


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("TRUSTED_USER_IDS", "OLLAMA_URLS", "FORCE_PERSONALITY", "SPARK_GATEWAY_URL", "GRUGTHINK_LLM_URL"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_two_bots_keep_their_own_settings(tmp_path, clean_env):
    seen = {}
    release = asyncio.Event()
    clients = []

    def make_client(*args, **kwargs):
        client = MagicMock()
        client.guilds = []

        async def start(token):
            bot_id = token.removeprefix("token-")
            in_task = _read_settings()
            # Ordering: both bots start before either reads again, so a
            # global would show the second bot's values to the first.
            await release.wait()
            seen[bot_id] = {
                "task": in_task,
                "task_after_other_started": _read_settings(),
                "thread": await asyncio.to_thread(_read_settings),
            }

        client.start = start
        clients.append(client)
        return client

    manager = SimpleNamespace(config_manager=None, bots={})
    for bot_id, values in BOTS.items():
        instance = MagicMock()
        instance.runtime_status = "stopped"
        instance.consecutive_failures = 0
        instance.config = _config(bot_id, values, tmp_path)
        instance._legacy_discord_token = f"token-{bot_id}"
        manager.bots[bot_id] = instance

    env_before = dict(os.environ)
    with (
        patch.object(lifecycle, "PersonalityEngine", MagicMock()),
        patch.object(lifecycle, "make_server_manager", MagicMock()),
        patch.object(lifecycle.commands, "Bot", side_effect=make_client),
        patch.object(lifecycle, "setup_bot_commands", AsyncMock()),
        patch.object(lifecycle.asyncio, "sleep", _fast_sleep),
    ):
        for bot_id in BOTS:
            assert await lifecycle.start_bot(manager, bot_id), manager.bots[bot_id].logger.error.call_args
        assert dict(os.environ) == env_before, "start_bot must not write bot settings into os.environ"
        release.set()
        await asyncio.gather(*(manager.bots[b].task for b in BOTS))

    for bot_id, values in BOTS.items():
        for where in ("task", "task_after_other_started", "thread"):
            assert seen[bot_id][where] == _expected(values), (bot_id, where)
    # Outside any bot, the process environment is the only source.
    assert runtime_config.TRUSTED_USER_IDS == []
    assert bot_settings.current() is None


def _load_bot_module():
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "grugthink", "discord_bot.py"))
    spec = importlib.util.spec_from_file_location("src.grugthink.bot_settings_isolation_module", path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "src.grugthink"
    with patch("src.grugthink.grug_db.make_server_manager", return_value=MagicMock()):
        spec.loader.exec_module(module)
    return module


def test_bot_py_uses_the_bot_package_config_module():
    from src.grugthink.bot import commands as bot_commands

    assert _load_bot_module().config is bot_commands.config


@pytest.mark.asyncio
async def test_query_model_thread_sees_the_bot_settings(clean_env):
    """The auto-verification path hands query_model to a worker thread; that
    thread must read the bot's trusted ids, not the process environment's."""
    bot_module = _load_bot_module()
    seen = {}

    def fake_query_model(*args):
        seen["trusted"] = bot_module.config.TRUSTED_USER_IDS
        return "TRUE"

    fake_self = SimpleNamespace(
        get_server_db=lambda guild_id: MagicMock(),
        log=MagicMock(),
        personality_engine=MagicMock(),
        get_bot_id=lambda: "bot_a",
    )
    message = SimpleNamespace(guild=None, author=SimpleNamespace(id=1))
    with patch.object(bot_module, "query_model", fake_query_model), bot_settings.activate({"TRUSTED_USER_IDS": "111"}):
        result = await bot_module.GrugThinkBot._query_auto_verification(fake_self, message, "dm", "grug cave")
    assert result == "TRUE"
    assert seen["trusted"] == [111]
