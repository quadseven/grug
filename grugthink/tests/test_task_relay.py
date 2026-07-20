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
