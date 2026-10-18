"""Checked committable suggestions (#1149, part of #1143).

A ```suggestion block replaces the anchored line in one click, so every block
must change that line and, for Python, leave the file parseable. These tests
pin the three sources (the model's field, a promoted prose fix, ruff's own
fix) and the dispatch seam that renders the result.
"""
from __future__ import annotations

import ast
import shutil

import pytest

import personas.code_reviewer.lint as lint_mod
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.diff_parser import DiffHunk
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
    result, _ = cr_dispatch.build_review_result(
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
        "filename": "/work/grug-lint-x/app/read.py",
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
        results, "/work/grug-lint-x/", {"app/read.py": {2}}, {"app/read.py": text},
    )
    assert finding.suggestion == "    return cfg.name"
    assert checked_suggestion(finding, 2, text) == "    return cfg.name"


def test_real_ruff_safe_fix_becomes_suggestion(monkeypatch):
    """The hand-built JSON above mirrors ruff's output; this runs the pinned
    ruff binary itself, so a ruff release that renames `applicability` or
    stops marking B009 safe fails here instead of silently offering no block.
    """
    if shutil.which("ruff") is None:
        pytest.fail("ruff is not on PATH; make webhook-test installs it")
    monkeypatch.setattr(lint_mod, "_ENABLED", True)
    text = "def read(cfg):\n    return getattr(cfg, \"name\")\n"
    hunk = DiffHunk(
        file_path="app/read.py", new_start=1, new_lines=frozenset(),
        body="@@ -0,0 +1,2 @@\n+def read(cfg):\n+    return getattr(cfg, \"name\")\n",
    )
    (finding,) = [f for f in lint_mod.scan_ruff((hunk,), {"app/read.py": text})
                  if "B009" in f.message]
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
    result, posted = cr_dispatch.build_review_result(
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


def test_backslash_continued_line_offers_no_block():
    """Replacing only the first physical line of a continued statement leaves
    its tail as a separate statement that can still parse, so no block."""
    text = "total = 1 + \\\n    2\n"
    finding = _finding(suggestion="total = 5", line=1, file="app/t.py", rule="x")
    assert checked_suggestion(finding, 1, text) is None


@pytest.mark.parametrize(("text", "candidate"), [
    # A trailing backslash joins the next line: `x = f(y)`.
    ("x = 1\n(y)\n", "x = f\\"),
    # An unclosed triple quote runs to the `\"\"\"` in the next line's comment.
    ('x = 1\ns = "a"  # """\n', 'x = """'),
])
def test_candidate_that_swallows_the_next_line_offers_no_block(text, candidate):
    """The replaced file parses, but only because the candidate left a
    statement open that the next line closes, so the commit rewrites two
    lines while the block shows one."""
    finding = _finding(suggestion=candidate, line=1, file="app/t.py", rule="x")
    replaced = candidate + "\n" + text.splitlines()[1] + "\n"
    ast.parse(replaced)
    assert checked_suggestion(finding, 1, text) is None


def test_open_candidate_for_an_open_line_is_still_checked_by_parse():
    """Rewriting the head of a bracketed statement keeps the bracket open;
    that is a real one-line fix, and the parse check covers it."""
    text = "total = add(1,\n    2)\n"
    finding = _finding(suggestion="total = mul(1,", line=1, file="app/t.py", rule="x")
    assert checked_suggestion(finding, 1, text) == "total = mul(1,"


def test_empty_suggestion_field_does_not_fall_back_to_prose():
    """A suggestion field the model filled, even with nothing usable, is the
    model's answer: the prose span is only read when the field is None."""
    blank = _finding(_NULL_DEREF_MESSAGE, suggestion="")
    assert checked_suggestion(blank, 2, _NULL_DEREF_FILE) is None
    fenced = _finding(_NULL_DEREF_MESSAGE, suggestion="```\nx\n```")
    assert checked_suggestion(fenced, 2, _NULL_DEREF_FILE) is None


def test_prose_span_echoing_the_anchored_line_is_not_offered():
    """A message quoting the flagged line itself is not a fix."""
    msg = "this raises: `option = next(o for o in options if o.name == wanted)`"
    assert checked_suggestion(_finding(msg), 2, _NULL_DEREF_FILE) is None


@pytest.mark.parametrize("parse_error", [RecursionError, MemoryError])
@pytest.mark.parametrize("failed_parse", [1, 2])
def test_parser_resource_error_offers_no_block(monkeypatch, parse_error, failed_parse):
    parse = ast.parse
    calls = 0

    def fail_parse(source):
        nonlocal calls
        calls += 1
        if calls == failed_parse:
            raise parse_error
        return parse(source)

    monkeypatch.setattr("personas.code_reviewer.suggestion.ast.parse", fail_parse)
    finding = _finding(suggestion="option = None")
    assert checked_suggestion(finding, 2, _NULL_DEREF_FILE) is None


@pytest.mark.parametrize(("text", "line", "candidate"), [
    # A JSON "\\udcff" escape decodes to a lone surrogate in the field.
    (_NULL_DEREF_FILE, 2, 'option = "\udcff"'),
    # The anchored line itself carries one; the open candidate tokenizes it.
    ('x = "\udcff"\ny = 1\n', 1, "x = f(1,"),
])
def test_unencodable_line_offers_no_block_instead_of_raising(text, line, candidate):
    """The open-statement guard runs before the parse check's try; a line
    the tokenizer cannot encode must not crash the review."""
    finding = _finding(suggestion=candidate, line=line, file="app/t.py", rule="x")
    assert checked_suggestion(finding, line, text) is None
