"""Tests for move_detection.py - whole-file deletions + identical additions."""
from __future__ import annotations

import pytest

from personas.code_reviewer.move_detection import split_move_hunks, _parse_diff_headers, _extract_added_file_content


class TestMoveDetection:
    def _hunk(self, path: str, content: str = "line\n" * 10) -> str:
        """Create a whole-new-file hunk with the given path and content."""
        lines = content.splitlines()
        body = ["@@ -0,0 +1,{} @@".format(len(lines))]
        body.extend(f"+{line}" for line in lines)
        return f"diff --git a/test.py b/{path}\nnew file mode 100644\nindex 0000000..abc\n--- /dev/null\n+++ b/{path}\n" + "\n".join(body)

    def test_no_move_returns_original_hunks(self):
        """When there are no moves, return hunks unchanged."""
        hunk = self._hunk("a.py")
        hunks_result, moves_result = split_move_hunks((hunk,), "test diff")
        assert hunks_result == (hunk,)
        assert moves_result == ()

    def test_identical_deletion_and_addition_excluded(self):
        """A file deleted and an identical file added are treated as a move."""
        diff = """diff --git a/old.py b/old.py
deleted file mode 100644
index abc..0000000
--- a/old.py
+++ /dev/null

diff --git a/new.py b/new.py
new file mode 100644
index 0000000..abc
--- /dev/null
+++ b/new.py
@@ -0,0 +1,10 @@
+line
+line
+line
+line
+line
+line
+line
+line
+line
+line
+line
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        assert hunks_result == ()
        assert len(moves_result) == 1
        assert moves_result[0] == ("old.py", "new.py")

    def test_multiple_moves_excluded(self):
        """Multiple files moved in one diff."""
        diff = """diff --git a/old1.py b/old1.py
deleted file mode 100644
--- a/old1.py
+++ /dev/null

diff --git a/old2.py b/old2.py
deleted file mode 100644
--- a/old2.py
+++ /dev/null

diff --git a/new1.py b/new1.py
new file mode 100644
index 0000000..abc
--- /dev/null
+++ b/new1.py
@@ -0,0 +1,10 @@
+line
+line
+line
+line
+line
+line
+line
+line
+line
+line
+line

diff --git a/new2.py b/new2.py
new file mode 100644
index 0000000..abc
--- /dev/null
+++ b/new2.py
@@ -0,0 +1,10 @@
+line
+line
+line
+line
+line
+line
+line
+line
+line
+line
+line
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        assert hunks_result == ()
        assert len(moves_result) == 2
        assert moves_result[0] == ("old1.py", "new1.py")
        assert moves_result[1] == ("old2.py", "new2.py")

    def test_partial_edit_not_treated_as_move(self):
        """A partial edit after a deletion is not treated as a move."""
        diff = """diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null

diff --git a/new.py b/new.py
new file mode 100644
index 0000000..abc
--- /dev/null
+++ b/new.py
@@ -0,0 +1,5 @@
+line1
+line2
+line3
+line4
+line5
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        assert hunks_result == ()
        assert len(moves_result) == 1
        assert moves_result[0] == ("old.py", "new.py")

    def test_edited_file_not_treated_as_move(self):
        """A file that is edited is not treated as a move, even if content matches a deleted file."""
        diff = """diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null

diff --git a/new.py b/new.py
index abc..def
--- a/new.py
+++ b/new.py
@@ -1,5 +1,5 @@
 line1
 line2
-line3
+line3_changed
 line4
 line5
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        assert hunks_result == ()
        assert len(moves_result) == 1
        assert moves_result[0] == ("old.py", "new.py")

    def test_moved_file_keeps_edit(self):
        """When a file is moved AND edited, the edit is reviewed, not the whole file."""
        diff = """diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null

diff --git a/new.py b/new.py
index abc..def
--- a/new.py
+++ b/new.py
@@ -1,5 +1,6 @@
 line1
 line2
-line3
+line3_changed
 line4
 line5
+new_line
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        # The edit should still be reviewed
        assert len(hunks_result) == 1
        assert len(moves_result) == 1

    def test_deleted_then_edited_does_not_count_as_move(self):
        """A deletion followed by an edit (even in the same path) is a modification, not a move."""
        diff = """diff --git a/file.py b/file.py
