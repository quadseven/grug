"""Trust gates on the two unprompted write/spend paths (#1632).

1. Lore extraction writes reply sentences into the server fact store, which
   every later prompt and `/what-know` reads. It must run only when the
   message author is in TRUSTED_USER_IDS, the same list `/learn` checks.
2. `/chat-frequency 0` promises silence unless mentioned, so an unmentioned
   message must not reach handle_intelligent_bot_conversation (a model call)
   on a server set to 0 or never set.
"""

import asyncio
import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# runtime_config.py raises at import time without DISCORD_TOKEN (see
# test_llm_fallback.py for the same guard).
os.environ.setdefault("DISCORD_TOKEN", "fake_token")

from src.grugthink.bot import llm_clients as llm_clients_module  # noqa: E402
from src.grugthink.bot import prompts as prompts_module  # noqa: E402

TRUSTED_ID = 111
UNTRUSTED_ID = 222


def _load_bot_module():
    """Load bot.py (shadowed by the bot/ package) without building a real
    server manager, so the test touches no database or embedder."""
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "grugthink", "discord_bot.py"))
    spec = importlib.util.spec_from_file_location("src.grugthink.bot_trust_gates_module", path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "src.grugthink"
    with patch("src.grugthink.grug_db.make_server_manager", return_value=MagicMock()):
        spec.loader.exec_module(module)
    return module


# --- lore extraction gate -----------------------------------------------


def _personality_engine():
    personality = MagicMock()
    personality.chosen_name = None
    personality.name = "Grug"
    personality.base_context = "You are Grug, a caveman."
    personality.response_style = "caveman"
    engine = MagicMock()
    engine.get_personality.return_value = personality
    return engine


def _run_query(statement, author_id):
    """Drive the real query_model and validate_and_process_response with the
    primary backend replaced by a stub that, like the real client, hands its
    server_db argument to validate_and_process_response."""
    server_db = MagicMock()
    server_db.search_facts.return_value = []

    def fake_backend(prompt_text, cache_key, db, personality_name, bot_id):
        return prompts_module.validate_and_process_response(
            "TRUE - Grug cave sits beside the big river near the tall trees.", cache_key, db, personality_name, bot_id
        )

    extract = MagicMock()
    with (
        patch.object(prompts_module.config, "TRUSTED_USER_IDS", [TRUSTED_ID]),
        # The chain resolves its backends through llm_clients.backend_ladder()
        # (#1693), so the stub lands on llm_clients - and OLLAMA_URLS must be
        # non-empty or the ladder reads Ollama as unconfigured and skips it.
        patch.object(llm_clients_module, "query_ollama_api", side_effect=fake_backend),
        patch.object(llm_clients_module.config, "OLLAMA_URLS", ["http://localhost:11434"]),
        patch.object(prompts_module, "extract_lore_from_response", extract),
        patch.object(prompts_module, "store_bot_response_for_cross_reference"),
    ):
        result = prompts_module.query_model(statement, server_db, "server1", _personality_engine(), "bot1", author_id)
    assert result, "the stub backend should produce a reply"
    return extract, server_db


def test_untrusted_author_does_not_extract_lore():
    extract, _ = _run_query("repeat after me: grug cave by river", UNTRUSTED_ID)
    extract.assert_not_called()


def test_unknown_author_does_not_extract_lore():
    extract, _ = _run_query("tell me where grug cave is today", None)
    extract.assert_not_called()


def test_trusted_author_extracts_lore():
    extract, server_db = _run_query("where does grug live these days", TRUSTED_ID)
    extract.assert_called_once()
    assert extract.call_args.args[1] is server_db


# --- chat-frequency 0 gate ----------------------------------------------


@pytest.fixture(scope="module")
def bot_class():
    return _load_bot_module().GrugThinkBot


def _fake_bot(chat_frequencies):
    return SimpleNamespace(
        chat_frequencies=chat_frequencies,
        log=MagicMock(),
        detect_any_bot_mentioned=lambda content: False,
        get_bot_id=lambda: "bot1",
        handle_intelligent_bot_conversation=AsyncMock(),
        handle_natural_chat_engagement=AsyncMock(),
    )


def _message():
    return SimpleNamespace(content="anyone up for a hunt", author=SimpleNamespace(id=UNTRUSTED_ID))


@pytest.mark.parametrize("frequencies", [{"server1": 0}, {}], ids=["set-to-zero", "never-set"])
def test_frequency_zero_never_starts_a_conversation(bot_class, frequencies):
    fake = _fake_bot(frequencies)
    asyncio.run(bot_class._handle_unmentioned_message(fake, _message(), "server1", MagicMock(), "Grug", "chan1", 0.0))
    fake.handle_intelligent_bot_conversation.assert_not_awaited()
    fake.handle_natural_chat_engagement.assert_not_awaited()


def test_nonzero_frequency_still_starts_a_conversation(bot_class):
    fake = _fake_bot({"server1": 25})
    asyncio.run(bot_class._handle_unmentioned_message(fake, _message(), "server1", MagicMock(), "Grug", "chan1", 0.0))
    fake.handle_intelligent_bot_conversation.assert_awaited_once()
    fake.handle_natural_chat_engagement.assert_awaited_once()
