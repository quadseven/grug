"""Tests for context-sensitive glossary selection when bounded."""

from __future__ import annotations

import os

from pathlib import Path

import pytest
from pytest import fixture

from llm_client import review_diff
from personas.code_reviewer.repo_docs import build_repo_docs_context
from review_evidence import Hunk, assemble_evidence

REPO_ROOT = Path(__file__).resolve().parents[3]


def _build_context_with_hunks(hunks):
    def fetch(path: str) -> str | None:
        target = REPO_ROOT / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    block = build_repo_docs_context(
        fetch_file=fetch,
        list_dir=lambda _path: None,
        hunks=hunks,
    )
    return block


def test_relevant_glossary_entry_before_boundary_reaches_prompt():
    """A glossary entry that appears before the 8000-char boundary is included."""
    CONTEXT_LINE = "_Avoid_: code keys on user-facing surfaces."
    hunks = [
        Hunk(
            path="services/_shared/personas/code_reviewer/dispatch.py",
            body="--- a/services/_shared/personas/code_reviewer/dispatch.py\n+++ b/...",
        )
    ]

    block = _build_context_with_hunks(hunks)
    assert block is not None
    assert CONTEXT_LINE in block


def test_relevant_glossary_entry_after_boundary_reaches_prompt():
    """A glossary entry that appears after the 8000-char boundary can still reach the prompt when relevant to changed paths."""
    hunks = [
        Hunk(
            path="services/_shared/personas/code_reviewer/persona.py",
            body="--- a/services/_shared/personas/code_reviewer/persona.py\n+++ b/...",
        )
    ]

    block = _build_context_with_hunks(hunks)
    assert block is not None

    CONTEXT_LINE = "_Avoid_: code keys on user-facing surfaces."
    JARGON_LINE = 'Industry jargon "Definition of Ready" / "DoR" is **not** used'

    assert CONTEXT_LINE in block
    assert JARGON_LINE in block


def test_absent_document_returns_none():
    """If CONTEXT.md does not exist, returns None instead of an error."""
    hunks = [
        Hunk(
            path="nonexistent/file.py",
            body="--- a/nonexistent/file.py\n+++ b/...",
        )
    ]

    block = _build_context_with_hunks(hunks)
    assert block is None


def test_deterministic_selection_under_repeated_calls():
    """The same hunks produce the same context block every time."""
    hunks = [
        Hunk(
            path="services/_shared/personas/code_reviewer/persona.py",
            body="--- a/services/_shared/personas/code_reviewer/persona.py\n+++ b/...",
        )
    ]

    block1 = _build_context_with_hunks(hunks)
    block2 = _build_context_with_hunks(hunks)

    assert block1 is not None
    assert block2 is not None
    assert block1 == block2


def test_total_budget_enforcement():
    """The assembled context never exceeds the 8000-char limit."""
    hunks = [
        Hunk(
            path="services/_shared/personas/code_reviewer/persona.py",
            body="--- a/services/_shared/personas/code_reviewer/persona.py\n+++ b/...",
        )
    ]

    block = _build_context_with_hunks(hunks)
    assert block is not None

    CONTEXT_LINE = "_Avoid_: code keys on user-facing surfaces."
    assert CONTEXT_LINE in block
    assert len(block) <= 8000


def test_visible_omission_notice():
    """The omission notice is present when parts of CONTEXT.md are not included."""
    hunks = [
        Hunk(
            path="services/_shared/personas/code_reviewer/persona.py",
            body="--- a/services/_shared/personas/code_reviewer/persona.py\n+++ b/...",
        )
    ]

    block = _build_context_with_hunks(hunks)
    assert block is not None

    OMISSION_NOTICE = "CONTEXT.md continues (omitted to stay within review context budget)"
    assert OMISSION_NOTICE in block


@pytest.mark.skipif(
    "GRUG_LIVE_LLM" not in os.environ,
    reason="live LLM backend not enabled (set GRUG_LIVE_LLM=1)",
)
def test_relevant_glossary_from_context_catches_later_rule():
    """A context-sensitive selection allows the jargon rule (past the old boundary) to reach the LLM when relevant."""
    hunks = [
        Hunk(
            path="services/_shared/personas/code_reviewer/persona.py",
            body="--- a/services/_shared/personas/code_reviewer/persona.py\n+++ b/...",
        )
    ]

    block = _build_context_with_hunks(hunks)
    assert block is not None

    JARGON_LINE = 'Industry jargon "Definition of Ready" / "DoR" is **not** used'
    assert JARGON_LINE in block

    evidence = assemble_evidence(
        [Hunk(path="services/_shared/personas/code_reviewer/persona.py", body="...")],
        repo_docs_context=block,
    )
    response = review_diff(evidence, installation_id=0)
    if response.kind in ("all_failed", "subscription_limit_reached"):
        pytest.skip(f"no reachable LLM backend: {response.kind} {response.error}")

    assert response.kind == "reviewed"
    messages = " ".join(f.message.lower() for f in response.findings)
    assert "definition of ready" in messages or "dor" in messages


def test_unrelated_document_stays_bounded():
    """A long CONTEXT.md with unrelated changes remains bounded, and only relevant entries are included."""
    hunks = [
        Hunk(
            path="services/_shared/personas/tpm/dor_checks.py",
            body="--- a/services/_shared/personas/tpm/dor_checks.py\n+++ b/...",
        )
    ]

    block = _build_context_with_hunks(hunks)
    assert block is not None

    assert len(block) <= 8000

    OMISSION_NOTICE = "CONTEXT.md continues (omitted to stay within review context budget)"
    assert OMISSION_NOTICE in block

    JARGON_LINE = 'Industry jargon "Definition of Ready" / "DoR" is **not** used'
    assert JARGON_LINE in block
