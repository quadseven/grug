"""Tests for Discord.py mocks to verify they work correctly.

This module tests the Discord.py mock fixtures defined in conftest.py
to ensure they provide the expected behavior for testing bot commands.
"""

import pytest


@pytest.mark.asyncio
async def test_mock_discord_user(mock_discord_user):
    """Test that mock_discord_user fixture provides expected attributes."""
    assert mock_discord_user.id == 123456789
    assert mock_discord_user.name == "TestUser"
    assert mock_discord_user.display_name == "TestUser"
    assert mock_discord_user.bot is False
    assert mock_discord_user.mention == "<@123456789>"


@pytest.mark.asyncio
async def test_mock_discord_member(mock_discord_member):
    """Test that mock_discord_member fixture provides guild-specific attributes."""
    assert mock_discord_member.id == 123456789
    assert mock_discord_member.name == "TestUser"
    assert mock_discord_member.nick == "TestNick"
    assert mock_discord_member.guild.id == 987654321
    assert mock_discord_member.guild.name == "Test Guild"


@pytest.mark.asyncio
async def test_mock_discord_guild(mock_discord_guild):
    """Test that mock_discord_guild fixture provides guild attributes."""
    assert mock_discord_guild.id == 987654321
    assert mock_discord_guild.name == "Test Guild"
    assert mock_discord_guild.member_count == 100
    assert mock_discord_guild.owner_id == 123456789


@pytest.mark.asyncio
async def test_mock_discord_text_channel(mock_discord_text_channel):
    """Test that mock_discord_text_channel supports async operations."""
    # Test sending a message
    message = await mock_discord_text_channel.send("Hello world")
    assert message.id == 999888777
    mock_discord_text_channel.send.assert_called_once_with("Hello world")

    # Test channel attributes
    assert mock_discord_text_channel.id == 111222333
    assert mock_discord_text_channel.name == "test-channel"
    assert mock_discord_text_channel.guild.id == 987654321


@pytest.mark.asyncio
async def test_mock_discord_message(mock_discord_message):
    """Test that mock_discord_message provides message operations."""
    # Test message attributes
    assert mock_discord_message.id == 999888777
    assert mock_discord_message.content == "Test message"
    assert mock_discord_message.author.id == 123456789

    # Test async operations
    await mock_discord_message.add_reaction("👍")
    mock_discord_message.add_reaction.assert_called_once_with("👍")

    await mock_discord_message.edit(content="Edited")
    mock_discord_message.edit.assert_called_once_with(content="Edited")

    await mock_discord_message.delete()
    mock_discord_message.delete.assert_called_once()


@pytest.mark.asyncio
async def test_mock_discord_embed(mock_discord_embed):
    """Test that mock_discord_embed supports embed operations."""
    # Test adding fields
    mock_discord_embed.add_field(name="Field1", value="Value1", inline=True)
    assert len(mock_discord_embed.fields) == 1
    assert mock_discord_embed.fields[0]["name"] == "Field1"
    assert mock_discord_embed.fields[0]["value"] == "Value1"

    # Test field chaining
    mock_discord_embed.add_field(name="Field2", value="Value2", inline=False)
    assert len(mock_discord_embed.fields) == 2


@pytest.mark.asyncio
async def test_mock_discord_interaction_response(mock_discord_interaction_response):
    """Test that mock_discord_interaction_response supports defer and send."""
    # Test defer
    await mock_discord_interaction_response.defer()
    mock_discord_interaction_response.defer.assert_called_once()

    # Test send_message
    await mock_discord_interaction_response.send_message("Hello", ephemeral=True)
    mock_discord_interaction_response.send_message.assert_called_once_with("Hello", ephemeral=True)


@pytest.mark.asyncio
async def test_mock_discord_interaction_followup(mock_discord_interaction_followup):
    """Test that mock_discord_interaction_followup supports sending messages."""
    message = await mock_discord_interaction_followup.send("Followup message")
    assert message.id == 111222333
    mock_discord_interaction_followup.send.assert_called_once_with("Followup message")


@pytest.mark.asyncio
async def test_mock_discord_interaction(mock_discord_interaction):
    """Test the complete interaction workflow with defer and followup."""
    # Test interaction attributes
    assert mock_discord_interaction.user.id == 123456789
    assert mock_discord_interaction.guild.id == 987654321
    assert mock_discord_interaction.channel.id == 111222333

    # Test typical interaction workflow
    await mock_discord_interaction.response.defer(ephemeral=True)
    mock_discord_interaction.response.defer.assert_called_once_with(ephemeral=True)

    message = await mock_discord_interaction.followup.send("Done!")
    assert message.content == "Followup message"
    mock_discord_interaction.followup.send.assert_called_once_with("Done!")


@pytest.mark.asyncio
async def test_mock_discord_client(mock_discord_client):
    """Test that mock_discord_client provides client operations."""
    assert mock_discord_client.user.id == 999999999
    assert mock_discord_client.user.name == "TestBot"
    assert mock_discord_client.user.bot is True
    assert mock_discord_client.latency == 0.05

    # Test async methods
    await mock_discord_client.fetch_user(123)
    mock_discord_client.fetch_user.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_mock_discord_app_commands_tree(mock_discord_app_commands_tree):
    """Test that mock_discord_app_commands_tree supports command tree operations."""
    # Test sync
    await mock_discord_app_commands_tree.sync()
    mock_discord_app_commands_tree.sync.assert_called_once()

    # Test get_commands
    commands = mock_discord_app_commands_tree.get_commands()
    assert commands == []


@pytest.mark.asyncio
async def test_interaction_workflow_complete(mock_discord_interaction):
    """Test a complete interaction workflow mimicking real bot command behavior."""
    # Simulate a command handler that defers, processes, and follows up
    interaction = mock_discord_interaction

    # Step 1: Defer the response (bot is thinking)
    await interaction.response.defer(ephemeral=False)

    # Step 2: Simulate some processing...
    # (In real bot, this would be querying the database or AI)

    # Step 3: Send followup message with results
    await interaction.followup.send("Processing complete! Here are the results.")

    # Verify the workflow was executed correctly
    interaction.response.defer.assert_called_once_with(ephemeral=False)
    interaction.followup.send.assert_called_once_with("Processing complete! Here are the results.")


@pytest.mark.asyncio
async def test_message_with_embed(mock_discord_text_channel, mock_discord_embed):
    """Test sending a message with an embed."""
    # Create an embed
    mock_discord_embed.title = "Test Embed"
    mock_discord_embed.description = "This is a test"
    mock_discord_embed.add_field(name="Field", value="Value")

    # Send message with embed
    await mock_discord_text_channel.send(embed=mock_discord_embed)

    # Verify
    mock_discord_text_channel.send.assert_called_once_with(embed=mock_discord_embed)
    assert len(mock_discord_embed.fields) == 1
