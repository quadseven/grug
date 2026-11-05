"""Tests for bot_identity (#1630).

A self-hosted App runs under its own slug. GRUG_BOT_SLUG names it, and every
own-comment check (thread resolution, Sentinel, the autofix commit author)
follows it, with `grug-tribe` only as the default.
"""
from __future__ import annotations

import bot_identity
import pytest
from personas.code_reviewer import thread_resolver as tr
from personas.sentinel import webhook_dispatch as wd

from tests.test_code_reviewer_thread_resolver import (  # noqa: F401
    _comment,
    _Gh,
    _thread,
    run,
)


def test_default_slug_is_grug_tribe(monkeypatch):
    monkeypatch.delenv("GRUG_BOT_SLUG", raising=False)
    assert bot_identity.bot_slug() == "grug-tribe"
    assert bot_identity.bot_login() == "grug-tribe[bot]"


def test_env_overrides_slug(monkeypatch):
    monkeypatch.setenv("GRUG_BOT_SLUG", " other-bot ")
    assert bot_identity.bot_slug() == "other-bot"
    assert bot_identity.bot_login() == "other-bot[bot]"


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("GRUG_BOT_SLUG", "  ")
    assert bot_identity.bot_slug() == "grug-tribe"


@pytest.mark.parametrize("login", ["other-bot", "other-bot[bot]", "Other-Bot[bot]", "OTHER-BOT[BOT]"])
def test_is_own_login_matches_graphql_and_rest_forms(monkeypatch, login):
    monkeypatch.setenv("GRUG_BOT_SLUG", "other-bot")
    assert bot_identity.is_own_login(login)


@pytest.mark.parametrize("login", [None, "", "grug-tribe", "grug-tribe[bot]", "other-bot-x"])
def test_is_own_login_rejects_others(monkeypatch, login):
    monkeypatch.setenv("GRUG_BOT_SLUG", "other-bot")
    assert not bot_identity.is_own_login(login)


def test_is_grug_follows_the_configured_slug(monkeypatch):
    monkeypatch.setenv("GRUG_BOT_SLUG", "other-bot")
    assert tr.is_grug("other-bot")
    assert tr.is_grug("other-bot[bot]")
    assert not tr.is_grug("grug-tribe")


def test_thread_resolution_under_another_slug(monkeypatch, run):  # noqa: F811 - pytest fixture
    """With GRUG_BOT_SLUG=other-bot, a fixed thread the other-bot App opened
    resolves, and one opened by grug-tribe is left alone as foreign."""
    monkeypatch.setenv("GRUG_BOT_SLUG", "other-bot")
    own = _thread("OWN", comments=[_comment(author="other-bot")])
    foreign = _thread("FOREIGN", comments=[_comment(author="grug-tribe")])
    gh = _Gh([own, foreign])
    run(gh)
    assert gh.resolved == ["OWN"]


def test_sentinel_own_bot_follows_the_configured_slug(monkeypatch):
    monkeypatch.setenv("GRUG_BOT_SLUG", "other-bot")
    own = {"user": {"type": "Bot", "login": "other-bot[bot]"}}
    hosted = {"user": {"type": "Bot", "login": "grug-tribe[bot]"}}
    human = {"user": {"type": "User", "login": "other-bot[bot]"}}
    assert wd._is_own_bot(own, "1")
    assert not wd._is_own_bot(hosted, "1")
    assert not wd._is_own_bot(human, "1")


def test_autofix_commit_identity_follows_the_configured_slug(monkeypatch):
    monkeypatch.setenv("GRUG_BOT_SLUG", "other-bot")
    assert bot_identity.commit_identity() == {
        "name": "other-bot[bot]",
        "email": "other-bot[bot]@users.noreply.github.com",
    }
