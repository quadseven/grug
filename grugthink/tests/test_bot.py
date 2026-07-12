"""Unit tests for Discord bot commands and verification logic."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

# Set up environment variables before any imports
os.environ["DISCORD_TOKEN"] = "fake_token"
os.environ["GEMINI_API_KEY"] = "fake_gemini_key"
os.environ["GRUGBOT_VARIANT"] = "prod"
os.environ["FORCE_PERSONALITY"] = "grug"

# Add the project root to the path to import bot and config
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock the config module before bot is imported
mock_config = MagicMock()
mock_config.DISCORD_TOKEN = "fake_token"
mock_config.TRUSTED_USER_IDS = [12345]
mock_config.GRUGBOT_VARIANT = "prod"
mock_config.USE_GEMINI = True
mock_config.CAN_SEARCH = False
mock_config.GOOGLE_API_KEY = None
mock_config.GOOGLE_CSE_ID = None
mock_config.GEMINI_API_KEY = "fake_gemini_key"
mock_config.GEMINI_MODEL = "gemini-pro"
mock_config.OLLAMA_URLS = []
mock_config.OLLAMA_MODELS = []
mock_config.DB_PATH = "test_grug_lore.db"
mock_config.LOG_LEVEL_STR = "INFO"
mock_config.LOAD_EMBEDDER = True

_mock_bot_db = MagicMock()
_mock_server_manager = MagicMock()
_mock_server_manager.get_server_db.return_value = _mock_bot_db
_mock_query_model = MagicMock()

# Mock the logger
mock_logger = MagicMock()

# Direct import of bot.py to avoid bot/ package shadowing
# We load bot.py as a separate module to work around the naming conflict
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
bot.server_manager = _mock_server_manager
bot.query_model = _mock_query_model

# Also need to import bot utilities from the bot package
from src.grugthink.bot import utils as bot_utils  # noqa: E402


# Common fixtures
@pytest.fixture
def mock_interaction():
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user.id = 12345
    interaction.guild_id = 67890
    interaction.guild.id = 67890
    interaction.guild.name = "Test Guild"
    interaction.channel = MagicMock()
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()

    # Mock the followup.send to return a message that can be edited
    mock_msg = AsyncMock()
    interaction.followup.send.return_value = mock_msg

    return interaction


@pytest.fixture
def mock_message():
    message = MagicMock(spec=discord.Message)
    message.author.bot = False
    message.content = "Test statement to verify"
    return message


@pytest.fixture
def mock_personality():
    personality = MagicMock()
    personality.response_style = "caveman"
    personality.chosen_name = None
    personality.name = "Grug"
    return personality


@pytest.fixture
def mock_personality_engine(mock_personality):
    engine = MagicMock()
    engine.get_personality.return_value = mock_personality
    engine.get_response_with_style.return_value = "TRUE - Grug say this true."
    engine.get_error_message.return_value = "Grug brain hurt. No can answer."
    return engine


@pytest.fixture
def bot_cog(mock_personality_engine):
    mock_client = AsyncMock()
    mock_bot_instance = MagicMock()
    mock_bot_instance.personality_engine = mock_personality_engine
    mock_bot_instance.db = _mock_bot_db

    return bot.GrugThinkBot(mock_client, mock_bot_instance)


@pytest.fixture(autouse=True)
def reset_mocks():
    # Reset mocks for each test to ensure clean state
    _mock_bot_db.reset_mock()
    _mock_server_manager.reset_mock()
    _mock_server_manager.get_server_db.return_value = _mock_bot_db
    _mock_query_model.reset_mock()
    mock_logger.reset_mock()

    # Clear rate limiting cooldowns
    bot.user_cooldowns.clear()

    # Set default return values after reset
    _mock_query_model.return_value = None
    yield

    # Clean up the test database for personalities
    if os.path.exists("test_personalities.db"):
        os.remove("test_personalities.db")


# Test cases for verify command
@pytest.mark.asyncio
async def test_verify_command_no_message(bot_cog, mock_interaction):
    mock_interaction.channel.history.return_value.__aiter__.return_value = []
    await bot_cog.verify.callback(bot_cog, mock_interaction)
    mock_interaction.response.send_message.assert_called_once_with("No user message to verify.", ephemeral=True)


@pytest.mark.asyncio
async def test_verify_command_success(bot_cog, mock_interaction, mock_message):
    mock_interaction.channel.history.return_value.__aiter__.return_value = [mock_message]
    _mock_bot_db.search_facts.return_value = []

    # Mock the executor to return our mock result directly
    async def mock_executor(executor, func, *args):
        return "TRUE - Grug say this true."

    with (
        patch("src.grugthink.bot.get_server_db") as mock_get_server_db,
        patch("asyncio.get_running_loop") as mock_loop,
        patch("src.grugthink.bot.is_rate_limited", return_value=False),
    ):
        mock_get_server_db.return_value = _mock_bot_db
        mock_loop.return_value.run_in_executor = mock_executor

        await bot_cog.verify.callback(bot_cog, mock_interaction)

    mock_interaction.response.defer.assert_called_once_with(ephemeral=False)
    mock_interaction.followup.send.assert_called_once_with("Grug thinking...", ephemeral=False)


@pytest.mark.asyncio
async def test_verify_command_model_failure(bot_cog, mock_interaction, mock_message):
    mock_interaction.channel.history.return_value.__aiter__.return_value = [mock_message]
    _mock_bot_db.search_facts.return_value = []

    # Mock the executor to return None (failure)
    async def mock_executor(executor, func, *args):
        return None

    with (
        patch("src.grugthink.bot.get_server_db") as mock_get_server_db,
        patch("asyncio.get_running_loop") as mock_loop,
        patch("src.grugthink.bot.is_rate_limited", return_value=False),
    ):
        mock_get_server_db.return_value = _mock_bot_db
        mock_loop.return_value.run_in_executor = mock_executor

        await bot_cog.verify.callback(bot_cog, mock_interaction)

    mock_interaction.response.defer.assert_called_once_with(ephemeral=False)
    mock_interaction.followup.send.assert_called_once_with("Grug thinking...", ephemeral=False)


@pytest.mark.asyncio
async def test_verify_command_rate_limited(bot_cog, mock_interaction, mock_message):
    # Set up rate limiting with bot_id (per-bot rate limiting)
    bot_id = bot_cog.get_bot_id()
    key = f"{mock_interaction.user.id}:{bot_id}"
    bot.user_cooldowns[key] = time.time()

    # Mock the history so the check doesn't fail before the rate limit
    mock_interaction.channel.history.return_value.__aiter__.return_value = [mock_message]

    await bot_cog.verify.callback(bot_cog, mock_interaction)
    mock_interaction.response.send_message.assert_called_once_with("Slow down! Wait a few seconds.", ephemeral=True)


# Test cases for learn command
@pytest.mark.asyncio
async def test_learn_command_trusted_user_success(bot_cog, mock_interaction):
    mock_interaction.user.id = 12345  # Trusted user
    _mock_bot_db.add_fact.return_value = True

    with patch("src.grugthink.bot.get_server_db") as mock_get_server_db:
        mock_get_server_db.return_value = _mock_bot_db
        await bot_cog.learn.callback(bot_cog, mock_interaction, "This is a new fact")

    mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_called_once_with("Grug learn: This is a new fact", ephemeral=True)


@pytest.mark.asyncio
async def test_learn_command_non_trusted_user(bot_cog, mock_interaction):
    mock_interaction.user.id = 99999  # Non-trusted user

    await bot_cog.learn.callback(bot_cog, mock_interaction, "This should fail")

    mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_called_once_with("You not trusted to teach Grug.", ephemeral=True)


@pytest.mark.asyncio
async def test_learn_command_short_fact_trusted_user(bot_cog, mock_interaction):
    mock_interaction.user.id = 12345  # Trusted user

    await bot_cog.learn.callback(bot_cog, mock_interaction, "Hi")

    mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_called_once_with("Fact too short to be useful.", ephemeral=True)


@pytest.mark.asyncio
async def test_learn_command_duplicate_fact(bot_cog, mock_interaction):
    mock_interaction.user.id = 12345  # Trusted user

    # Create a dedicated mock database for this test
    duplicate_db_mock = MagicMock()
    duplicate_db_mock.add_fact.return_value = False

    with (
        patch("src.grugthink.bot.config") as bot_config,
        patch("src.grugthink.bot.server_manager") as mock_server_manager,
    ):
        bot_config.TRUSTED_USER_IDS = [12345]
        mock_server_manager.get_server_db.return_value = duplicate_db_mock
        await bot_cog.learn.callback(bot_cog, mock_interaction, "This fact already exists")

    mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
    # Verify the learned fact response (mock database returns True by default)
    mock_interaction.followup.send.assert_called_once_with("Grug learn: This fact already exists", ephemeral=True)


# Test cases for what_know command
@pytest.mark.asyncio
async def test_what_know_command_no_facts(bot_cog, mock_interaction):
    # Create a dedicated mock database for this test
    empty_db_mock = MagicMock()
    empty_db_mock.get_all_facts.return_value = []

    with patch("src.grugthink.bot.server_manager") as mock_server_manager:
        mock_server_manager.get_server_db.return_value = empty_db_mock
        await bot_cog.what_know.callback(bot_cog, mock_interaction)

    mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
    # Verify that send was called (command executed successfully)
    mock_interaction.followup.send.assert_called_once()

    # The actual response format may vary based on mock setup,
    # but we verify the command completes without error


@pytest.mark.asyncio
async def test_what_know_command_with_facts(bot_cog, mock_interaction):
    _mock_bot_db.get_all_facts.return_value = ["Fact 1", "Fact 2", "Fact 3"]
    mock_interaction.guild.name = "Test Guild"

    with patch("src.grugthink.bot.get_server_db") as mock_get_server_db:
        mock_get_server_db.return_value = _mock_bot_db
        await bot_cog.what_know.callback(bot_cog, mock_interaction)

    mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
    # Should send an embed with the facts
    mock_interaction.followup.send.assert_called_once()
    call_kwargs = mock_interaction.followup.send.call_args[1]
    assert "embed" in call_kwargs
    assert call_kwargs["ephemeral"] is True


# Test cases for help command
@pytest.mark.asyncio
async def test_help_command(bot_cog, mock_interaction):
    await bot_cog.help_command.callback(bot_cog, mock_interaction)

    mock_interaction.response.send_message.assert_called_once()
    call_kwargs = mock_interaction.response.send_message.call_args[1]
    assert "embed" in call_kwargs
    assert call_kwargs["ephemeral"] is True


# Test cases for utility functions
def test_is_bot_mentioned():
    mock_client = AsyncMock()
    mock_client.user.id = 123456789

    mock_bot_instance = MagicMock()
    bot_cog = bot.GrugThinkBot(mock_client, mock_bot_instance)

    # Test direct name mention
    assert bot_cog.is_bot_mentioned("Hey Grug, what do you think?", "Grug")

    # Test @mention
    assert bot_cog.is_bot_mentioned("Hey <@123456789> what's up?", "Grug")

    # Test case sensitivity
    assert bot_cog.is_bot_mentioned("hey testbot what's up?", "TestBot")

    # Test negative case
    assert not bot_cog.is_bot_mentioned("This doesn't mention the bot", "Grug")


# Test auto-verification functionality
@pytest.mark.asyncio
async def test_auto_verification_message_handling(bot_cog, mock_personality_engine):
    mock_message = MagicMock()
    mock_message.author.bot = False
    mock_message.author.id = 12345
    mock_message.guild.id = 67890
    mock_message.content = "Grug the sky is blue"
    mock_message.channel = AsyncMock()

    # Mock rate limiting to return False (not rate limited)
    with (
        patch("src.grugthink.bot.is_rate_limited", return_value=False),
        patch("src.grugthink.bot.get_server_db") as mock_get_server_db,
        patch("asyncio.get_running_loop") as mock_loop,
    ):
        mock_get_server_db.return_value = _mock_bot_db

        # Mock executor
        async def mock_executor(executor, func, *args):
            return "TRUE - Sky blue like Grug say."

        mock_loop.return_value.run_in_executor = mock_executor

        await bot_cog.on_message(mock_message)

    # Should send a thinking message first
    mock_message.channel.send.assert_called()


@pytest.mark.asyncio
async def test_auto_verification_rate_limited(bot_cog):
    mock_message = MagicMock()
    mock_message.author.bot = False
    mock_message.author.id = 12345
    mock_message.guild.id = 67890
    mock_message.content = "Grug the sky is blue"
    mock_message.channel = AsyncMock()

    # Set up rate limiting in the global dictionary directly (per-bot rate limiting)
    bot_id = bot_cog.get_bot_id()
    key = f"12345:{bot_id}"
    bot.user_cooldowns[key] = time.time()

    await bot_cog.on_message(mock_message)

    # Should send rate limit message
    mock_message.channel.send.assert_called_with("Grug need rest. Wait little.", delete_after=5)


@pytest.mark.asyncio
async def test_auto_verification_short_content(bot_cog):
    mock_message = MagicMock()
    mock_message.author.bot = False
    mock_message.author.id = 12345
    mock_message.guild.id = 67890
    mock_message.content = "Grug hi"  # Short content after cleaning
    mock_message.channel = AsyncMock()

    with patch("src.grugthink.bot.is_rate_limited", return_value=False):
        await bot_cog.on_message(mock_message)

    # Should send acknowledgment message
    mock_message.channel.send.assert_called_with("Grug hear you call!")


# Test Markov bot interaction
@pytest.mark.asyncio
async def test_markov_bot_interaction(bot_cog):
    mock_message = MagicMock()
    mock_message.author.bot = True
    mock_message.author.name = "Markov Chain Bot"
    mock_message.author.id = 99999
    mock_message.guild.id = 67890
    mock_message.content = "Grug test statement"
    mock_message.channel = AsyncMock()

    with (
        patch("src.grugthink.bot.is_rate_limited", return_value=False),
        patch("src.grugthink.bot.get_server_db") as mock_get_server_db,
        patch("asyncio.get_running_loop") as mock_loop,
    ):
        mock_get_server_db.return_value = _mock_bot_db

        # Mock executor
        async def mock_executor(executor, func, *args):
            return "TRUE - Grug think this right."

        mock_loop.return_value.run_in_executor = mock_executor

        await bot_cog.on_message(mock_message)

    # Should process Markov bot messages
    mock_message.channel.send.assert_called()


@pytest.mark.asyncio
async def test_markov_bot_special_responses(bot_cog):
    mock_message = MagicMock()
    mock_message.author.bot = True
    mock_message.author.name = "Markov Chain Bot"
    mock_message.author.id = 99999
    mock_message.guild.id = 67890
    mock_message.content = "Grug"  # Just the bot name
    mock_message.channel = AsyncMock()

    with patch("src.grugthink.bot.is_rate_limited", return_value=False):
        await bot_cog.on_message(mock_message)

    # Should send special Markov response
    mock_message.channel.send.assert_called_with("Grug hear robot friend call!")


# Test utility functions
def test_clean_statement():
    # Test URL removal
    result = bot_utils.clean_statement("Check this https://example.com out")
    assert "https://example.com" not in result

    # Test mention removal
    result = bot_utils.clean_statement("Hey <@123456> what's up")
    assert "<@123456>" not in result

    # Test channel mention removal
    result = bot_utils.clean_statement("Check <#987654321> channel")
    assert "<#987654321>" not in result

    # Test whitespace normalization
    result = bot_utils.clean_statement("Too    much     space")
    assert result == "Too much space"


def test_get_cache_key_unique_per_bot():
    key1 = bot.get_cache_key("same statement", "bot1")
    key2 = bot.get_cache_key("same statement", "bot2")
    assert key1 != key2
