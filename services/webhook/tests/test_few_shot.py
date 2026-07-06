"""#538 (#361 slice 3): few-shot exemplar injection into Elder's prompt.

Pure tests: rendering bounds, cache round-trip shape, prompt append order
(EXAMPLES after RULES), best-effort fetch isolation, the ingest refresh,
sanitizer parity with best_practices, and strength-ranked class capping.
No LLM, no network, no store.
"""

from __future__ import annotations

from few_shot import (
    exemplars_block,
    exemplars_from_dicts,
    exemplars_from_rows,
    exemplars_to_dicts,
)
from ledger import LedgerRow, accepted_findings_by_class


def _row(
    pr: int,
    finding_class: str,
    severity: str = "HIGH",
    verdict: str = "fixed",
    finding: str = "synthetic finding",
) -> LedgerRow:
    return LedgerRow(
        repo="githumps/grug",
        pr=pr,
        reviewer="codex",
        severity=severity,
        finding_class=finding_class,
        finding=finding,
        verdict=verdict,
    )


# --- rendering ---------------------------------------------------------------


def test_block_empty_when_no_exemplars():
    assert exemplars_block([]) == ""


def test_block_renders_class_severity_finding_and_pr():
    by_class = accepted_findings_by_class(
        [_row(7, "silent-failure", severity="CRITICAL", finding="swallowed drop")]
    )
    block = exemplars_block(exemplars_from_rows(by_class))
    assert "EXAMPLES" in block
    assert "silent-failure" in block
    assert "CRITICAL" in block
    assert "swallowed drop" in block
    assert "#7" in block


def test_block_excludes_false_positives_via_corpus_layer():
    by_class = accepted_findings_by_class(
        [
            _row(1, "correctness", finding="real bug"),
            _row(2, "correctness", verdict="false-positive", finding="fp noise"),
        ]
    )
    block = exemplars_block(exemplars_from_rows(by_class))
    assert "real bug" in block
    assert "fp noise" not in block


def test_block_bounded_by_max_chars_and_per_class():
    rows = [
        _row(i, f"class-{i % 4}", finding=f"finding {i} " + "x" * 80)
        for i in range(40)
    ]
    block = exemplars_block(
        exemplars_from_rows(accepted_findings_by_class(rows)), max_chars=500
    )
    assert len(block) <= 500


def test_block_sanitizes_newlines_in_findings():
    by_class = accepted_findings_by_class(
        [_row(1, "correctness", finding="line one\nline two\r\nline three")]
    )
    block = exemplars_block(exemplars_from_rows(by_class))
    # One exemplar = one line; embedded newlines must not fork the block.
    exemplar_lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(exemplar_lines) == 1


# --- cache round-trip ---------------------------------------------------------


def test_exemplar_dict_roundtrip():
    by_class = accepted_findings_by_class(
        [
            _row(1, "correctness", severity="HIGH", finding="a"),
            _row(2, "silent-failure", severity="CRITICAL", finding="b"),
        ]
    )
    exemplars = exemplars_from_rows(by_class)
    dicts = exemplars_to_dicts(exemplars)
    assert all(isinstance(d, dict) for d in dicts)
    restored = exemplars_from_dicts(dicts)
    assert restored == exemplars
    assert exemplars_block(restored) == exemplars_block(exemplars)


def test_exemplars_from_dicts_skips_malformed_and_warns(caplog):
    dicts = [
        {"class": "correctness", "severity": "HIGH", "finding": "ok", "pr": 1},
        {"severity": "HIGH"},  # missing load-bearing fields -> skipped
        "not-a-dict",
    ]
    with caplog.at_level("WARNING", logger="grug.few_shot"):
        restored = exemplars_from_dicts(dicts)  # type: ignore[arg-type]
    assert [e.finding_class for e in restored] == ["correctness"]
    # The WARNING is the point (stage-2 fix): rot must be visible, not
    # just skipped - deleting the log line must fail this test.
    assert "skipped=2 total=3" in caplog.text


# --- prompt injection order ---------------------------------------------------


def test_examples_append_after_practices_in_system_prompt():
    from llm_client import Hunk, _build_messages

    hunks = [Hunk(path="x.py", body="@@ -1 +1 @@\n+a = 1\n")]
    messages = _build_messages(
        hunks,
        "v1",
        None,
        None,
        None,
        team_practices="TEAM-LEARNED PRACTICES:\n- [c x1] rule (e.g. #1)",
        few_shot_examples="EXAMPLES OF ACCEPTED FINDINGS:\n- [c/HIGH] f (PR #1)",
    )
    system = messages[0]["content"]
    assert "TEAM-LEARNED PRACTICES" in system
    assert "EXAMPLES OF ACCEPTED FINDINGS" in system
    # RULES norms first, EXAMPLES shape last.
    assert system.index("TEAM-LEARNED PRACTICES") < system.index(
        "EXAMPLES OF ACCEPTED FINDINGS"
    )


def test_empty_examples_leave_prompt_byte_identical():
    from llm_client import Hunk, _build_messages

    hunks = [Hunk(path="x.py", body="@@ -1 +1 @@\n+a = 1\n")]
    base = _build_messages(hunks, "v1", None, None, None)
    with_empty = _build_messages(
        hunks, "v1", None, None, None, few_shot_examples=""
    )
    assert base[0]["content"] == with_empty[0]["content"]


def test_few_shot_block_best_effort_no_repo():
    from llm_client import _few_shot_block

    assert _few_shot_block(None) == ""
    assert _few_shot_block({"pr_number": 1}) == ""


# --- ingest refresh -----------------------------------------------------------


def test_ingest_refreshes_exemplars_beside_practices():
    from ingest_ledger import ingest_text

    line = (
        '{"ts": "t", "repo": "githumps/grug", "pr": 3, "reviewer": "codex",'
        ' "severity": "HIGH", "class": "correctness",'
        ' "finding": "exemplar seed", "verdict": "fixed"}'
    )
    puts: list[dict] = []
    practice_calls: list[tuple] = []
    exemplar_calls: list[tuple] = []
    result = ingest_text(
        line,
        put=puts.append,
        put_practices=lambda repo, p: practice_calls.append((repo, p)),
        put_exemplars=lambda repo, e: exemplar_calls.append((repo, e)),
    )
    assert result["ingested"] == 1
    assert practice_calls and practice_calls[0][0] == "githumps/grug"
    assert exemplar_calls and exemplar_calls[0][0] == "githumps/grug"
    assert any(d.get("finding") == "exemplar seed" for d in exemplar_calls[0][1])


def test_block_strips_control_chars_and_caps_item_length():
    """Sanitizer parity with the #527 sibling (#541 lesson): control chars
    never reach the SYSTEM prompt, and one oversized finding is capped -
    never allowed to blank the whole block."""
    by_class = accepted_findings_by_class(
        [
            _row(1, "correctness", finding="evil \x1b[31m ansi"),
            _row(2, "silent-failure", finding="y" * 5000),
        ]
    )
    block = exemplars_block(exemplars_from_rows(by_class))
    assert "\x1b" not in block
    assert "silent-failure" in block  # capped, not dropped
    assert "correctness" in block


def test_block_ranks_classes_by_strength_not_insertion_order():
    """Which classes survive max_classes must be strongest-first, never
    corpus-insertion order."""
    rows = [
        _row(1, "weak-class", severity="LOW"),
        _row(2, "strong-class", severity="CRITICAL"),
    ]
    block = exemplars_block(
        exemplars_from_rows(accepted_findings_by_class(rows)), max_classes=1
    )
    assert "strong-class" in block
    assert "weak-class" not in block
