"""grug#1398: a finding's prose line numbers agree with its head-commit anchor.

The model sometimes quotes a line by its pre-change number. Only a number the
hunk's old-to-new map names as a pre-change alias of the anchor line is
rewritten; every other real line a message names survives unchanged.
"""
from __future__ import annotations

import re

from llm_client import Backend, LlmReviewResponse
from llm_client import Finding as LlmFinding
from llm_client import Hunk as LlmHunk
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.diff_parser import parse_diff, pre_change_aliases
from personas.code_reviewer.line_refs import align_line_refs, repair_where_refs
from personas.code_reviewer.persona import evaluate_diff
from review_pipeline import plan_review

# The first hunk adds 27 lines, so the second hunk's head numbers run 27
# ahead of its pre-change numbers: head line 130 (an added line) sits at
# pre-change line 103, the case the ticket saw live.
_ADDED = "".join(f"+added {i}\n" for i in range(27))
_DIFF = (
    "diff --git a/src/todos.js b/src/todos.js\n"
    "--- a/src/todos.js\n"
    "+++ b/src/todos.js\n"
    "@@ -1,2 +1,29 @@\n"
    " first\n"
    f"{_ADDED}"
    " second\n"
    "@@ -100,5 +127,6 @@\n"
    " ctx100\n"
    " ctx101\n"
    " ctx102\n"
    "+const grave = load();\n"
    " ctx103\n"
    " ctx104\n"
)


def _hunks():
    return parse_diff(_DIFF)


def _review(*findings: LlmFinding):
    llm = LlmReviewResponse(
        kind="reviewed", findings=findings,
        backend_used=Backend.POOLSIDE, model_name="m",
    )
    return evaluate_diff(_hunks(), llm)


def _finding(
    message: str, line: int = 130, path: str = "src/todos.js", rule: str = "null-deref",
) -> LlmFinding:
    return LlmFinding(
        path=path, line=line, rule=rule, severity="medium", message=message,  # type: ignore[arg-type]
    )


def _where(body: str) -> str:
    m = re.search(r"\*\*Where:\*\* `([^`]+)`", body)
    assert m, body
    return m.group(1)


def test_pre_change_aliases_walk_the_old_to_new_map() -> None:
    second = _hunks()[1]
    # Added line 130 is inserted before pre-change line 103.
    assert pre_change_aliases(second, 130) == frozenset({103})
    # Context line 131 is pre-change line 103; the header offset gives 104.
    assert pre_change_aliases(second, 131) == frozenset({103, 104})
    # A line this hunk does not show has no alias.
    assert pre_change_aliases(second, 90) == frozenset()


def test_prechange_line_in_message_is_published_as_the_anchor_line() -> None:
    evaluation = _review(_finding("Grug see a quiet grave at line 103"))
    (f,) = evaluation.findings
    assert f.line == 130
    assert f.message == "Grug see a quiet grave at line 130"
    result, _ = cr_dispatch.build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
    )
    (comment,) = result.comments
    assert "at line 130" in comment.body
    assert "line 103" not in comment.body


def test_where_footer_names_the_inline_anchor() -> None:
    """The footer and the inline anchor are the same file and line, and a
    model-written path:line for the anchor is aligned to it too."""
    evaluation = _review(
        _finding("Where: `todos.js:103` reads grave before load"),
        _finding("Grug see trouble", line=5, rule="sync-io-in-async"),
    )
    result, _ = cr_dispatch.build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
    )
    assert len(result.comments) == 2
    for comment in result.comments:
        assert _where(comment.body) == f"{comment.path}:{comment.line}"
    assert "`todos.js:130`" in result.comments[0].body
    assert "todos.js:103" not in result.comments[0].body


def test_two_different_head_lines_both_survive() -> None:
    """Operator ruling: a finding citing two real lines keeps both."""
    message = (
        "The guard at line 128 does not cover the call at line 130; "
        "see lines 128 and 132, and the helper at line 90."
    )
    evaluation = _review(_finding(message))
    assert evaluation.findings[0].message == message


def test_a_number_that_is_not_an_alias_of_the_anchor_is_left_alone() -> None:
    # 104 is a pre-change number, but of line 131, not of the anchor 130.
    msg = "Grug see grave at line 103 and its twin at line 104"
    assert align_line_refs(msg, "src/todos.js", 130, _hunks()) == (
        "Grug see grave at line 130 and its twin at line 104"
    )
    # A reference to another file, or a number that is no line, is untouched.
    other = "Compare `other.js:103` and PR 103 before line 103"
    assert align_line_refs(other, "src/todos.js", 130, _hunks()) == (
        "Compare `other.js:103` and PR 103 before line 130"
    )


