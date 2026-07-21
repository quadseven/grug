"""Tests for the Grug -> Hermes task-relay classifier.

The pure routing contract is covered here. Discord itself is mocked only at
the transport boundary so the exact instruction handed to Hermes is tested.
The long-running reply watcher still requires a live-bot smoke test.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.grugthink.bot import task_relay


@pytest.mark.parametrize(
    "statement",
    [
        "implement rate limiting for the login endpoint",
        "can you fix the bug in capture.py",
        "please build a new dashboard page",
        "refactor the auth module",
        "patch the flaky test",
        "write some code to parse this file",
        "write the code for that",
        "open a pr for this",
        "create a pull request please",
        "review this PR",
        "review the pr for macchina",
        "look at this diff",
        "can you do a code review",
        "audit the changes in grug",
        "peer review PR #720 in grug",
    ],
)
def test_looks_like_task_true(statement):
    assert task_relay.looks_like_task(statement) is True


@pytest.mark.parametrize(
    "statement",
    [
        "what is the capital of France",
        "grug is happy today",
        "is it true that mammoths are extinct",
        "tell me about your family",
        "how are you doing",
        "",
    ],
)
def test_looks_like_task_false(statement):
    assert task_relay.looks_like_task(statement) is False


@pytest.mark.parametrize(
    "statement",
    [
        "review this PR",
        "audit the changes in grug",
        "peer review PR #720 in grug",
        "implement the fix, then review the diff",
    ],
)
def test_review_requests_use_read_only_intent(statement):
    request = task_relay.classify_task(statement)

    assert request is not None and request.kind == "review"


def test_change_requests_use_change_intent():
    request = task_relay.classify_task("implement rate limiting in grug")

    assert request is not None and request.kind == "change"


def test_review_instruction_requests_peer_review_without_edits():
    request = task_relay.TaskRequest(kind="review", content="review PR #720 in grug")

    instruction = task_relay.format_relay_request(request, "Evan")

    assert "independent peer-review or audit tools" in instruction
    assert "Do not edit code" in instruction


def test_relay_instruction_sanitizes_requester_name():
    request = task_relay.TaskRequest(kind="change", content="fix auth in grug")

    instruction = task_relay.format_relay_request(request, "@everyone")

    assert "@everyone" not in instruction


def test_resolve_repo_finds_known_repo():
    assert task_relay.resolve_repo("implement X in the macchina repo") == "macchina"
    assert task_relay.resolve_repo("fix a thing in digital-ledger please") == "digital-ledger"
    assert task_relay.resolve_repo("do something in grug") == "grug"


def test_resolve_repo_prefers_longest_match():
    # "grug" is a substring-free word inside "grugthink" - the longest-first
    # sort must pick "grugthink", not misfire on the shorter "grug" entry.
    assert task_relay.resolve_repo("fix the bot in grugthink") == "grugthink"


def test_resolve_repo_returns_none_when_no_repo_named():
    assert task_relay.resolve_repo("implement a thing somewhere") is None


def test_repo_channels_cover_every_hermes_channel_key():
    # Sanity: every entry is a plausible Discord snowflake (17-19 digit int),
    # catching an obvious typo before it silently 404s at relay time.
    for repo, channel_id in task_relay.REPO_CHANNELS.items():
        assert isinstance(channel_id, int), repo
        assert 10**16 <= channel_id < 10**19, repo


# --- authorization / mention-sanitization (security model) ---


def test_is_authorized_fails_closed_when_unset(monkeypatch):
    monkeypatch.delenv("TASK_RELAY_ALLOWED_USER_IDS", raising=False)
    assert task_relay.is_authorized(123456) is False


def test_is_authorized_fails_closed_when_empty(monkeypatch):
    monkeypatch.setenv("TASK_RELAY_ALLOWED_USER_IDS", "")
    assert task_relay.is_authorized(123456) is False


def test_is_authorized_allows_listed_ids_only(monkeypatch):
    monkeypatch.setenv("TASK_RELAY_ALLOWED_USER_IDS", "111, 222,333")
    assert task_relay.is_authorized(111) is True
    assert task_relay.is_authorized(222) is True
    assert task_relay.is_authorized(333) is True
    assert task_relay.is_authorized(444) is False


def test_is_authorized_ignores_malformed_entries(monkeypatch):
    # A stray non-numeric entry must not crash the parse or accidentally
    # authorize everyone via a bad comparison.
    monkeypatch.setenv("TASK_RELAY_ALLOWED_USER_IDS", "111, not-a-number, 222")
    assert task_relay.is_authorized(111) is True
    assert task_relay.is_authorized(222) is True
    assert task_relay.is_authorized(0) is False


def test_get_hermes_user_id_unset_returns_none(monkeypatch):
    monkeypatch.delenv("HERMES_BOT_USER_ID", raising=False)
    assert task_relay._get_hermes_user_id() is None


def test_get_hermes_user_id_parses_valid_id(monkeypatch):
    monkeypatch.setenv("HERMES_BOT_USER_ID", "999888777")
    assert task_relay._get_hermes_user_id() == 999888777


def test_get_hermes_user_id_rejects_malformed(monkeypatch):
    monkeypatch.setenv("HERMES_BOT_USER_ID", "not-an-id")
    assert task_relay._get_hermes_user_id() is None


@pytest.mark.parametrize(
    "raw,must_not_contain",
    [
        ("please @everyone implement this", "@everyone"),
        ("URGENT @here fix now", "@here"),
        ("ping <@&123456789012345678> now", "<@&123456789012345678>"),
        ("hey <@98765432109876543> fix this", "<@98765432109876543>"),
    ],
)
def test_sanitize_for_relay_breaks_mention_syntax(raw, must_not_contain):
    sanitized = task_relay._sanitize_for_relay(raw)
    assert must_not_contain not in sanitized
    # The zero-width space must not delete any visible character - a
    # human reading the relayed message should see the same text.
    assert sanitized.replace("​", "") == raw


def test_sanitize_for_relay_leaves_ordinary_text_untouched():
    text = "implement rate limiting for the login endpoint"
    assert task_relay._sanitize_for_relay(text) == text


def test_long_hermes_response_is_split_with_mentions_disabled():
    chunks = task_relay.split_relay_response("Grug", "@everyone " + "x" * 4000)

    assert len(chunks) == 3
    assert all(len(chunk) <= task_relay.DISCORD_MESSAGE_LIMIT for chunk in chunks)
    assert all("@everyone" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_relay_sends_review_contract_to_hermes(monkeypatch):
    monkeypatch.setenv("TASK_RELAY_ALLOWED_USER_IDS", "111")
    original_channel = MagicMock()
    original_channel.send = AsyncMock()
    original_message = MagicMock()
    original_message.id = 720
    original_message.author.id = 111
    original_message.author.display_name = "Evan"
    original_message.channel = original_channel

    thread = MagicMock()
    relay_message = MagicMock()
    relay_message.create_thread = AsyncMock(return_value=thread)
    hermes_channel = MagicMock()
    hermes_channel.send = AsyncMock(return_value=relay_message)
    client = MagicMock()
    client.get_channel.return_value = hermes_channel

    watch = AsyncMock()
    monkeypatch.setattr(task_relay, "_watch_and_relay", watch)

    await task_relay.relay_to_hermes(client, original_message, "Grug", "peer review PR #720 in grug")

    relayed = hermes_channel.send.await_args.args[0]
    assert "Treat this as read-only review work" in relayed
    assert "independent peer-review or audit tools" in relayed
    assert hermes_channel.send.await_args.kwargs["allowed_mentions"].everyone is False
    watch.assert_awaited_once_with(client, thread, original_message, "Grug")


@pytest.mark.asyncio
async def test_thread_failure_reports_dispatched_task_without_inviting_retry(monkeypatch):
    monkeypatch.setenv("TASK_RELAY_ALLOWED_USER_IDS", "111")
    original_channel = MagicMock()
    original_channel.send = AsyncMock()
    original_message = MagicMock()
    original_message.id = 721
    original_message.author.id = 111
    original_message.author.display_name = "Evan"
    original_message.channel = original_channel

    relay_message = MagicMock()
    relay_message.id = 999
    relay_message.jump_url = "https://discord.example/messages/999"
    relay_message.create_thread = AsyncMock(side_effect=task_relay.discord.HTTPException(MagicMock(), "failed"))
    hermes_channel = MagicMock()
    hermes_channel.send = AsyncMock(return_value=relay_message)
    client = MagicMock()
    client.get_channel.return_value = hermes_channel

    watch = AsyncMock()
    monkeypatch.setattr(task_relay, "_watch_and_relay", watch)

    await task_relay.relay_to_hermes(client, original_message, "Grug", "fix auth in grug")

    notice = original_channel.send.await_args.args[0]
    assert "delivered task" in notice
    assert "Do not retry" in notice
    assert relay_message.jump_url in notice
    watch.assert_not_awaited()
