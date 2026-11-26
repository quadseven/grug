"""Which model family a model name belongs to (grug#1437).

A model reviewing what its own family wrote shares that family's blind spots,
so Elder compares the family named in a pull request's footer with the family
of each review arm. The table is closed on purpose: the author's name comes
from an untrusted pull request body, and only a family from this table ever
reaches a check summary. A name it does not know has no family, which keeps
today's arm order.

For PRs opened by GitHub Apps (bots), configuration names the Apps whose
footers are trusted and maps single-engine App logins to model families.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Shown when an arm's model cannot be placed, such as a router that picks its
# own model (`spark:warm-any`, `openrouter/free`).
UNKNOWN_FAMILY = "unknown"

_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (family, re.compile(rf"(?<![a-z0-9])(?:{names})"))
    for family, names in (
        ("claude", "claude|opus|sonnet|haiku|fable"),
        ("gpt", "gpt|codex"),
        ("gemini", "gemini|gemma"),
        ("qwen", "qwen"),
        ("deepseek", "deepseek"),
        ("kimi", "kimi"),
        ("glm", "glm"),
        ("grok", "grok"),
        ("minimax", "minimax"),
        ("llama", "llama"),
        ("laguna", "laguna"),
        ("nemotron", "nemotron"),
        ("longcat", "longcat"),
        ("muse", "muse"),
    )
)

# Trusted source for App author families (grug#1553). GRUG_APP_AUTHOR_FAMILIES
# lists `login:family` pairs, separated by `;` or newlines, for example
# `lane-app[bot]:claude;other-app[bot]:gpt`. Logins match exactly, ignoring case.
_APP_AUTHOR_FAMILIES_ENV = "GRUG_APP_AUTHOR_FAMILIES"
_KNOWN_FAMILIES = frozenset(family for family, _ in _FAMILY_PATTERNS)


def app_author_family(login: str | None) -> str | None:
    """The family listed for the GitHub App `login`, or None when unlisted.

    The table is parsed from the environment on each call, so a changed value
    (or a test's monkeypatch) takes effect without a reload."""
    wanted = (login or "").strip().lower()
    if not wanted:
        return None
    raw = os.environ.get(_APP_AUTHOR_FAMILIES_ENV, "")
    table: dict[str, str] = {}
    for pair in raw.replace("\n", ";").split(";"):
        name, sep, family = pair.partition(":")
        family = family.strip().lower()
        # Only a family the closed table knows may reach a check summary.
        if sep and name.strip() and family in _KNOWN_FAMILIES:
            table[name.strip().lower()] = family
    return table.get(wanted)


def app_uses_author_footer(login: str | None) -> bool:
    """Whether the exact App login is on the comma-separated footer allowlist."""
    wanted = (login or "").strip().lower()
    return bool(wanted) and any(
        wanted == name.strip().lower()
        for name in os.getenv("GRUG_APP_FOOTER_AUTHORS", "").split(",")
    )


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
