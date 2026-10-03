"""Tests for repo_docs.build_repo_docs_context (#903).

CONTEXT.md + docs/adr/ surfaced as a DISTINCT review-context block
(### REPO DOCS), separate from team-practices/Lore and from the #674
agent-guideline block. Fail-safe: any fetch problem degrades to None.
"""

from __future__ import annotations

from types import SimpleNamespace

from llm_client import Hunk, _build_review_parts
from personas.code_reviewer import repo_docs
from personas.code_reviewer.repo_docs import build_repo_docs_context

CONTEXT_TEXT = """# CONTEXT.md — grug domain glossary

| **Hunt Plan** | Product name for Chief's gate. Industry jargon "Definition of Ready" / "DoR" is **not** used on product surfaces. |
"""

ADR_REGISTRY = """# ADR-0010: Persona dispatch registry

Decisions: adding a persona = one spec entry + one dispatch module + default config keys, no dispatcher edits.
"""

ADR_SQS = """# ADR-0004: Rerun via SQS

Decisions: reruns go through the SQS FIFO.
"""


def _hunk(path):
    return SimpleNamespace(file_path=path, body="diff")


def _fetcher(files):
    def fetch(path):
        if path in files:
            return files[path]
        return None

    return fetch


def _lister(entries):
    def list_dir(path):
        if path == "docs/adr/":
            return list(entries)
        return None

    return list_dir


def test_context_md_included_when_present():
    ctx = build_repo_docs_context(
        fetch_file=_fetcher({"CONTEXT.md": CONTEXT_TEXT}),
        list_dir=_lister([]),
        hunks=[_hunk("services/webhook/app.py")],
    )
    assert ctx is not None
    assert "### REPO DOCS" in ctx
    assert "Hunt Plan" in ctx
    assert "CONTEXT.md" in ctx


def test_context_md_truncated_with_notice():
    long_text = "x" * (repo_docs._MAX_CONTEXT_CHARS + 100)
    ctx = build_repo_docs_context(
        fetch_file=_fetcher({"CONTEXT.md": long_text}),
        list_dir=_lister([]),
        hunks=[_hunk("a.py")],
    )
    assert ctx is not None
    assert "truncated" in ctx
    assert long_text not in ctx  # the full file must not be embedded
    # the capped content plus notice is present
    assert "... (truncated to 8000 chars; CONTEXT.md continues)" in ctx


def test_missing_context_md_still_renders_adrs():
    ctx = build_repo_docs_context(
        fetch_file=_fetcher(
            {"docs/adr/0010-persona-dispatch-registry.md": ADR_REGISTRY}
        ),
        list_dir=_lister(["0010-persona-dispatch-registry.md"]),
        hunks=[_hunk("services/_shared/personas/code_reviewer/dispatch.py")],
    )
    assert ctx is not None
    assert "0010-persona-dispatch-registry" in ctx
    assert "CONTEXT.md" not in ctx.split("---")[0]


def test_adr_scoped_by_changed_paths():
    ctx = build_repo_docs_context(
        fetch_file=_fetcher(
            {
                "docs/adr/0010-persona-dispatch-registry.md": ADR_REGISTRY,
                "docs/adr/0004-rerun-via-sqs.md": ADR_SQS,
            }
        ),
        list_dir=_lister(
            ["0010-persona-dispatch-registry.md", "0004-rerun-via-sqs.md"]
        ),
        hunks=[_hunk("services/_shared/personas/code_reviewer/dispatch.py")],
    )
    assert ctx is not None
    assert "0010-persona-dispatch-registry" in ctx
    assert "0004-rerun-via-sqs" not in ctx


def test_no_docs_anywhere_returns_none():
    ctx = build_repo_docs_context(
        fetch_file=_fetcher({}),
        list_dir=_lister([]),
        hunks=[_hunk("a.py")],
    )
    assert ctx is None


def test_fetch_exception_degrades_to_none():
    def boom(path):
        raise RuntimeError("api down")

    ctx = build_repo_docs_context(
        fetch_file=boom, list_dir=_lister([]), hunks=[_hunk("a.py")]
    )
    assert ctx is None


def test_list_dir_exception_degrades_to_none():
    def boom(path):
        raise RuntimeError("api down")

    ctx = build_repo_docs_context(
        fetch_file=_fetcher({"CONTEXT.md": CONTEXT_TEXT}),
        list_dir=boom,
        hunks=[_hunk("a.py")],
    )
    # CONTEXT.md alone is still useful; only the ADR half degrades
    assert ctx is not None
    assert "Hunt Plan" in ctx


def test_block_header_is_distinct():
    ctx = build_repo_docs_context(
        fetch_file=_fetcher({"CONTEXT.md": CONTEXT_TEXT}),
        list_dir=_lister([]),
        hunks=[_hunk("a.py")],
    )
    assert ctx.startswith("### REPO DOCS")
    assert "authoritative" in ctx


def test_build_review_parts_accepts_repo_docs_context():
    # Threading contract: _build_review_parts takes the new kwarg.
    hunks = [Hunk(path="a.py", body="diff")]
    parts, _ = _build_review_parts(hunks, repo_docs_context="docs!")
    assert any("### REPO DOCS" in p and "docs!" in p for p in parts)


def test_build_review_parts_renders_after_ci_status():
    hunks = [Hunk(path="a.py", body="diff")]
    parts, _ = _build_review_parts(hunks, ci_context="ci!", repo_docs_context="docs!")
    joined = "\n".join(parts)
    assert joined.index("### CI STATUS") < joined.index("### REPO DOCS")


def test_build_review_parts_omits_block_when_none():
    hunks = [Hunk(path="a.py", body="diff")]
    parts_none, _ = _build_review_parts(hunks, repo_docs_context=None)
    parts_absent, _ = _build_review_parts(hunks)
    assert parts_none == parts_absent
    assert not any("### REPO DOCS" in p for p in parts_none)


def test_hunk_path_attribute_shape_accepted():
    # llm_client.Hunk uses .path; dispatch diff-hunks use .file_path.
    ctx = build_repo_docs_context(
        fetch_file=_fetcher({"CONTEXT.md": CONTEXT_TEXT}),
        list_dir=_lister([]),
        hunks=[Hunk(path="a.py", body="diff")],
    )
    assert ctx is not None
