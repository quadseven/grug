"""Checked committable suggestions (#1149, part of #1143).

A ```suggestion block replaces the anchored line in one click, so every block
must change that line and, for Python, leave the file parseable. These tests
pin the three sources (the model's field, a promoted prose fix, ruff's own
fix) and the dispatch seam that renders the result.
"""
from __future__ import annotations

from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.lint import _map_results
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
from personas.code_reviewer.suggestion import checked_suggestion, ruff_fix_line

_NULL_DEREF_FILE = (
    "def pick(options, wanted):\n"
    "    option = next(o for o in options if o.name == wanted)\n"
    "    return option.value\n"
)
_NULL_DEREF_MESSAGE = (
    "next() raises StopIteration when nothing matches; pass a default and "
    "guard the None: `option = next((o for o in options if ...), None)`"
)


def _finding(
    message: str = "m", suggestion: str | None = None, *,
    file: str = "app/pick.py", line: int = 2, rule: str = "null-deref",
) -> Finding:
    return Finding(
        file=file, line=line, severity="high", rule_name=rule,
        message=message, suggestion=suggestion,
    )


def test_prose_replacement_is_promoted_to_suggestion():
    """The live miss: the exact one-line fix sat in the message as a code
    span, the suggestion field was empty, and no block was offered."""
    out = checked_suggestion(_finding(_NULL_DEREF_MESSAGE), 2, _NULL_DEREF_FILE)
    assert out == "    option = next((o for o in options if ...), None)"


def test_suggestion_that_breaks_parse_renders_as_prose():
    """A one-click commit must never leave a Python file unparseable."""
    broken = _finding(suggestion="    option = next((o for o in options, None)")
    assert checked_suggestion(broken, 2, _NULL_DEREF_FILE) is None

    evaluation = CodeReviewEvaluation(
        findings=(broken,), conclusion="failure", degraded_reason=None,
    )
    result, _ = cr_dispatch._build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
        file_contents={"app/pick.py": _NULL_DEREF_FILE},
    )
    assert result is not None
    body = result.comments[0].body
    assert "```suggestion" not in body
    assert "next((o for o in options, None)" in body


def test_ruff_safe_fix_becomes_suggestion():
    """Ruff's safe one-line fix rides through lint mapping and the check."""
    text = "def read(cfg):\n    return getattr(cfg, \"name\")\n"
    results = [{
        "filename": "/tmp/grug-lint-x/app/read.py",
        "code": "B009",
        "message": "Do not call `getattr` with a constant attribute value",
        "location": {"row": 2, "column": 12},
        "fix": {
            "applicability": "safe",
            "message": "Replace `getattr` with attribute access",
            "edits": [{
                "content": "cfg.name",
                "location": {"row": 2, "column": 12},
                "end_location": {"row": 2, "column": 32},
            }],
        },
    }]
    (finding,) = _map_results(
        results, "/tmp/grug-lint-x/", {"app/read.py": {2}}, {"app/read.py": text},
    )
    assert finding.suggestion == "    return cfg.name"
    assert checked_suggestion(finding, 2, text) == "    return cfg.name"


def test_ruff_unsafe_or_multiline_fix_is_not_offered():
    line = "    return getattr(cfg, \"name\")"
    edit = {
        "content": "cfg.name",
        "location": {"row": 2, "column": 12},
        "end_location": {"row": 2, "column": 32},
    }
    assert ruff_fix_line({"applicability": "unsafe", "edits": [edit]}, 2, line) is None
    spans_lines = dict(edit, end_location={"row": 3, "column": 1})
    assert ruff_fix_line({"applicability": "safe", "edits": [spans_lines]}, 2, line) is None
    assert ruff_fix_line(None, 2, line) is None


def test_unchanged_line_is_not_offered():
    """A block identical to the anchored line is a no-op commit."""
    same = _finding(suggestion="    return option.value")
    assert checked_suggestion(same, 3, _NULL_DEREF_FILE) is None
    # Unindented echo of the same line is still the same line.
    assert checked_suggestion(_finding(suggestion="return option.value", line=3), 3, _NULL_DEREF_FILE) is None


def test_prose_span_for_another_statement_is_not_promoted():
    """A span that does not open with the anchored line's target or keyword
    is not a rewrite of that line."""
    msg = "guard it with `if option is None: return None` first"
    assert checked_suggestion(_finding(msg), 2, _NULL_DEREF_FILE) is None
    two_spans = "use `option = None` or `option = next(iter(options))`"
    assert checked_suggestion(_finding(two_spans), 2, _NULL_DEREF_FILE) is None
    prefix_only = "`optional = 1`"
    assert checked_suggestion(_finding(prefix_only), 2, _NULL_DEREF_FILE) is None


def test_no_file_text_offers_no_block():
    assert checked_suggestion(_finding(suggestion="x = 1"), 2, "") is None


def test_dispatch_inline_body_carries_checked_suggestion_block():
    """The dispatch seam: the promoted fix lands as a committable block in
    the posted inline body, and the posted findings keep their identity."""
    finding = _finding(_NULL_DEREF_MESSAGE)
    evaluation = CodeReviewEvaluation(
        findings=(finding,), conclusion="failure", degraded_reason=None,
    )
    result, posted = cr_dispatch._build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
        file_contents={"app/pick.py": _NULL_DEREF_FILE},
    )
    assert result is not None
    assert posted == (finding,)
    body = result.comments[0].body
    assert (
        "```suggestion\n    option = next((o for o in options if ...), None)\n```"
        in body
    )
