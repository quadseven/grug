"""grug#1398: a finding's prose line numbers agree with its head-commit anchor.

The model sometimes quotes a line by its pre-change number. Only a number the
hunk's old-to-new map names as a pre-change alias of the anchor line, and that
the diff proves is past the head file's end, is rewritten; every number that
is or may be a real head line survives unchanged (operator ruling: a finding
citing two lines keeps both).
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


# A file that shrank from 30 lines to 4, with its end on screen (the
# no-newline marker after a context line). Head line 3 (an added line) is
# inserted before pre-change line 30, and 30 cannot be a head line: the
# head file has 4.
_DELS = "".join(f"-gone {i}\n" for i in range(27))
_EOF_DIFF = (
    "diff --git a/src/todos.js b/src/todos.js\n"
    "--- a/src/todos.js\n"
    "+++ b/src/todos.js\n"
    "@@ -1,30 +1,4 @@\n"
    " first\n"
    f"{_DELS}"
    " ctx\n"
    "+const grave = load();\n"
    " last\n"
    "\\ No newline at end of file\n"
)


def _hunks(diff: str = _DIFF):
    return parse_diff(diff)


def _review(*findings: LlmFinding, diff: str = _DIFF):
    llm = LlmReviewResponse(
        kind="reviewed", findings=findings,
        backend_used=Backend.POOLSIDE, model_name="m",
    )
    return evaluate_diff(_hunks(diff), llm)


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
    """A pre-change number that cannot be a head line (the head file has 4
    lines, the model wrote 30) is published as the anchor line."""
    evaluation = _review(
        _finding("Grug see a quiet grave at line 30", line=3), diff=_EOF_DIFF,
    )
    (f,) = evaluation.findings
    assert f.line == 3
    assert f.message == "Grug see a quiet grave at line 3"
    result, _ = cr_dispatch.build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
    )
    (comment,) = result.comments
    assert "at line 3" in comment.body
    assert "line 30" not in comment.body


def test_alias_that_is_a_real_head_line_outside_the_hunks_is_kept() -> None:
    """Pre-change 103 is an alias of head 130, but head line 103 exists
    (unchanged, above the second hunk): the model may mean it. Both lines a
    finding names survive."""
    msg = "The guard at line 103 does not cover the call at line 130"
    assert align_line_refs(msg, "src/todos.js", 130, _hunks()) == msg
    (f,) = _review(_finding(msg)).findings
    assert f.message == msg


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
    hunks = _hunks(_EOF_DIFF)
    # 29 is a pre-change number, but of head line 2, not of the anchor 3.
    msg = "Grug see grave at line 30 and its twin at line 29"
    assert align_line_refs(msg, "src/todos.js", 3, hunks) == (
        "Grug see grave at line 3 and its twin at line 29"
    )
    # A reference to another file, or a number that is no line, is untouched.
    other = "Compare `other.js:30` and PR 30 before line 30"
    assert align_line_refs(other, "src/todos.js", 3, hunks) == (
        "Compare `other.js:30` and PR 30 before line 3"
    )


def test_no_rewrite_when_the_diff_does_not_show_the_head_file_end() -> None:
    """Without the end on screen, a number past the shown lines may still be
    a real head line, so nothing is rewritten."""
    diff = _EOF_DIFF.replace("\\ No newline at end of file\n", "")
    msg = "Grug see a quiet grave at line 30"
    assert align_line_refs(msg, "src/todos.js", 3, _hunks(diff)) == msg


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
    hunks = parse_diff(_EOF_DIFF.replace("src/todos.js", ".github/todos.js"))
    assert align_line_refs(
        "Where: `.github/todos.js:30`", ".github/todos.js", 3, hunks,
    ) == "Where: `.github/todos.js:3`"


def test_chunked_review_keeps_numbers_consistent() -> None:
    """A diff reviewed in more than one call: each cohort keeps whole hunks
    with their original headers, so findings from either call are aligned
    against the same map once the answers are merged."""
    gone = "".join(f"-gone {i}\n" for i in range(10))
    second_file = (
        "diff --git a/lib/b.py b/lib/b.py\n"
        "--- a/lib/b.py\n"
        "+++ b/lib/b.py\n"
        "@@ -1,12 +1,3 @@\n"
        " b1\n"
        f"{gone}"
        "+b_new = risky()\n"
        " b12\n"
        "\\ No newline at end of file\n"
    )
    hunks = parse_diff(_EOF_DIFF + second_file)
    llm_hunks = [LlmHunk(path=h.file_path, body=h.body) for h in hunks]
    plan = plan_review(llm_hunks, max_cohort_chars=200, max_cohort_paths=1)
    assert len(plan.cohorts) >= 2
    cohort_paths = [set(c.paths) for c in plan.cohorts]
    assert any("src/todos.js" in p for p in cohort_paths)
    assert any("lib/b.py" in p for p in cohort_paths)
    # One finding per call, each quoting its own pre-change number:
    # head 3 is pre-change 30; head 2 of lib/b.py (added) is pre-change 12.
    merged = (
        _finding("Grug see a quiet grave at line 30", line=3),
        _finding("Grug see risky call at `lib/b.py:12`", line=2, path="lib/b.py"),
    )
    llm = LlmReviewResponse(
        kind="reviewed", findings=merged,
        backend_used=Backend.POOLSIDE, model_name="m",
    )
    evaluation = evaluate_diff(hunks, llm)
    by_path = {f.file: f for f in evaluation.findings}
    assert by_path["src/todos.js"].message.endswith("at line 3")
    assert by_path["lib/b.py"].message.endswith("`lib/b.py:2`")
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
    # The first Where names one line of the anchor's file: repaired. The
    # second names another file (and a second line): left as written.
    assert "Where: line 130 and Where: `other.js:9`; see line 151" in body


def test_where_naming_two_locations_keeps_both() -> None:
    """Operator ruling: a Where citing two lines keeps both."""
    for msg in (
        "Where: `todos.js:90` and `todos.js:130`",
        "Where: line 90; the call at line 130",
        "**Where:** lines 90 and 130",
    ):
        assert repair_where_refs(msg, "src/todos.js", 130) == msg
        (f,) = _review(_finding(msg)).findings
        assert f.message == msg
        body = cr_dispatch._inline_comment_body(f)
        assert msg in body


def test_where_naming_another_file_is_left_alone() -> None:
    msg = "**Where:** `src/helper.js:12` guard misses this call"
    assert repair_where_refs(msg, "src/todos.js", 130) == msg
    (f,) = _review(_finding(msg)).findings
    assert f.message == msg
    assert msg in cr_dispatch._inline_comment_body(f)


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
