"""Tests for /diagnose backend reporting (#1693).

The reply path falls back through a four-backend ladder - Ollama, Poolside,
OpenRouter, then Gemini - but /diagnose used to branch on
config.USE_GEMINI and report only "Using Gemini API" or "Using Ollama API",
so an operator debugging a silent bot was told the wrong backend whenever
a fallback tier was the one actually serving (or configured).

Coverage:
  - /diagnose lists every backend in ladder order with whether it is
    configured, from the one ladder definition the fallback chain uses.
  - with ONLY OpenRouter configured, /diagnose shows OpenRouter as
    configured (and the other three as not configured).
  - /diagnose shows which backend served the most recent reply for the bot.
"""

import os

# runtime_config.py raises at IMPORT time if DISCORD_TOKEN is unset - same
# guard test_llm_fallback.py / test_bot_commands.py use.
os.environ.setdefault("DISCORD_TOKEN", "fake_token")

import json as json_module  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from src.grugthink.bot import commands as bot_commands  # noqa: E402
from src.grugthink.bot import llm_clients  # noqa: E402
from src.grugthink.bot import prompts as prompts_module  # noqa: E402

TRUSTED_ID = 12345
BOT_ID = "test_bot"


@pytest.fixture(autouse=True)
def _clear_last_served():
    registry = getattr(llm_clients, "_last_served_backend", None)
    if registry is not None:
        registry.clear()
    yield
    if registry is not None:
        registry.clear()


def _config(**overrides):
    """A config stand-in with NO backend configured unless overridden -
    same patch-both-modules shape test_llm_fallback.py uses, because the
    ladder is read through llm_clients' config while /diagnose also reads
    commands' config."""
    cfg = MagicMock()
    cfg.OLLAMA_URLS = []
    cfg.OLLAMA_MODELS = ["llama3.2:3b"]
    cfg.POOLSIDE_API_KEY = None
    cfg.OPENROUTER_API_KEY = None
    cfg.GEMINI_API_KEY = None
    cfg.USE_GEMINI = False
    cfg.GEMINI_MODEL = "gemini-pro"
    cfg.TRUSTED_USER_IDS = [TRUSTED_ID]
    cfg.DB_PATH = "/nonexistent/grug_lore.db"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _only_openrouter_config():
    return _config(OPENROUTER_API_KEY="<redacted>")


def _make_bot():
    bot = MagicMock()
    bot.get_bot_id.return_value = BOT_ID
    personality = MagicMock()
    personality.chosen_name = "Grug"
    personality.name = "Grug"
    personality.response_style = "caveman"
    bot.personality_engine.get_personality.return_value = personality
    bot.chat_frequencies = {}
    bot.get_server_db.side_effect = Exception("no database in this test")
    return bot


def _make_interaction():
    interaction = AsyncMock()
    interaction.guild = MagicMock(id=123)
    interaction.user = MagicMock()
    interaction.user.id = TRUSTED_ID
    interaction.response.send_message = AsyncMock()
    return interaction


async def _run_diagnose(cfg):
    interaction = _make_interaction()
    with (
        patch("src.grugthink.bot.commands.config", cfg),
        patch("src.grugthink.bot.llm_clients.config", cfg),
    ):
        await bot_commands.diagnose(interaction, _make_bot())
    assert interaction.response.send_message.call_count == 1
    return interaction.response.send_message.call_args[0][0]


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self) -> dict:
        return self._json_data

    def iter_content(self, chunk_size: int = 1):
        yield json_module.dumps(self._json_data).encode()

    def close(self) -> None:
        pass


def _serve_one_reply_via_openrouter(cfg):
    """Drive the real query_model dispatch with only OpenRouter able to
    answer, so the reply is genuinely served by the fallback chain."""

    def _post(url, json=None, headers=None, timeout=None, **kwargs):
        assert "openrouter.ai" in url, f"unexpected backend called: {url}"
        return _FakeResponse(200, {"choices": [{"message": {"content": "OPENROUTER_REPLY"}}]})

    personality = MagicMock()
    personality.chosen_name = None
    personality.name = "Grug"
    personality.base_context = "You are Grug, a caveman."
    personality.response_style = "caveman"
    pe = MagicMock()
    pe.get_personality.return_value = personality
    pe.evolve_personality = MagicMock()
    server_db = MagicMock()
    server_db.search_facts.return_value = []

    with (
        patch.object(llm_clients.session, "post", _post),
        patch("src.grugthink.bot.llm_clients.config", cfg),
        patch("src.grugthink.bot.prompts.config", cfg),
        patch(
            "src.grugthink.bot.prompts.validate_and_process_response",
            side_effect=lambda r, *a, **k: r or None,
        ),
    ):
        result = prompts_module.query_model(
            "A totally unique statement for the diagnose test",
            server_db,
            "server1",
            pe,
            current_bot_id=BOT_ID,
        )
    assert result == "OPENROUTER_REPLY"


class TestDiagnoseBackendLadder:
    @pytest.mark.asyncio
    async def test_only_openrouter_configured_shows_openrouter(self):
        """The #1693 regression test: with only OpenRouter configured,
        /diagnose must name OpenRouter as a configured backend instead of
        claiming the bot uses Gemini or Ollama."""
        text = await _run_diagnose(_only_openrouter_config())

        assert "OpenRouter: configured" in text
        assert "Ollama: not configured" in text
        assert "Poolside: not configured" in text
        assert "Gemini: not configured" in text
        assert "Using Gemini API" not in text
        assert "Using Ollama API" not in text

    @pytest.mark.asyncio
    async def test_ladder_listed_in_fallback_order(self):
        text = await _run_diagnose(_only_openrouter_config())

        positions = [text.index(name) for name in ("Ollama", "Poolside", "OpenRouter", "Gemini")]
        assert positions == sorted(positions)

    def test_backend_ladder_is_one_shared_definition(self):
        """The chain and /diagnose read the same ladder: names in fallback
        order, configured flags reflecting the live config."""
        cfg = _only_openrouter_config()
        with patch("src.grugthink.bot.llm_clients.config", cfg):
            ladder = llm_clients.backend_ladder()

        assert [b.name for b in ladder] == ["Ollama", "Poolside", "OpenRouter", "Gemini"]
        assert {b.name: b.configured for b in ladder} == {
            "Ollama": False,
            "Poolside": False,
            "OpenRouter": True,
            "Gemini": False,
        }

    @pytest.mark.asyncio
    async def test_diagnose_shows_backend_that_served_most_recent_reply(self):
        cfg = _only_openrouter_config()
        _serve_one_reply_via_openrouter(cfg)

        text = await _run_diagnose(cfg)

        assert "Last reply served by: OpenRouter" in text

    @pytest.mark.asyncio
    async def test_diagnose_before_any_reply_says_none_served_yet(self):
        text = await _run_diagnose(_only_openrouter_config())

        assert "Last reply served by: none yet" in text
