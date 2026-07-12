# Discord Mocks Quick Reference

Quick reference guide for using Discord.py mocks in tests.

## Basic Setup

```python
import pytest

@pytest.mark.asyncio
async def test_my_command(mock_discord_interaction):
    # Your test code here
    pass
```

## Common Fixtures

| Fixture | Purpose | Key Attributes |
|---------|---------|----------------|
| `mock_discord_user` | Discord user | `id`, `name`, `display_name` |
| `mock_discord_member` | Guild member | `nick`, `roles`, `guild` |
| `mock_discord_guild` | Server | `id`, `name`, `member_count` |
| `mock_discord_text_channel` | Text channel | `id`, `name`, `send()` |
| `mock_discord_message` | Message | `content`, `author`, `edit()` |
| `mock_discord_embed` | Embed | `add_field()`, `fields` |
| `mock_discord_interaction` | Complete interaction | `response`, `followup` |

## Typical Test Pattern

### 1. Test with Defer and Followup

```python
@pytest.mark.asyncio
async def test_command(mock_discord_interaction):
    # Defer response
    await mock_discord_interaction.response.defer(ephemeral=True)

    # Process
    result = "Done!"

    # Send followup
    await mock_discord_interaction.followup.send(result)

    # Verify
    mock_discord_interaction.response.defer.assert_called_once()
```

### 2. Test with Direct Response

```python
@pytest.mark.asyncio
async def test_direct(mock_discord_interaction):
    await mock_discord_interaction.response.send_message(
        "Hello!",
        ephemeral=True
    )

    mock_discord_interaction.response.send_message.assert_called_once()
```

### 3. Test with Embeds

```python
@pytest.mark.asyncio
async def test_embed(mock_discord_interaction, mock_discord_embed):
    mock_discord_embed.title = "Test"
    mock_discord_embed.add_field(name="Field", value="Value")

    await mock_discord_interaction.response.send_message(
        embed=mock_discord_embed
    )

    assert len(mock_discord_embed.fields) == 1
```

## Async Methods Cheatsheet

| Object | Method | Returns |
|--------|--------|---------|
| `channel` | `send(content)` | Mock message |
| `message` | `add_reaction(emoji)` | None |
| `message` | `edit(content=...)` | None |
| `message` | `delete()` | None |
| `interaction.response` | `defer(ephemeral=...)` | None |
| `interaction.response` | `send_message(...)` | None |
| `interaction.followup` | `send(...)` | Mock message |
| `client` | `fetch_user(id)` | Mock user |

## Default Values

```python
mock_discord_user.id = 123456789
mock_discord_guild.id = 987654321
mock_discord_text_channel.id = 111222333
mock_discord_message.id = 999888777
mock_discord_client.user.id = 999999999
```

## Verification Examples

```python
# Called once with specific args
mock.defer.assert_called_once_with(ephemeral=True)

# Called once (any args)
mock.send.assert_called_once()

# Called with specific args (last call)
mock.send.assert_called_with("Hello")

# Not called
mock.delete.assert_not_called()

# Called N times
assert mock.send.call_count == 3
```

## Customizing Mocks

```python
# Override default values
mock_discord_user.id = 999
mock_discord_user.name = "CustomName"

# Add custom behavior
mock_discord_client.fetch_user.return_value = mock_discord_user

# Simulate errors
mock_discord_client.fetch_user.side_effect = Exception("Error")
```

## Common Patterns

### Test Permission Check

```python
def test_requires_trusted_user(mock_discord_interaction):
    mock_discord_interaction.user.id = 12345  # Not trusted

    # Call command that checks permissions
    # Assert permission denied message
```

### Test Message History

```python
@pytest.mark.asyncio
async def test_history(mock_discord_text_channel):
    # Customize history
    async def custom_history(limit=100):
        yield mock_discord_message
        yield mock_discord_message

    mock_discord_text_channel.history = custom_history

    # Use in test
    messages = [m async for m in mock_discord_text_channel.history(limit=10)]
    assert len(messages) == 2
```

### Test Bot Response

```python
@pytest.mark.asyncio
async def test_bot_reply(mock_discord_text_channel):
    msg = await mock_discord_text_channel.send("Bot says hello")

    assert msg.content == "Test message"
    mock_discord_text_channel.send.assert_called_with("Bot says hello")
```

## Full Example

```python
import pytest

@pytest.mark.asyncio
async def test_help_command(mock_discord_interaction, mock_discord_embed):
    """Test the /help command."""
    # Setup embed
    mock_discord_embed.title = "Help"
    mock_discord_embed.description = "Commands"
    mock_discord_embed.add_field(name="/help", value="Show help")

    # Simulate command execution
    await mock_discord_interaction.response.send_message(
        embed=mock_discord_embed,
        ephemeral=True
    )

    # Verify
    assert mock_discord_embed.title == "Help"
    assert len(mock_discord_embed.fields) == 1
    mock_discord_interaction.response.send_message.assert_called_once()

    # Check call arguments
    call_args = mock_discord_interaction.response.send_message.call_args
    assert call_args.kwargs['ephemeral'] is True
```

## Troubleshooting

**Problem:** Fixture not found
**Solution:** Ensure `tests/conftest.py` exists

**Problem:** AsyncMock not awaitable
**Solution:** Use `@pytest.mark.asyncio` decorator

**Problem:** Mock not returning expected value
**Solution:** Set `return_value` or customize with `side_effect`

**Problem:** Can't verify mock calls
**Solution:** Use `assert_called_once()`, `assert_called_with()`, etc.

## See Also

- `/docs/DISCORD_MOCKS.md` - Full documentation
- `/tests/test_discord_mocks.py` - Example tests
- `/tests/conftest.py` - Fixture definitions
