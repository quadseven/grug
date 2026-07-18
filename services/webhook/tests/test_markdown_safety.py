"""Tests for markdown_safety.neutralize_mentions - the shared mention-
breaking helper used by both Teller's walkthrough renderer and /grug
ask's answer handler (#561) before splicing model-authored text into a
GitHub comment posted under the app's own installation-token authority.
"""

from __future__ import annotations

from markdown_safety import neutralize_mentions

_ZWSP = chr(0x200B)  # zero-width space; kept as a runtime chr() call, not
# a literal character, so this source file stays 7-bit ASCII on disk.


def test_neutralize_mentions_breaks_a_live_mention():
    out = neutralize_mentions("ping @cait about this")
    assert "@cait" not in out
    assert f"@{_ZWSP}cait" in out


def test_neutralize_mentions_breaks_every_mention_in_text():
    out = neutralize_mentions("@evan and @cait should both see this")
    assert "@evan" not in out
    assert "@cait" not in out
    assert f"@{_ZWSP}evan" in out
    assert f"@{_ZWSP}cait" in out


def test_neutralize_mentions_leaves_bare_at_sign_alone():
    """`@` not immediately followed by a word character isn't a mention
    GitHub's parser would recognize - no need to break it."""
    out = neutralize_mentions("the answer is 3 @ 5pm")
    assert out == "the answer is 3 @ 5pm"


def test_neutralize_mentions_empty_string():
    assert neutralize_mentions("") == ""
