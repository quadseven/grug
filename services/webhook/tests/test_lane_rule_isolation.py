"""One deterministic lane-work rule raising must not abort the others (#1834).

Seen live: the sanitiser rule raised on a replacement literal with an
escape, the dispatcher swallowed the error for the whole pass, and every
other deterministic rule was skipped while the pass still read clear.
"""

from __future__ import annotations

import logging

import pytest
from personas.code_reviewer import dispatch, lane_work
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

_DIFF = (
    "diff --git a/notes.md b/notes.md\nnew file mode 100644\n"
    "index 0000000..1111111\n--- /dev/null\n+++ b/notes.md\n"
    "@@ -0,0 +1,1 @@\n+hello\n"
)


def _call(failed=None):
    return lane_work.lane_work_findings(
        diff_text=_DIFF, hunks=parse_diff(_DIFF), file_contents={},
        base_line_counts={}, boxes=(), pr_title="Notes", pr_body="", failed=failed,
    )


def _finding(rule: str) -> Finding:
    return Finding(
        file="notes.md", line=1, severity="medium", rule_name=rule,
        message="m", suggestion=None, effort="quick-win",
    )


def _boom(*_args, **_kwargs):
    raise ValueError("bad escape")


def test_a_rule_that_raises_does_not_stop_the_rules_after_it(monkeypatch):
    monkeypatch.setattr(lane_work, "scan_sanitizers", _boom)
    monkeypatch.setattr(
        lane_work, "scan_fabricated_results", lambda *_a, **_k: [_finding("after-the-failure")],
    )
    failed: list[tuple[str, str]] = []
    found = _call(failed)
    assert [f.rule_name for f in found] == ["after-the-failure"]
    assert failed == [("sanitizers", "ValueError")]


def test_a_failed_rule_is_logged_once_by_name_and_kind(monkeypatch, caplog):
    monkeypatch.setattr(lane_work, "scan_sanitizers", _boom)
    with caplog.at_level(logging.WARNING):
        _call()
    records = [r for r in caplog.records if r.getMessage() == "lane_work_rule_failed"]
    assert len(records) == 1
    assert records[0].rule == "sanitizers"
    assert records[0].kind == "ValueError"


def test_a_generator_rule_that_raises_midway_is_isolated(monkeypatch):
    def lazy(*_a, **_k):
        yield _finding("first")
        raise RuntimeError("late")

    monkeypatch.setattr(lane_work, "scan_wrong_target", lazy)
    failed: list[tuple[str, str]] = []
    _call(failed)
    assert failed == [("wrong-target", "RuntimeError")]


def test_a_pass_with_no_failure_reports_none():
    failed: list[tuple[str, str]] = []
    _call(failed)
    assert failed == []


def test_the_summary_names_a_rule_that_could_not_run(monkeypatch):
    monkeypatch.setattr(lane_work, "scan_sanitizers", _boom)
    skipped: list[tuple[str, str]] = []
    evaluation = CodeReviewEvaluation(findings=(), conclusion="success")
    dispatch._merge_lane_work(
        evaluation, parse_diff(_DIFF), _DIFF, {}, {}, {}, correctness_only=False,
        skipped=skipped,
    )
    note = dispatch._analyzer_skip_note(skipped)
    assert "lane-work:sanitizers (ValueError)" in note
    assert "without them" in note


def test_a_rule_that_does_not_fail_adds_no_summary_note():
    skipped: list[tuple[str, str]] = []
    evaluation = CodeReviewEvaluation(findings=(), conclusion="success")
    dispatch._merge_lane_work(
        evaluation, parse_diff(_DIFF), _DIFF, {}, {}, {}, correctness_only=False,
        skipped=skipped,
    )
    assert skipped == []
    assert dispatch._analyzer_skip_note(skipped) == ""