def test_alias_that_is_also_a_shown_head_line_is_kept() -> None:
    """Head line 6 is pre-change line 4, but head line 4 is on screen too
    (a context line of the first hunk): the model may mean it, so keep it."""
    diff = (
        "diff --git a/.github/x.yml b/.github/x.yml\n"
        "--- a/.github/x.yml\n"
        "+++ b/.github/x.yml\n"
        "@@ -1,2 +1,4 @@\n a\n+b\n+c\n d\n"
        "@@ -3,3 +5,4 @@\n e\n+f\n g\n h\n"
    )
    hunks = parse_diff(diff)
    assert pre_change_aliases(hunks[1], 6) == frozenset({4})
    msg = "f at line 6 undoes d at line 4"
    assert align_line_refs(msg, ".github/x.yml", 6, hunks) == msg


def test_dot_path_reference_is_aligned() -> None:
    hunks = parse_diff(_DIFF.replace("src/todos.js", ".github/todos.js"))
    assert align_line_refs(
        "Where: `.github/todos.js:103`", ".github/todos.js", 130, hunks,
    ) == "Where: `.github/todos.js:130`"


def test_chunked_review_keeps_numbers_consistent() -> None:
    """A diff reviewed in more than one call: each cohort keeps whole hunks
    with their original headers, so findings from either call are aligned
    against the same map once the answers are merged."""
    second_file = (
        "diff --git a/lib/b.py b/lib/b.py\n"
        "--- a/lib/b.py\n"
        "+++ b/lib/b.py\n"
        "@@ -40,3 +50,4 @@\n"
        " b40\n"
        "+b_new = risky()\n"
        " b41\n"
        " b42\n"
    )
    hunks = parse_diff(_DIFF + second_file)
    llm_hunks = [LlmHunk(path=h.file_path, body=h.body) for h in hunks]
    plan = plan_review(llm_hunks, max_cohort_chars=200, max_cohort_paths=1)
    assert len(plan.cohorts) >= 2
    cohort_paths = [set(c.paths) for c in plan.cohorts]
    assert any("src/todos.js" in p for p in cohort_paths)
    assert any("lib/b.py" in p for p in cohort_paths)
    # One finding per call, each quoting its own pre-change number:
    # head 130 is pre-change 103; head 51 (added) is pre-change 41.
    merged = (
        _finding("Grug see a quiet grave at line 103"),
        _finding("Grug see risky call at `lib/b.py:41`", line=51, path="lib/b.py"),
    )
    llm = LlmReviewResponse(
        kind="reviewed", findings=merged,
        backend_used=Backend.POOLSIDE, model_name="m",
    )
    evaluation = evaluate_diff(hunks, llm)
    by_path = {f.file: f for f in evaluation.findings}
    assert by_path["src/todos.js"].message.endswith("at line 130")
    assert by_path["lib/b.py"].message.endswith("`lib/b.py:51`")
    result, _ = cr_dispatch.build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
    )
    for comment in result.comments:
        assert _where(comment.body) == f"{comment.path}:{comment.line}"


def test_model_where_naming_another_line_is_repaired_at_publish() -> None:
    """The ticket's live case: a finding anchored at 158 whose own text says
    `**Where:** todos.js:151`. 151 is no pre-change alias, so the line map
    keeps it; the publish-time check still repairs the Where to the anchor,
    so the rendered body names one location only."""
    evaluation = _review(
        _finding("**Where:** `todos.js:151` reads grave before load"),
    )
    (f,) = evaluation.findings
    assert f.message == "**Where:** `todos.js:130` reads grave before load"
    result, _ = cr_dispatch.build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
    )
    (comment,) = result.comments
    wheres = re.findall(r"Where:\*\* `([^`]+)`", comment.body)
    assert wheres and {w.split(":")[-1] for w in wheres} == {"130"}
    assert "src/todos.js:130" in wheres
    assert "151" not in comment.body


def test_inline_body_repairs_where_even_without_evaluate_diff() -> None:
    """The check lives in the publish path itself: a Finding that never
    went through evaluate_diff still renders a Where matching its anchor."""
    (base,) = _review(_finding("x")).findings
    from dataclasses import replace
    f = replace(base, message="Where: line 151 and Where: `other.js:9`; see line 151")
    body = cr_dispatch._inline_comment_body(f)
    assert "Where: line 130 and Where: `src/todos.js:130`; see line 151" in body


def test_repair_where_refs_is_narrow_and_idempotent() -> None:
    msg = "Where: `todos.js:130` ok; the guard at line 90 stays"
    assert repair_where_refs(msg, "src/todos.js", 130) == msg
    fixed = repair_where_refs("**Where**: todos.js:7", "src/todos.js", 130)
    assert fixed == "**Where**: todos.js:130"
    assert repair_where_refs(fixed, "src/todos.js", 130) == fixed
    assert repair_where_refs("nowhere near line 7", "src/todos.js", 130) == (
        "nowhere near line 7"
    )
    # A bare Where with no other prose is repaired too (Elder thread on #1453).
    assert repair_where_refs("`Where: todos.js:151`", "src/todos.js", 130) == (
        "`Where: todos.js:130`"
    )
