"""Which model family a model name belongs to (grug#1437).

A model reviewing what its own family wrote shares that family's blind spots,
so Elder compares the family named in a pull request's footer with the family
of each review arm. The table is closed on purpose: the author's name comes
from an untrusted pull request body, and only a family from this table ever
reaches a check summary. A name it does not know has no family, which keeps
today's arm order.

For PRs opened by GitHub Apps (bots), a trusted source (grug#1553) maps
App logins to model families instead of using the Loom footer.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import ClassVar

# Shown when an arm's model cannot be placed, such as a router that picks its
# own model (`spark:warm-any`, `openrouter/free`).
UNKNOWN_FAMILY = "unknown"

_FAMILY_PATTERNS: ClassVar[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (family, re.compile(rf"(?<![a-z0-9])(?:{names})"))
    for family, names in (
        ("claude", "claude|opus|sonnet|haiku|fable"),
        ("gpt", "gpt|codex"),
        ("gemini", "gemini|gemma"),
        ("qwen", "qwen"),
        ("deepseek", "deepseek"),
        ("kimi", "kimi"),
        ("grok", "grok"),
        ("minimax", "minimax"),
        ("llama", "llama"),
        ("laguna", "laguna"),
        ("nemotron", "nemotron"),
        ("muse", "muse"),
    )
)

# Trusted source for App author family (grug#1553). Maps App logins to model families.
# The login must not be a free-text match: a human login that merely contains a
# model name must not gain a family. Only listed Apps get families assigned.
_APP_AUTHOR_FAMILIES: ClassVar[frozenset[tuple[str, str]]] = frozenset(
    tuple(pair.split(":", 1))
    for line in os.getenv("GRUG_APP_AUTHOR_FAMILIES", "").splitlines()
    for pair in line.strip().split(";")
    if line.strip() and ":" in pair
)


_env_app_families_cache: ClassVar[set[tuple[str, str]] | None] = None


def _load_app_author_families() -> set[tuple[str, str]]:
    global _env_app_families_cache
    if _env_app_families_cache is None:
        _env_app_families_cache = set(_APP_AUTHOR_FAMILIES)
    return _env_app_families_cache


def _get_app_author_family(app_login: str | None) -> str | None:
    """Get model family for an App author from trusted source (grug#1553).

    Returns None for:
    - None or empty app_login
    - app_login not in the trusted list
    - app_login lowercased version not in the trusted list
    """
    if not app_login:
        return None

    app_login_lower = app_login.lower()
    families = _load_app_author_families()

    for trusted_login, family in families:
        if app_login_lower == trusted_login.lower():
            return family

    return None


def app_author_family(app_login: str | None) -> str | None:
    """Get model family for an App author from trusted source (grug#1553).

    Returns None for:
    - None or empty app_login
    - app_login not in the trusted list
    - app_login lowercased version not in the trusted list
    This is the trusted source that maps App logins to model families,
    used when a GitHub App opens a pull request (grug#1553). A human login
    that merely contains a model name must not gain a family.
    """
    return _get_app_author_family(app_login)


def model_family(model: str | None) -> str | None:
    """The family of `model`, or None when the name is empty or unknown."""
    name = (model or "").lower()
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(name):
            return family
    return None


@dataclass(frozen=True, slots=True)
class ReviewFamilies:
    """Who wrote a pull request and who reviewed it, as model families.

    `reviewers` holds one family per distinct family that answered: a staged
    review can reach a different arm for each cohort."""

    author: str
    reviewers: tuple[str, ...]

    @property
    def reviewer(self) -> str:
        return ", ".join(self.reviewers)

    @property
    def same_family(self) -> bool:
        return self.author in self.reviewers


def review_families(
    author: str | None, models_used: tuple[str, ...],
) -> ReviewFamilies | None:
    """The families of one finished review, or None with no known author."""
    if not author:
        return None
    reviewers = dict.fromkeys(
        model_family(model) or UNKNOWN_FAMILY for model in models_used
    )
    return ReviewFamilies(
        author=author, reviewers=tuple(sorted(reviewers)) or (UNKNOWN_FAMILY,),
    )