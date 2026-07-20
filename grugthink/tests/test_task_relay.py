"""Tests for the Grug -> Hermes task-relay classifier.

Only the pure, deterministic pieces (looks_like_task, resolve_repo) are
covered here - relay_to_hermes/_watch_and_relay are live Discord I/O and
can only be meaningfully verified against the real bots (see grug PR that
introduced this module for the live-verification plan).
"""

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
