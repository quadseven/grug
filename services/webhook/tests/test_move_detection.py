"""Tests for move_detection: relocations Elder must not review as new code (#772)."""
from __future__ import annotations

from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.move_detection import (
    SubtreeImport, find_moves, split_move_hunks, subtree_imports,
)

BODY = "".join(f"line {i}\n" for i in range(1, 9))
BLOB_A = "a" * 40
BLOB_B = "b" * 40


def _added(path: str, blob: str, body: str = BODY) -> str:
    lines = body.splitlines()
    plus = "".join(f"+{ln}\n" for ln in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        f"index 0000000..{blob[:10]}\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{plus}"
    )


def _deleted(path: str, blob: str, body: str = BODY) -> str:
    lines = body.splitlines()
    minus = "".join(f"-{ln}\n" for ln in lines)
    return (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n"
        f"index {blob[:10]}..0000000\n--- a/{path}\n+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n{minus}"
    )


def _renamed(old: str, new: str, sim: int, edit: bool = False) -> str:
    head = (
        f"diff --git a/{old} b/{new}\nsimilarity index {sim}%\n"
        f"rename from {old}\nrename to {new}\n"
    )
    if not edit:
        return head
    return (
        head + f"index 1111111..2222222 100644\n--- a/{old}\n+++ b/{new}\n"
        "@@ -8,1 +8,1 @@\n-line 8\n+line 8 changed\n"
    )


def test_pure_rename_is_a_move_and_has_no_hunks() -> None:
    diff = _renamed("old/a.py", "new/a.py", 100)
    kept, moves = split_move_hunks(parse_diff(diff), diff)
    assert kept == ()
    assert moves == (("old/a.py", "new/a.py"),)


def test_rename_with_edits_reviews_only_changed_hunk() -> None:
    diff = _renamed("old/a.py", "new/a.py", 90, edit=True)
    kept, moves = split_move_hunks(parse_diff(diff), diff)
    assert moves == ()
    assert [h.file_path for h in kept] == ["new/a.py"]
    assert kept[0].new_lines == frozenset({8})
    assert "line 1" not in kept[0].body


def test_added_file_matching_deleted_blob_is_a_move() -> None:
    diff = _deleted("old.py", BLOB_A) + _added("new.py", BLOB_A)
    kept, moves = split_move_hunks(parse_diff(diff), diff)
    assert kept == ()
    assert moves == (("old.py", "new.py"),)


def test_new_file_without_matching_blob_is_reviewed_in_full() -> None:
    diff = _deleted("old.py", BLOB_A) + _added("new.py", BLOB_B)
    kept, moves = split_move_hunks(parse_diff(diff), diff)
    assert moves == ()
    assert [h.file_path for h in kept] == ["new.py"]


def test_added_file_with_no_deletion_is_reviewed() -> None:
    diff = _added("new.py", BLOB_A)
    kept, moves = split_move_hunks(parse_diff(diff), diff)
    assert moves == () and len(kept) == 1


def test_one_deleted_file_excuses_one_add() -> None:
    diff = _deleted("old.py", BLOB_A) + _added("x.py", BLOB_A) + _added("y.py", BLOB_A)
    kept, moves = split_move_hunks(parse_diff(diff), diff)
    assert moves == (("old.py", "x.py"),)
    assert [h.file_path for h in kept] == ["y.py"]


def test_short_blob_prefix_is_not_trusted() -> None:
    diff = _deleted("old.py", "abc") + _added("new.py", "abc")
    kept, moves = split_move_hunks(parse_diff(diff), diff)
    assert moves == () and len(kept) == 1


def test_find_moves_names_rename_and_blob_pairs_together() -> None:
    diff = _renamed("a.py", "b.py", 100) + _deleted("c.py", BLOB_A) + _added("d.py", BLOB_A)
    assert find_moves(diff) == (("a.py", "b.py"), ("c.py", "d.py"))


MESSAGES = (
    "Squash 'vendor-dir/' content from commit abc\n\n"
    "git-subtree-dir: vendor-dir\ngit-subtree-split: " + "c" * 40 + "\n",
    "Merge commit\n\nnothing here",
    "bad\n\ngit-subtree-dir: ../escape\ngit-subtree-split: " + "d" * 40,
    "bad2\n\ngit-subtree-dir: vendor-dir\ngit-subtree-split: not-a-sha",
)


def test_subtree_imports_parses_trailers_and_drops_bad_ones() -> None:
    assert subtree_imports(MESSAGES) == (SubtreeImport("vendor-dir", "c" * 40),)


def test_subtree_add_pure_adds_excluded_but_edited_file_reviewed() -> None:
    same = "c" * 40
    edited = "e" * 40
    diff = (
        _added("vendor-dir/a.py", BLOB_A)
        + _added("vendor-dir/sub/b.py", BLOB_B)
        + _added("vendor-dir/edited.py", edited)
    )
    trees = {
        "c" * 40: {"a.py": BLOB_A, "sub/b.py": BLOB_B, "edited.py": same},
    }
    imports = (SubtreeImport("vendor-dir", "c" * 40),)
    kept, moves = split_move_hunks(
        parse_diff(diff), diff, imports=imports, split_trees=trees,
    )
    assert [h.file_path for h in kept] == ["vendor-dir/edited.py"]
    assert len(moves) == 2
    assert moves[0][1] == "vendor-dir/a.py"
    assert moves[0][0].endswith("a.py")


def test_missing_split_tree_falls_back_to_full_review() -> None:
    diff = _added("vendor-dir/a.py", BLOB_A)
    imports = (SubtreeImport("vendor-dir", "c" * 40),)
    kept, moves = split_move_hunks(parse_diff(diff), diff, imports=imports, split_trees={})
    assert moves == () and len(kept) == 1


def test_file_outside_subtree_dir_is_not_matched() -> None:
    diff = _added("other/a.py", BLOB_A)
    imports = (SubtreeImport("vendor-dir", "c" * 40),)
    trees = {"c" * 40: {"a.py": BLOB_A}}
    kept, moves = split_move_hunks(parse_diff(diff), diff, imports=imports, split_trees=trees)
    assert moves == () and len(kept) == 1


def test_summary_names_moved_files() -> None:
    from personas.code_reviewer.dispatch import _move_note

    note = _move_note((("old/a.py", "new/a.py"),))
    assert "1 moved file(s)" in note and "`old/a.py` -> `new/a.py`" in note
    assert _move_note(()) == ""


def test_split_copied_hunks_reports_pure_rename(monkeypatch) -> None:
    from personas.code_reviewer import dispatch

    monkeypatch.setattr(dispatch, "_fetch_subtree_context", lambda *a: ((), {}))
    diff = _renamed("old/a.py", "new/a.py", 100)
    hunks, dups, moves = dispatch._split_copied_hunks(
        parse_diff(diff), diff, 1, "o", "r", 1, "h" * 40,
    )
    assert (hunks, dups, moves) == ((), (), (("old/a.py", "new/a.py"),))