deleted file mode 100644
--- a/file.py
+++ /dev/null

diff --git a/file.py b/file.py
index abc..def
--- a/file.py
+++ b/file.py
@@ -1,5 +1,6 @@
 old1
 old2
-old3
+new3
 old4
 old5
+new6
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        # This is an edit, not a move
        assert len(hunks_result) == 1
        assert len(moves_result) == 0

    def test_different_content_no_move(self):
        """When deleted and added files have different content, nothing is excluded."""
        diff = """diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null

diff --git a/new.py b/new.py
new file mode 100644
index 0000000..def
--- /dev/null
+++ b/new.py
@@ -0,0 +1,10 @@
+different
+different
+different
+different
+different
+different
+different
+different
+different
+different
+different
+different
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        assert hunks_result == ()
        assert len(moves_result) == 0

    def test_header_parsing_handles_malformed_diff(self):
        """If header parsing fails, don't exclude anything (fail-safe)."""
        diff = """diff --git malformed
@@ -1 +1 @@
-old
+new
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        assert hunks_result == ()
        assert len(moves_result) == 0

    def test_extract_added_file_content_only_hunks(self):
        """_extract_added_file_content only reads hunks."""
        diff = """diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null

diff --git a/new.py b/new.py
new file mode 100644
index 0000000..abc
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+line1
+line2
+line3

diff --git a/edited.py b/edited.py
index 111..222
--- a/edited.py
+++ b/edited.py
@@ -1,2 +1,2 @@
 keep
-new
+changed
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        # Only the new file should be extracted, not the edited file
        content = _extract_added_file_content(hunks_result[0])
        assert len(content) == 1
        assert "new.py" in content

    def test_empty_diff_no_moves(self):
        """An empty diff has no moves."""
        hunks_result, moves_result = split_move_hunks((), "")
        assert hunks_result == ()
        assert len(moves_result) == 0

    def test_malformed_hunk_skipped(self):
        """A malformed hunk is skipped without causing issues."""
        diff = """diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null

diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ this is not a valid hunk header @@
+line1
+line2
"""
        hunks_result, moves_result = split_move_hunks((), diff)
        # Malformed hunk should be skipped, but other hunks should remain
        assert len(hunks_result) == 0
        assert len(moves_result) == 0


class TestDiffHeaderParsing:
    def test_detects_deleted_file(self):
        diff = """diff --git a/file.py b/file.py
deleted file mode 100644
--- a/file.py
+++ /dev/null
"""
        headers, blobs = _parse_diff_headers(diff)
        assert len(headers) == 1
        path, status, sim = headers[0]
        assert path == "file.py"
        assert status == "removed"
        assert sim is None

    def test_detects_added_file_with_blob(self):
        diff = """diff --git a/new.py b/new.py
new file mode 100644
index 0000000..abc123
--- /dev/null
+++ b/new.py
"""
        headers, blobs = _parse_diff_headers(diff)
        assert len(headers) == 1
        path, status, sim = headers[0]
        assert path == "new.py"
        assert status == "added"
        assert sim is None
        assert "abc123" in blobs

    def test_detects_mixed_changes(self):
        diff = """diff --git a/deleted.py b/deleted.py
deleted file mode 100644
--- a/deleted.py
+++ /dev/null

diff --git a/added.py b/added.py
new file mode 100644
index 0000000..abc
--- /dev/null
+++ b/added.py
"""
        headers, blobs = _parse_diff_headers(diff)
        assert len(headers) == 2

        # Find deleted
        deleted = next((h for h in headers if h[1] == "removed"), None)
        assert deleted is not None
        assert deleted[0] == "deleted.py"

        # Find added
        added = next((h for h in headers if h[1] == "added"), None)
        assert added is not None
        assert added[0] == "added.py"

    def test_handles_malformed_diff_gracefully(self):
        diff = """not a valid diff
"""
        headers, blobs = _parse_diff_headers(diff)
        assert headers == []
        assert blobs == {}

    def test_empty_diff(self):
        headers, blobs = _parse_diff_headers("")
        assert headers == []
        assert blobs == {}
