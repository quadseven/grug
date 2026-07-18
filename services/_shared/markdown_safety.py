"""Neutralize model-authored text before it lands in a GitHub comment
posted under the app's own installation-token authority.

Extracted from personas/walkthrough/render.py (#554 peer review round 4,
codex) when a second caller (rerun.py's /grug ask handler, #561) needed
the exact same mention-breaking behavior - keeping one regex instead of
two copies drifting apart.
"""

from __future__ import annotations

import re

_MENTION_RE = re.compile(r"(?<!\w)@(?=\w)")


def neutralize_mentions(text: str) -> str:
    """Break `@user` into `@<ZWSP>user` before it reaches GitHub's markdown
    renderer. The comment posts with the app's OWN installation-token
    authority, so a live mention in model-authored (or repo-controlled
    path) text would notify a real GitHub user as if Grug itself pinged
    them - a prompt-injected diff can influence this text. Visually
    identical to a reader; GitHub's mention parser requires an unbroken
    `@word` token."""
    return _MENTION_RE.sub("@\u200b", text)
