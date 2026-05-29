"""Tests for personas/code_reviewer/diff_parser.py."""
from __future__ import annotations

from personas.code_reviewer.diff_parser import DiffHunk, parse_diff


_SIMPLE_DIFF = """diff --git a/src/x.py b/src/x.py
index abc..def 100644
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def test_simple_single_file_diff_extracts_one_hunk() -> None:
    hunks = parse_diff(_SIMPLE_DIFF)
    assert len(hunks) == 1
    h = hunks[0]
    assert h.file_path == "src/x.py"
    assert h.new_start == 1
    # New-side lines: line 1 (context), 2 (new1), 3 (new2) — "old" was removed.
    # parser must record line numbers for the added/changed new-side content
    # so evaluate_diff can validate LLM findings target real lines.
    assert 2 in h.new_lines  # new1
    assert 3 in h.new_lines  # new2


def test_multi_file_diff_extracts_one_hunk_per_file() -> None:
    diff = _SIMPLE_DIFF + """diff --git a/src/y.py b/src/y.py
index 111..222 100644
--- a/src/y.py
+++ b/src/y.py
@@ -10,2 +10,3 @@
 keep
+added_in_y
 keep_again
"""
    hunks = parse_diff(diff)
    assert len(hunks) == 2
    assert {h.file_path for h in hunks} == {"src/x.py", "src/y.py"}
    y_hunk = next(h for h in hunks if h.file_path == "src/y.py")
    assert y_hunk.new_start == 10
    assert 11 in y_hunk.new_lines


def test_binary_file_block_is_skipped() -> None:
    """`Binary files ... differ` blocks produce no hunks. The Elder
    persona can't review binary content; silently dropping is correct
    (vs raising — many diffs have a benign image touch alongside code)."""
    diff = (
        "diff --git a/img.png b/img.png\n"
        "Binary files a/img.png and b/img.png differ\n"
        + _SIMPLE_DIFF
    )
    hunks = parse_diff(diff)
    # Only the text-file hunk remains; the binary block produced nothing.
    assert len(hunks) == 1
    assert hunks[0].file_path == "src/x.py"


def test_rename_without_changes_produces_no_hunks() -> None:
    """A pure rename has no `@@` block — nothing to review. Must not
    crash, must not synthesize a fake hunk."""
    diff = """diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
"""
    hunks = parse_diff(diff)
    assert hunks == ()


def test_rename_with_inline_edit_uses_new_path() -> None:
    """Rename + content change — the hunk's file_path must be the new
    path. LLM findings reference the new tree."""
    diff = """diff --git a/old.py b/renamed.py
similarity index 90%
rename from old.py
rename to renamed.py
index abc..def 100644
--- a/old.py
+++ b/renamed.py
@@ -1,2 +1,2 @@
 keep
-old_line
+new_line
"""
    hunks = parse_diff(diff)
    assert len(hunks) == 1
    assert hunks[0].file_path == "renamed.py"


def test_empty_diff_returns_empty_tuple() -> None:
    assert parse_diff("") == ()


def test_diff_with_multiple_hunks_per_file_returns_each() -> None:
    """A file with two separate @@-blocks (e.g. edits at the top and
    bottom) must produce two hunks bound to the same file_path."""
    diff = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,2 +1,3 @@
 a
+top_add
 b
@@ -100,1 +101,2 @@
 c
+bottom_add
"""
    hunks = parse_diff(diff)
    assert len(hunks) == 2
    assert all(h.file_path == "src/x.py" for h in hunks)
    assert hunks[0].new_start == 1
    assert hunks[1].new_start == 101


def test_diff_hunk_is_frozen() -> None:
    """`DiffHunk` must be immutable so callers can hash/share it."""
    import dataclasses
    assert dataclasses.is_dataclass(DiffHunk)
    field = next(f for f in dataclasses.fields(DiffHunk) if f.name == "file_path")
    assert field is not None
    h = parse_diff(_SIMPLE_DIFF)[0]
    try:
        h.file_path = "other.py"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("DiffHunk should be frozen")


def test_hunk_body_preserved_for_llm_consumption() -> None:
    """`body` retains the original `@@`-prefixed text so the LLM gets
    full diff context (not just line numbers). This is the shape
    `llm_client.Hunk(path, body)` expects."""
    hunks = parse_diff(_SIMPLE_DIFF)
    assert "+new1" in hunks[0].body
    assert "-old" in hunks[0].body
