import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import User
from discord.ext import commands

# Set up test environment variables before imports
os.environ["DISCORD_TOKEN"] = "fake_token"
os.environ["GEMINI_API_KEY"] = "fake_gemini_key"
os.environ["GRUGBOT_VARIANT"] = "prod"
os.environ["FORCE_PERSONALITY"] = "grug"
os.environ["TESTING"] = "true"

# Add the project root to the path to import bot and config
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock the config module before bot is imported
mock_config = MagicMock()
mock_config.TRUSTED_USER_IDS = [12345]
mock_config.DISCORD_TOKEN = "fake_token"
mock_config.GRUGBOT_VARIANT = "prod"
mock_config.USE_GEMINI = True
mock_config.DB_PATH = "test_grug_lore.db"
mock_config.LOG_LEVEL_STR = "INFO"
mock_config.LOAD_EMBEDDER = True

# Mock the logger
mock_logger = MagicMock()

# Direct import of bot.py to avoid bot/ package shadowing
import importlib.util  # noqa: E402

_bot_py_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "grugthink", "bot.py"))
_spec = importlib.util.spec_from_file_location("src.grugthink.bot_module", _bot_py_path)
bot = importlib.util.module_from_spec(_spec)

# Set __package__ so relative imports work
bot.__package__ = "src.grugthink"

# Set up mocks before executing the module
sys.modules["src.grugthink.config"] = mock_config
sys.modules["src.grugthink.grug_db"] = MagicMock()
sys.modules["src.grugthink.bot_module"] = bot

# Execute the bot.py module
_spec.loader.exec_module(bot)

# Patch attributes on the now-loaded bot module
bot.log = mock_logger

# Store references for use in tests
GrugThinkBot = bot.GrugThinkBot
TRUSTED_USER_IDS = mock_config.TRUSTED_USER_IDS


@pytest.fixture
def bot_instance():
    """Fixture for a mocked bot instance."""
    instance = MagicMock()
    instance.config.data_dir = "/tmp"
    instance.config.bot_id = "test_bot"
    instance.logger = MagicMock()
    instance.personality_engine = MagicMock()
    instance.personality_engine.get_personality.return_value = MagicMock(chosen_name="Grug", response_style="default")
    return instance


@pytest.fixture
def client(bot_instance):
    """Fixture for a mocked client."""
    client = MagicMock(spec=commands.Bot)
    client.tree = MagicMock()
    client.user = MagicMock(spec=User)
    client.user.id = 12345
    return client


@pytest.fixture
def cog(client, bot_instance):
    """Fixture for the GrugThinkBot cog."""
    return GrugThinkBot(client, bot_instance)


@pytest.fixture
def interaction():
    """Fixture for a mocked interaction."""
    interaction = AsyncMock()
    interaction.guild = MagicMock(id="test_guild")
    interaction.guild_id = "test_guild"
    interaction.user = MagicMock(spec=User)
    interaction.user.id = TRUSTED_USER_IDS[0] if TRUSTED_USER_IDS else 12345
    return interaction


@pytest.mark.asyncio
async def test_get_chat_frequency(cog, interaction):
    """Test the /get-chat-frequency command."""
    cog.chat_frequencies["test_guild"] = 10
    await cog.get_chat_frequency.callback(cog, interaction)
    interaction.response.send_message.assert_called_with(
        "My current chat frequency in this server is 10%.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_chat_frequency_set(cog, interaction):
    """Test the /chat-frequency command for setting."""
    await cog.chat_frequency.callback(cog, interaction, 50)
    interaction.response.send_message.assert_called_with(
        "Grug will moderately participate in chat (50% frequency).",
        ephemeral=True,
    )
    assert cog.chat_frequencies["test_guild"] == 50


@pytest.mark.asyncio
async def test_chat_frequency_set_unauthorized(cog, interaction):
    """Test that unauthorized users cannot set the chat frequency."""
    interaction.user.id = 99999  # Not a trusted user
    await cog.chat_frequency.callback(cog, interaction, 50)
    interaction.response.send_message.assert_called_with(
        "❌ You don't have permission to modify chat settings.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_chat_frequency_set_invalid_value(cog, interaction):
    """Test that the chat frequency cannot be set to an invalid value."""
    await cog.chat_frequency.callback(cog, interaction, 101)
    interaction.response.send_message.assert_called_with("Percentage must be between 0 and 100!", ephemeral=True)
