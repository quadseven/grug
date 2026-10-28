"""Regression and live-verification tests for #903 (C5, C6).

C6: each test here fails against the pre-fix tree, where Elder never read
CONTEXT.md. The pre-fix prompt is reproduced by assembling evidence with no
`repo_docs_context`, the only input the pre-fix code had.

C5: `test_context_md_contradiction_caught` needs a reachable LLM backend. It
is skipped unless `GRUG_LIVE_LLM=1`, and skipped again when every backend
fails, so CI without a backend stays green. Requires only `pytest` and
`boto3`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from llm_client import review_diff
from personas.code_reviewer.repo_docs import build_repo_docs_context
from review_evidence import Hunk, assemble_evidence

REPO_ROOT = Path(__file__).resolve().parents[3]
# Inside the 8000-char cap, so the model really sees it.
CONTEXT_LINE = "_Avoid_: code keys on user-facing surfaces."
# Further down CONTEXT.md, past the cap today (see the xfail below).
JARGON_LINE = 'Industry jargon "Definition of Ready" / "DoR" is **not** used'
CONTRADICTING_DIFF = (
    "--- a/web/pricing.html\n+++ b/web/pricing.html\n@@ -1,1 +1,2 @@\n"
    " <h2>How it works</h2>\n"
    "+<p>Our code_reviewer persona checks every pull request.</p>\n"
)


def _real_context_block() -> str:
    def fetch(path: str) -> str | None:
        target = REPO_ROOT / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    block = build_repo_docs_context(
        fetch_file=fetch,
        list_dir=lambda _path: None,
        hunks=[Hunk(path="web/pricing.html", body=CONTRADICTING_DIFF)],
    )
    assert block is not None, "repo CONTEXT.md must be readable from the tree"
    return block


def _prompt_text(repo_docs_context: str | None) -> str:
    hunks = [Hunk(path="web/pricing.html", body=CONTRADICTING_DIFF)]
    parts, _ = assemble_evidence(
        hunks, repo_docs_context=repo_docs_context
    ).reviewer_view()
    return "\n".join(parts)


def test_real_context_md_still_carries_the_rule():
    # Guards the fixture: if CONTEXT.md drops the line, C5 proves nothing.
    text = (REPO_ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    assert CONTEXT_LINE in text
    assert JARGON_LINE in text


def test_real_context_line_reaches_reviewer_prompt():
    assert CONTEXT_LINE in _prompt_text(_real_context_block())


@pytest.mark.xfail(
    strict=True,
    reason="CONTEXT.md outgrew _MAX_CONTEXT_CHARS; the jargon rule is truncated away",
)
def test_jargon_rule_survives_the_context_cap():
    assert JARGON_LINE in _prompt_text(_real_context_block())


def test_pre_fix_prompt_lacks_the_context_line():
    # The pre-fix behavior: no repo docs input, so the rule is invisible.
    assert CONTEXT_LINE not in _prompt_text(None)


@pytest.mark.skipif(
    os.environ.get("GRUG_LIVE_LLM") != "1",
    reason="live LLM backend not enabled (set GRUG_LIVE_LLM=1)",
)
def test_context_md_contradiction_caught():
    evidence = assemble_evidence(
        [Hunk(path="web/pricing.html", body=CONTRADICTING_DIFF)],
        repo_docs_context=_real_context_block(),
    )
    response = review_diff(evidence, installation_id=0)
    if response.kind in ("all_failed", "subscription_limit_reached"):
        pytest.skip(f"no reachable LLM backend: {response.kind} {response.error}")
    assert response.kind == "reviewed"
    messages = " ".join(f.message.lower() for f in response.findings)
    assert "code_reviewer" in messages or "elder" in messages
