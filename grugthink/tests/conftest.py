"""Pytest configuration and fixtures for tests.

This module imports fixtures from the main package conftest.py and
defines any test-specific fixtures needed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

# The LLM API mock fixtures (mock_gemini_api, mock_ollama_api,
# mock_ollama_errors, mock_gemini_module, mock_gemini_errors, etc.) are
# registered via pytest_plugins in the top-level grugthink/conftest.py
# (CodeRabbit #630: pytest_plugins must live in the ROOT conftest, not a
# nested one like this file - current pytest rejects it here at collection
# time). See that file for the actual registration.

# ============================================================================
# Discord.py Mocks for Testing Bot Commands
# ============================================================================


@pytest.fixture
def mock_discord_user():
    """Create a mock Discord User object.

    Provides a mock User with typical attributes and methods used in bot commands.
    The user has a fixed ID (123456789) and can be customized per test if needed.

    Returns:
        MagicMock: A mock Discord User with common attributes.

    Example:
        >>> def test_command(mock_discord_user):
        ...     assert mock_discord_user.id == 123456789
        ...     assert mock_discord_user.name == "TestUser"
    """
    user = MagicMock()
    user.id = 123456789
    user.name = "TestUser"
    user.display_name = "TestUser"
    user.discriminator = "0001"
    user.bot = False
    user.mention = "<@123456789>"
    return user


@pytest.fixture
def mock_discord_member(mock_discord_user):
    """Create a mock Discord Member object.

    A Member is a User with additional guild-specific attributes like roles,
    nickname, and guild permissions. Inherits from mock_discord_user.

    Args:
        mock_discord_user: The base user fixture to extend.

    Returns:
        MagicMock: A mock Discord Member with guild-specific attributes.

    Example:
        >>> def test_command(mock_discord_member):
        ...     assert mock_discord_member.nick == "TestNick"
        ...     assert mock_discord_member.guild.id == 987654321
    """
    member = MagicMock(spec=["id", "name", "display_name", "bot", "mention", "nick", "roles", "guild"])
    # Copy user attributes
    member.id = mock_discord_user.id
    member.name = mock_discord_user.name
    member.display_name = mock_discord_user.display_name
    member.bot = mock_discord_user.bot
    member.mention = mock_discord_user.mention

    # Add member-specific attributes
    member.nick = "TestNick"
    member.roles = []
    member.guild = MagicMock()
    member.guild.id = 987654321
    member.guild.name = "Test Guild"

    return member


@pytest.fixture
def mock_discord_guild():
    """Create a mock Discord Guild (server) object.

    Provides a mock Guild with common attributes like name, ID, channels, and members.

    Returns:
        MagicMock: A mock Discord Guild object.

    Example:
        >>> def test_command(mock_discord_guild):
        ...     assert mock_discord_guild.id == 987654321
        ...     assert mock_discord_guild.name == "Test Guild"
    """
    guild = MagicMock()
    guild.id = 987654321
    guild.name = "Test Guild"
    guild.member_count = 100
    guild.channels = []
    guild.members = []
    guild.roles = []
    guild.owner_id = 123456789
    return guild


@pytest.fixture
def mock_discord_text_channel(mock_discord_guild):
    """Create a mock Discord TextChannel object.

    Provides a mock TextChannel with async methods for sending messages and
    retrieving message history.

    Args:
        mock_discord_guild: The guild that owns this channel.

    Returns:
        MagicMock: A mock Discord TextChannel with async methods.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_send_message(mock_discord_text_channel):
        ...     msg = await mock_discord_text_channel.send("Hello")
        ...     mock_discord_text_channel.send.assert_called_with("Hello")
    """
    channel = MagicMock()
    channel.id = 111222333
    channel.name = "test-channel"
    channel.guild = mock_discord_guild
    channel.mention = "<#111222333>"
    channel.type = "text"

    # Mock async methods
    channel.send = AsyncMock(return_value=MagicMock(id=999888777, content="Test message"))
    channel.typing = MagicMock()
    channel.fetch_message = AsyncMock()

    # Mock history as an async generator
    async def mock_history(limit=100):
        """Mock message history generator."""
        # Return empty list by default, can be overridden in tests
        for _ in range(0):
            yield

    channel.history = mock_history

    return channel


@pytest.fixture
def mock_discord_message(mock_discord_user, mock_discord_text_channel):
    """Create a mock Discord Message object.

    Provides a mock Message with common attributes and async methods for
    reactions, editing, and deletion.

    Args:
        mock_discord_user: The author of the message.
        mock_discord_text_channel: The channel containing the message.

    Returns:
        MagicMock: A mock Discord Message object.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_message(mock_discord_message):
        ...     await mock_discord_message.add_reaction("👍")
        ...     assert mock_discord_message.content == "Test message"
    """
    message = MagicMock()
    message.id = 999888777
    message.content = "Test message"
    message.author = mock_discord_user
    message.channel = mock_discord_text_channel
    message.guild = mock_discord_text_channel.guild
    message.created_at = MagicMock()
    message.edited_at = None
    message.mentions = []
    message.mention_everyone = False
    message.embeds = []
    message.attachments = []

    # Mock async methods
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    message.reply = AsyncMock()

    return message


@pytest.fixture
def mock_discord_embed():
    """Create a mock Discord Embed object.

    Provides a mock Embed with methods for adding fields, setting colors,
    and other embed properties.

    Returns:
        MagicMock: A mock Discord Embed object.

    Example:
        >>> def test_embed(mock_discord_embed):
        ...     mock_discord_embed.add_field(name="Test", value="Value")
        ...     assert len(mock_discord_embed.fields) == 1
    """
    embed = MagicMock()
    embed.title = None
    embed.description = None
    embed.color = None
    embed.fields = []
    embed.footer = None
    embed.image = None
    embed.thumbnail = None
    embed.author = None

    # Mock methods
    def add_field(name, value, inline=True):
        field = {"name": name, "value": value, "inline": inline}
        embed.fields.append(field)
        return embed

    embed.add_field = MagicMock(side_effect=add_field)
    embed.set_footer = MagicMock(return_value=embed)
    embed.set_image = MagicMock(return_value=embed)
    embed.set_thumbnail = MagicMock(return_value=embed)
    embed.set_author = MagicMock(return_value=embed)

    return embed


@pytest.fixture
def mock_discord_interaction_response():
    """Create a mock Discord InteractionResponse object.

    Provides async methods for deferring responses and sending messages.
    This is accessed via interaction.response in Discord.py.

    Returns:
        MagicMock: A mock InteractionResponse with async methods.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_response(mock_discord_interaction_response):
        ...     await mock_discord_interaction_response.defer()
        ...     mock_discord_interaction_response.defer.assert_called_once()
    """
    response = MagicMock()
    response.defer = AsyncMock()
    response.send_message = AsyncMock()
    response.edit_message = AsyncMock()
    response.is_done = MagicMock(return_value=False)

    return response


@pytest.fixture
def mock_discord_interaction_followup():
    """Create a mock Discord Webhook (followup) object.

    Provides async methods for sending followup messages after an
    interaction has been deferred or responded to.

    Returns:
        MagicMock: A mock Webhook with async send method.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_followup(mock_discord_interaction_followup):
        ...     msg = await mock_discord_interaction_followup.send("Followup")
        ...     mock_discord_interaction_followup.send.assert_called_with("Followup")
    """
    followup = MagicMock()
    # Mock send to return a message-like object
    followup.send = AsyncMock(return_value=MagicMock(id=111222333, content="Followup message"))

    return followup


@pytest.fixture
def mock_discord_interaction(
    mock_discord_user,
    mock_discord_guild,
    mock_discord_text_channel,
    mock_discord_interaction_response,
    mock_discord_interaction_followup,
):
    """Create a mock Discord Interaction object.

    Provides a complete interaction mock with response, followup, user,
    guild, and channel attributes. This is the main object passed to
    slash command handlers.

    Args:
        mock_discord_user: The user who triggered the interaction.
        mock_discord_guild: The guild where the interaction occurred.
        mock_discord_text_channel: The channel where the interaction occurred.
        mock_discord_interaction_response: The response object for initial replies.
        mock_discord_interaction_followup: The followup object for additional messages.

    Returns:
        MagicMock: A mock Discord Interaction object.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_interaction(mock_discord_interaction):
        ...     await mock_discord_interaction.response.defer()
        ...     await mock_discord_interaction.followup.send("Done")
        ...     assert mock_discord_interaction.user.id == 123456789
    """
    interaction = MagicMock()
    interaction.user = mock_discord_user
    interaction.guild = mock_discord_guild
    interaction.guild_id = mock_discord_guild.id
    interaction.channel = mock_discord_text_channel
    interaction.channel_id = mock_discord_text_channel.id
    interaction.response = mock_discord_interaction_response
    interaction.followup = mock_discord_interaction_followup
    interaction.type = 2  # APPLICATION_COMMAND
    interaction.token = "test_interaction_token"
    interaction.id = 555666777
    interaction.application_id = 444555666

    return interaction


@pytest.fixture
def mock_discord_client():
    """Create a mock Discord Client object.

    Provides a mock client with async methods for fetching users, guilds,
    and other Discord objects. This represents the bot's connection to Discord.

    Returns:
        MagicMock: A mock Discord Client with async methods.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_client(mock_discord_client):
        ...     user = await mock_discord_client.fetch_user(123)
        ...     assert user is not None
    """
    client = MagicMock()
    client.user = MagicMock()
    client.user.id = 999999999
    client.user.name = "TestBot"
    client.user.bot = True

    # Mock async methods
    client.fetch_user = AsyncMock()
    client.fetch_guild = AsyncMock()
    client.fetch_channel = AsyncMock()
    client.wait_until_ready = AsyncMock()
    client.close = AsyncMock()

    # Mock properties
    client.guilds = []
    client.latency = 0.05

    return client


@pytest.fixture
def mock_discord_app_commands_tree(mock_discord_client):
    """Create a mock Discord app_commands.CommandTree object.

    The CommandTree manages slash command registration and syncing.
    Provides async methods for syncing commands to guilds or globally.

    Args:
        mock_discord_client: The client that owns this command tree.

    Returns:
        MagicMock: A mock CommandTree with async methods.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_tree(mock_discord_app_commands_tree):
        ...     await mock_discord_app_commands_tree.sync()
        ...     mock_discord_app_commands_tree.sync.assert_called_once()
    """
    tree = MagicMock()
    tree.client = mock_discord_client
    tree.sync = AsyncMock()
    tree.copy_global_to = MagicMock()
    tree.clear_commands = MagicMock()
    tree.get_commands = MagicMock(return_value=[])
    tree.add_command = MagicMock()
    tree.remove_command = MagicMock()

    return tree
