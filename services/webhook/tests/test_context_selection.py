"""CONTEXT.md reaches review by relevance, within a fixed budget (grug#1498)."""

from __future__ import annotations

from personas.code_reviewer.repo_docs import _MAX_CONTEXT_CHARS, build_repo_docs_context
from review_evidence import Hunk

INTRO = "# Glossary\n\nIntro paragraph every review keeps."
FILLER = "\n\n".join(
    f"**Filler{i}**: routine wording about unrelated material number {i}." * 3
    for i in range(400)
)
RULE = "**Lantern**: a kiln entry the wording rule names."
DOC = f"{INTRO}\n\n{FILLER}\n\n{RULE}"
TABLE_DOC = (
    f"{INTRO}\n\n{FILLER}\n\n| Term | Meaning |\n|---|---|\n"
    "| Other | nothing here |\n| Lantern | a kiln entry |"
)
MATCHING = [Hunk(path="web/page.html", body="+<p>Lantern kiln</p>")]


def _block(doc: str | None, hunks: list[Hunk] = MATCHING) -> str | None:
    return build_repo_docs_context(
        fetch_file=lambda _path: doc, list_dir=lambda _path: None, hunks=hunks
    )


def test_a_short_document_arrives_whole() -> None:
    block = _block("# Short\n\nOne rule.")
    assert block is not None
    assert "One rule." in block
    assert "truncated" not in block


def test_an_absent_document_adds_no_section() -> None:
    assert _block(None) is None


def test_a_relevant_entry_past_the_cap_reaches_the_review() -> None:
    assert DOC.index(RULE) > _MAX_CONTEXT_CHARS
    block = _block(DOC)
    assert block is not None
    assert RULE in block
    assert "Intro paragraph every review keeps." in block


def test_a_relevant_table_row_keeps_its_header() -> None:
    block = _block(TABLE_DOC)
    assert block is not None
    assert "| Term | Meaning |\n|---|---|\n| Lantern | a kiln entry |" in block


def test_a_vocabulary_rule_survives_with_no_shared_token() -> None:
    rule = "**Quartz**: say stone. _Avoid_: crystal."
    block = _block(f"{INTRO}\n\n{FILLER}\n\n{rule}")
    assert block is not None
    assert rule in block


def test_the_budget_holds_and_the_notice_shows() -> None:
    block = _block(DOC)
    assert block is not None
    body = block.split("---\n", 1)[1]
    assert len(body) <= _MAX_CONTEXT_CHARS + 200
    assert f"truncated to {_MAX_CONTEXT_CHARS} chars" in block
    assert "Filler399" not in block


def test_selection_is_deterministic_and_keeps_document_order() -> None:
    assert _block(DOC) == _block(DOC)
    block = _block(DOC)
    assert block is not None
    assert block.index("Intro paragraph") < block.index(RULE)
