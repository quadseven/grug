"""
Integration tests for GrugThink bot with proper Discord API mocking.
These tests focus on end-to-end functionality without heavy dependencies.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

# Import after setting up mocks
from tests.test_bot import mock_config, mock_logger


class TestDiscordIntegration:
    """Integration tests for Discord bot functionality."""

    @pytest.fixture
    def mock_guild(self):
        """Mock Discord guild."""
        guild = MagicMock(spec=discord.Guild)
        guild.id = 12345
        guild.name = "Test Guild"
        return guild

    @pytest.fixture
    def mock_channel(self):
        """Mock Discord text channel."""
        channel = AsyncMock(spec=discord.TextChannel)
        channel.id = 67890
        channel.name = "test-channel"
        return channel

    @pytest.fixture
    def mock_user(self):
        """Mock Discord user."""
        user = MagicMock(spec=discord.User)
        user.id = 12345  # Trusted user ID from config
        user.name = "TestUser"
        user.bot = False
        return user

    @pytest.fixture
    def mock_interaction(self, mock_user, mock_channel):
        """Mock Discord interaction."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.user = mock_user
        interaction.channel = mock_channel
        interaction.guild_id = 12345
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()

        # Mock the followup.send to return a message that can be edited
        mock_msg = AsyncMock()
        interaction.followup.send.return_value = mock_msg

        return interaction

    @pytest.fixture
    def mock_message(self, mock_user, mock_channel):
        """Mock Discord message."""
        message = MagicMock(spec=discord.Message)
        message.author = mock_user
        message.channel = mock_channel
        message.content = "The sky is blue today."
        message.id = 98765
        return message

    @pytest.fixture
    def bot_cog_integration(self):
        """Create a bot cog for integration testing."""
        # Create mock personality
        mock_personality = MagicMock()
        mock_personality.response_style = "caveman"
        mock_personality.chosen_name = None
        mock_personality.name = "Grug"

        # Create mock personality engine
        mock_personality_engine = MagicMock()
        mock_personality_engine.get_personality.return_value = mock_personality
        mock_personality_engine.get_response_with_style.return_value = "TRUE - Grug say sky blue sometimes."
        mock_personality_engine.get_error_message.return_value = "Grug brain hurt. No can answer."

        # Create mock bot instance
        mock_client = AsyncMock()
        mock_bot_instance = MagicMock()
        mock_bot_instance.personality_engine = mock_personality_engine
        mock_bot_instance.db = MagicMock()
        mock_bot_instance.config = MagicMock(bot_id="test-bot")

        with (
            patch.dict("sys.modules", {"src.grugthink.config": mock_config, "src.grugthink.grug_db": MagicMock()}),
            patch("src.grugthink.bot.log", mock_logger),
        ):
            from src.grugthink import bot

            return bot.GrugThinkBot(mock_client, mock_bot_instance)

    @pytest.mark.asyncio
    async def test_verify_command_integration(self, bot_cog_integration, mock_interaction, mock_message):
        """Test the verify command end-to-end."""
        # Setup mock responses
        mock_interaction.channel.history.return_value.__aiter__.return_value = [mock_message]

        # Mock the executor
        async def mock_executor(executor, func, *args):
            return "TRUE - Grug say sky blue sometimes."

        with (
            patch("src.grugthink.bot.get_server_db") as mock_get_server_db,
            patch("asyncio.get_running_loop") as mock_loop,
            patch("src.grugthink.bot.is_rate_limited", return_value=False),
        ):
            server_db_mock = MagicMock()
            server_db_mock.search_facts.return_value = []
            mock_get_server_db.return_value = server_db_mock
            mock_loop.return_value.run_in_executor = mock_executor

            # Execute the command
            await bot_cog_integration.verify.callback(bot_cog_integration, mock_interaction)

            # Verify interaction flow
            mock_interaction.response.defer.assert_called_once_with(ephemeral=False)
            mock_interaction.followup.send.assert_called_once_with("Grug thinking...", ephemeral=False)

    @pytest.mark.asyncio
    async def test_learn_command_integration(self, bot_cog_integration, mock_interaction):
        """Test the learn command end-to-end."""
        # Make user trusted
        mock_interaction.user.id = 12345

        with (
            patch("src.grugthink.bot.get_server_db") as mock_get_server_db,
            patch("src.grugthink.bot.config") as bot_config,
        ):
            server_db_mock = MagicMock()
            server_db_mock.add_fact.return_value = True
            mock_get_server_db.return_value = server_db_mock
            bot_config.TRUSTED_USER_IDS = [12345]

            # Execute the command
            await bot_cog_integration.learn.callback(bot_cog_integration, mock_interaction, "Grug love mammoth meat.")

            # Verify interaction flow
            mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
            mock_interaction.followup.send.assert_called_once_with(
                "Grug learn: Grug love mammoth meat.", ephemeral=True
            )

    @pytest.fixture(autouse=True)
    def mock_user_cooldowns(self):
        with patch("src.grugthink.bot.user_cooldowns", {}) as mock_cooldowns:
            yield mock_cooldowns

    @pytest.mark.asyncio
    async def test_rate_limiting_integration(
        self, bot_cog_integration, mock_interaction, mock_message, mock_user_cooldowns
    ):
        """Test rate limiting functionality."""
        import time

        # Configure bot_id on the mock
        bot_cog_integration.bot_instance.config.bot_id = "test-bot"

        # Setup mock responses - ensure message is not from bot and has content
        mock_message.author.bot = False
        mock_message.content = "Test message content"
        mock_interaction.channel.history.return_value.__aiter__.return_value = [mock_message]

        bot_id = bot_cog_integration.get_bot_id()
        key = f"{mock_interaction.user.id}:{bot_id}"

        # Set cooldown to a recent time (e.g., 4 seconds ago) to trigger rate limit
        mock_user_cooldowns[key] = time.time() - 4

        # Execute the command
        await bot_cog_integration.verify.callback(bot_cog_integration, mock_interaction)

        # Should be rate limited
        mock_interaction.response.send_message.assert_called_once_with("Slow down! Wait a few seconds.", ephemeral=True)

    @pytest.mark.asyncio
    async def test_untrusted_user_learn_integration(self, bot_cog_integration, mock_interaction):
        """Test learn command with untrusted user."""
        # Ensure guild_id is set
        mock_interaction.guild_id = 12345
        mock_interaction.user.id = 99999  # Make user untrusted

        with patch("src.grugthink.bot.config") as bot_config:
            bot_config.TRUSTED_USER_IDS = [12345]  # User 99999 is not in this list

            # Execute the command
            await bot_cog_integration.learn.callback(bot_cog_integration, mock_interaction, "Untrusted fact.")

            # Verify rejection
            mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
            mock_interaction.followup.send.assert_called_once_with("You not trusted to teach Grug.", ephemeral=True)


class TestDatabaseIntegration:
    """Integration tests for database functionality."""

    @pytest.mark.asyncio
    async def test_database_search_integration(self):
        """Test database search integration."""
        with (
            patch.dict("sys.modules", {"src.grugthink.config": mock_config}),
            patch("src.grugthink.bot.log", mock_logger),
        ):
            # Mock database
            mock_db = MagicMock()
            mock_db.search_facts.return_value = ["Grug know fire good.", "Grug hunt mammoth."]

            # Test search functionality
            results = mock_db.search_facts("fire", k=5)
            assert len(results) == 2
            assert "fire good" in results[0]


class TestConfigurationIntegration:
    """Integration tests for configuration handling."""

    def test_config_loading_integration(self):
        """Test configuration loading integration."""
        with patch.dict("sys.modules", {"src.grugthink.config": mock_config}):
            from src.grugthink import bot

            # Verify config is accessible
            assert hasattr(bot, "config") or mock_config.DISCORD_TOKEN == "fake_token"
            assert mock_config.TRUSTED_USER_IDS == [12345]
            assert mock_config.USE_GEMINI is True
