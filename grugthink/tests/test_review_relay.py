"""Tests for the Grug -> real Elder verdict review-relay.

Only the pure, deterministic pieces (extract_pr_number, format_verdict,
_get_token) are covered here - fetch_elder_verdict/relay_review are live
GitHub API + Discord I/O and can only be meaningfully verified against
the real bots (see grug PR that introduced this module).
"""

import pytest

from src.grugthink.bot import review_relay
from src.grugthink.bot.review_relay import ElderVerdict


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("review PR #123 for macchina", 123),
        ("what did elder say about #4567", 4567),
        ("look at #1", 1),
        ("no pr number here", None),
        ("", None),
    ],
)
def test_extract_pr_number(statement, expected):
    assert review_relay.extract_pr_number(statement) == expected


def test_get_token_unset_returns_none(monkeypatch):
    monkeypatch.delenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", raising=False)
    assert review_relay._get_token() is None


def test_get_token_returns_configured_value(monkeypatch):
    monkeypatch.setenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", "ghp_fake_token")
    assert review_relay._get_token() == "ghp_fake_token"


def test_format_verdict_none_means_not_found_or_not_answerable():
    message = review_relay.format_verdict(None, "Grug", "macchina", 42)
    assert "Grug" in message
    assert "macchina" in message
    assert "42" in message


def test_format_verdict_still_running():
    verdict = ElderVerdict(conclusion=None, title=None, summary=None, html_url=None)
    message = review_relay.format_verdict(verdict, "Grug", "grug", 100)
    assert "still look" in message


def test_format_verdict_success():
    verdict = ElderVerdict(
        conclusion="success",
        title="Elder: 0 findings",
        summary="No issues found.",
        html_url="https://grug.lol/some/check",
    )
    message = review_relay.format_verdict(verdict, "Grug", "infra", 1851)
    assert "good hunt" in message
    assert "Elder: 0 findings" in message
    assert "No issues found." in message
    assert "https://grug.lol/some/check" in message


def test_format_verdict_failure():
    verdict = ElderVerdict(conclusion="failure", title=None, summary=None, html_url=None)
    message = review_relay.format_verdict(verdict, "Grug", "infra", 1851)
    assert "bad omen" in message


def test_format_verdict_unknown_conclusion_falls_back_to_raw_word():
    verdict = ElderVerdict(conclusion="cancelled", title=None, summary=None, html_url=None)
    message = review_relay.format_verdict(verdict, "Grug", "infra", 1851)
    assert "cancelled" in message


def test_format_verdict_trims_long_summary():
    long_summary = "x" * 1000
    verdict = ElderVerdict(conclusion="success", title=None, summary=long_summary, html_url=None)
    message = review_relay.format_verdict(verdict, "Grug", "infra", 1)
    assert len(message) < len(long_summary) + 200
    assert message.count("x") <= review_relay._SUMMARY_MAX_CHARS


def test_check_elder_names_matches_canonical_and_legacy():
    # Mirrors services/_shared/personas/tribe.py's CHECK_ELDER +
    # aliases - the canonical name must always be present.
    assert "Grug - Elder" in review_relay.CHECK_ELDER_NAMES
