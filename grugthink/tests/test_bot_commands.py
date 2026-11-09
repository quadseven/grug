"""Chat-frequency slash commands, driven through the real cog and the real
bot/commands.py implementations.

bot.py is shadowed by the bot/ package, so it is loaded from its path. The load
patches make_server_manager for its duration only and installs nothing in
sys.modules, so later test files see the real modules.
"""

import importlib.util
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord import User
from discord.ext import commands

# runtime_config.py raises at import time without DISCORD_TOKEN (same guard as
# test_bot_trust_gates.py and test_llm_fallback.py).
os.environ.setdefault("DISCORD_TOKEN", "fake_token")

from src.grugthink.bot import commands as bot_commands  # noqa: E402

TRUSTED_ID = 12345


def _load_bot_module():
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "grugthink", "discord_bot.py"))
    spec = importlib.util.spec_from_file_location("src.grugthink.bot_commands_test_module", path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "src.grugthink"
    with patch("src.grugthink.grug_db.make_server_manager", return_value=MagicMock()):
        spec.loader.exec_module(module)
    return module


GrugThinkBot = _load_bot_module().GrugThinkBot


@pytest.fixture(autouse=True)
def trusted_ids():
    with patch.object(bot_commands.config, "TRUSTED_USER_IDS", [TRUSTED_ID]):
        yield


@pytest.fixture
def bot_instance(tmp_path):
    instance = MagicMock()
    instance.config.data_dir = str(tmp_path)
    instance.config.bot_id = "test_bot"
    instance.logger = MagicMock()
    instance.personality_engine = MagicMock()
    instance.personality_engine.get_personality.return_value = MagicMock(chosen_name="Grug", response_style="default")
    return instance


@pytest.fixture
def cog(bot_instance):
    client = MagicMock(spec=commands.Bot)
    client.tree = MagicMock()
    client.user = MagicMock(spec=User)
    client.user.id = 99
    return GrugThinkBot(client, bot_instance)


@pytest.fixture
def interaction():
    interaction = AsyncMock()
    interaction.guild = MagicMock(id="test_guild")
    interaction.guild_id = "test_guild"
    interaction.user = MagicMock(spec=User)
    interaction.user.id = TRUSTED_ID
    return interaction


@pytest.mark.asyncio
async def test_get_chat_frequency(cog, interaction):
    cog.chat_frequencies["test_guild"] = 10
    await cog.get_chat_frequency.callback(interaction)
    interaction.response.send_message.assert_called_with(
        "My current chat frequency in this server is 10%.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_chat_frequency_set(cog, interaction):
    await cog.chat_frequency.callback(interaction, 50)
    interaction.response.send_message.assert_called_with(
        "Grug will moderately participate in chat (50% frequency).",
        ephemeral=True,
    )
    assert cog.chat_frequencies["test_guild"] == 50


@pytest.mark.asyncio
async def test_chat_frequency_set_unauthorized(cog, interaction):
    interaction.user.id = 99999
    await cog.chat_frequency.callback(interaction, 50)
    interaction.response.send_message.assert_called_with(
        "❌ You don't have permission to modify chat settings.", ephemeral=True
    )
    assert "test_guild" not in cog.chat_frequencies


@pytest.mark.asyncio
async def test_chat_frequency_set_invalid_value(cog, interaction):
    await cog.chat_frequency.callback(interaction, 101)
    interaction.response.send_message.assert_called_with("Percentage must be between 0 and 100!", ephemeral=True)


def test_registered_commands_expose_only_their_own_parameters(cog):
    """discord.py builds each slash command's options from the wrapper's
    signature: the hidden bot argument must not appear, and types and
    defaults must survive."""
    freq = {p.name: p for p in cog.chat_frequency.parameters}
    assert list(freq) == ["percentage"]
    assert freq["percentage"].type.name == "integer"
    assert freq["percentage"].required

    know = {p.name: p for p in cog.what_know.parameters}
    assert list(know) == ["page", "search"]
    assert know["page"].type.name == "integer"
    assert know["page"].default == 1
    assert not know["search"].required


@pytest.mark.asyncio
async def test_wrapper_passes_keyword_arguments_like_discord(cog, interaction):
    """discord.py calls the callback as callback(interaction, **options)."""
    await cog.chat_frequency.callback(interaction, percentage=50)
    assert cog.chat_frequencies["test_guild"] == 50
