"""Tests for personas/code_reviewer/diff_parser.py."""
from __future__ import annotations

import pytest

from personas.code_reviewer.diff_parser import DiffHunk, DiffParseError, parse_diff


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
    h = parse_diff(_SIMPLE_DIFF)[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.file_path = "other.py"  # type: ignore[misc]


def test_hunk_body_preserved_for_llm_consumption() -> None:
    """`body` retains the original `@@`-prefixed text so the LLM gets
    full diff context (not just line numbers). This is the shape
    `llm_client.Hunk(path, body)` expects."""
    hunks = parse_diff(_SIMPLE_DIFF)
    assert "+new1" in hunks[0].body
    assert "-old" in hunks[0].body


def test_no_newline_marker_does_not_shift_line_numbers() -> None:
    """A diff that ends with `\\ No newline at end of file` must not
    advance the new-line cursor on that annotation line — otherwise
    every subsequent +line gets a wrong number and the hallucination
    filter rejects real findings."""
    diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,2 +1,3 @@
 keep
-old
\\ No newline at end of file
+new_a
+new_b
"""
    hunks = parse_diff(diff)
    assert len(hunks) == 1
    # `keep` is line 1 (context); `old` removed; `new_a` added at 2,
    # `new_b` at 3. If the `\` marker shifted the cursor, new_a would
    # be 3 and new_b would be 4 → off-by-one for every finding.
    assert 2 in hunks[0].new_lines
    assert 3 in hunks[0].new_lines
    assert 4 not in hunks[0].new_lines


def test_realistic_multi_hunk_shape_with_correct_line_numbers() -> None:
    """A second hunk header `@@ -50,3 +51,4 @@` advances the new-side
    base to 51 — the second hunk's +lines start at 51, not at 1 +
    (count from first hunk). Catches a regression where the parser
    naively continued line numbering across hunks."""
    diff = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 first_keep
+first_add
 second_keep
 third_keep
@@ -50,3 +51,4 @@
 fiftieth_keep
+added_at_52
 fifty_first_keep
 fifty_second_keep
"""
    hunks = parse_diff(diff)
    assert len(hunks) == 2
    # First hunk: +first_add is on new-side line 2 (after first_keep@1).
    assert 2 in hunks[0].new_lines
    # Second hunk: +added_at_52 is on new-side line 52.
    assert 52 in hunks[1].new_lines
    # Numbering MUST NOT bleed across hunks.
    assert 52 not in hunks[0].new_lines


def test_pure_deletion_dev_null_yields_zero_hunks() -> None:
    """A file deletion has `+++ /dev/null` and no @@ block. Must produce
    no hunks and not mis-attribute anything to the deleted path. Guards
    against a regression where the `!= "/dev/null"` guard flips."""
    diff = """diff --git a/x.py b/x.py
deleted file mode 100644
index abc..0000000
--- a/x.py
+++ /dev/null
"""
    assert parse_diff(diff) == ()


def test_real_deletion_with_content_hunk_yields_zero_hunks() -> None:
    """The realistic shape GitHub emits for a deleted text file: a
    full `@@ -1,N +0,0 @@` hunk with each removed line. new_start=0
    would trip DiffHunk.__post_init__ (new_start >= 1) and crash
    parse_diff. The deletion_skip flag must suppress this hunk."""
    diff = """diff --git a/old.py b/old.py
deleted file mode 100644
index abc..0000000
--- a/old.py
+++ /dev/null
@@ -1,3 +0,0 @@
-line_one
-line_two
-line_three
"""
    # Must not raise + must produce zero hunks (nothing to review).
    assert parse_diff(diff) == ()


def test_single_line_hunk_without_count_parses() -> None:
    """GitHub emits `@@ -1 +1 @@` (no `,N` count) for single-line hunks.
    Tests should exercise the optional `,m` branch — without this, a
    regression that makes `,m` mandatory would `DiffParseError` on
    legitimate single-line diffs."""
    diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-old
+new
"""
    hunks = parse_diff(diff)
    assert len(hunks) == 1
    assert 1 in hunks[0].new_lines


def test_no_newline_marker_does_not_shift_line_numbers() -> None:
    """A garbled @@ header is more likely a fetcher bug or GitHub
    format drift than a real hunk we should partially extract. Silent
    skip would let evaluate_diff return a clean "success" verdict —
    false pass. Caller catches DiffParseError → advisory neutral."""
    diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ this is not a valid hunk header @@
 keep
+new
"""
    with pytest.raises(DiffParseError, match="malformed `@@` hunk header"):
        parse_diff(diff)


def test_malformed_diff_git_header_raises_diff_parse_error() -> None:
    """`diff --git` line missing the `a/<old> b/<new>` shape — refuse
    to silently swallow. The prior behavior set current_file=None and
    dropped every subsequent hunk in this file."""
    diff = """diff --git not_a_valid_path_format
--- a/x.py
+++ b/x.py
@@ -1,2 +1,2 @@
 keep
-old
+new
"""
    with pytest.raises(DiffParseError, match="malformed `diff --git`"):
        parse_diff(diff)


def test_emptied_file_hunk_yields_zero_hunks_and_does_not_raise() -> None:
    """A file EMPTIED to zero bytes (truncate-to-empty commit) keeps its
    `+++ b/<path>` line - deletion_skip never arms - and still emits a
    `@@ -1,N +0,0 @@` hunk. new_start=0 crashed parse_diff with an
    AssertionError that escaped the dispatch degrade contract and
    crash-looped the consumer into the rerun DLQ (grug PR #577 force-push,
    2026-07-10). The zero-new-side guard must skip the hunk instead."""
    diff = """diff --git a/emptied.py b/emptied.py
index 325e1ec8..e69de29b 100644
--- a/emptied.py
+++ b/emptied.py
@@ -1,3 +0,0 @@
-line_one
-line_two
-line_three
"""
    assert parse_diff(diff) == ()


def test_emptied_file_does_not_swallow_the_next_files_hunks() -> None:
    """The zero-new-side skip consumes ONLY the emptied file's hunk body;
    a following file in the same diff must still produce its hunks."""
    diff = """diff --git a/emptied.py b/emptied.py
index 325e1ec8..e69de29b 100644
--- a/emptied.py
+++ b/emptied.py
@@ -1,2 +0,0 @@
-gone_one
-gone_two
diff --git a/kept.py b/kept.py
index abc..def 100644
--- a/kept.py
+++ b/kept.py
@@ -1,2 +1,3 @@
 context
-old
+new
"""
    hunks = parse_diff(diff)
    assert [h.file_path for h in hunks] == ["kept.py"]
    assert hunks[0].new_start == 1


def test_diff_hunk_invariant_violation_raises_diff_parse_error() -> None:
    """Boundary invariants must raise DiffParseError - the only parser
    exception the persona dispatch degrade contract catches - never a
    bare AssertionError (which poisons the consumer's rerun loop, and
    which `python -O` would silently strip)."""
    with pytest.raises(DiffParseError, match="new_start must be >= 1"):
        DiffHunk(
            file_path="x.py",
            new_start=0,
            new_lines=frozenset(),
            body="@@ -1,1 +0,0 @@",
        )


def test_zero_start_with_nonzero_count_raises() -> None:
    """`+0,N` (zero start, nonzero count) is malformed unified diff - the
    zero-new-side skip must only accept the legal `+0,0` shape, never
    silently swallow a corrupt header (FLINT PR #580)."""
    diff = """diff --git a/x.py b/x.py
index abc..def 100644
--- a/x.py
+++ b/x.py
@@ -1,3 +0,2 @@
-gone
"""
    with pytest.raises(DiffParseError, match="malformed zero-start"):
        parse_diff(diff)


# --- #609: review-exclusion split -------------------------------------------

from personas.code_reviewer.diff_parser import (  # noqa: E402
    is_review_excluded_path, split_reviewable_hunks,
)


class TestReviewExclusionSplit:
    def _hunk(self, path):
        return DiffHunk(file_path=path, new_start=1, new_lines=frozenset({1}), body="@@ -1 +1 @@\n+x")

    def test_data_and_lockfiles_excluded(self):
        for p in (
            "logs/review-findings.jsonl", "data/x.csv", "web/app.min.js",
            "package-lock.json", "uv.lock", "poetry.lock", "go.sum",
            "assets/logo.svg", "node_modules/a/b.js", "vendor/lib.py",
        ):
            assert is_review_excluded_path(p), p

    def test_code_paths_kept(self):
        for p in (
            "services/webhook/consumer.py", "k8s/deployment.yaml",
            "src/locker.py", "docs/adr/0001.md", "a/distance.py",
            "web/index.js", "config.json",
        ):
            assert not is_review_excluded_path(p), p

    def test_split_partitions_and_dedupes(self):
        hunks = (
            self._hunk("src/a.py"),
            self._hunk("logs/corpus.jsonl"),
            self._hunk("logs/corpus.jsonl"),   # second hunk, same file
            self._hunk("src/b.py"),
        )
        kept, excluded = split_reviewable_hunks(hunks)
        assert [h.file_path for h in kept] == ["src/a.py", "src/b.py"]
        assert excluded == ("logs/corpus.jsonl",)

    def test_all_excluded_yields_empty_kept(self):
        kept, excluded = split_reviewable_hunks((self._hunk("x.lock"),))
        assert kept == ()
        assert excluded == ("x.lock",)


# --- oversized-hunk split ---------------------------------------------------

from personas.code_reviewer.diff_parser import (  # noqa: E402
    split_oversized_hunks,
)


class TestOversizedHunkSplit:
    def _hunk(self, path, chars=10):
        return DiffHunk(
            file_path=path, new_start=1, new_lines=frozenset({1}),
            body="@@ -1 +1 @@\n+" + ("x" * chars),
        )

    def test_hunk_over_budget_is_excluded_by_name(self):
        # Observed shape: a generated .json rewritten wholesale becomes
        # one hunk 21x the cohort cap. It must be NAMED, not planned.
        hunks = (
            self._hunk("services/api.py", 10),
            self._hunk("data/generated/lookup_table.json", 5_000),
        )
        kept, oversized = split_oversized_hunks(hunks, 1_000)
        assert [h.file_path for h in kept] == ["services/api.py"]
        assert oversized == ("data/generated/lookup_table.json",)

    def test_hunk_at_the_budget_is_kept(self):
        # Boundary: the cap is inclusive, so a hunk exactly at budget still
        # gets reviewed. Only a hunk that cannot fit is dropped.
        hunk = self._hunk("a.py", 100)
        kept, oversized = split_oversized_hunks((hunk,), len(hunk.body))
        assert kept == (hunk,)
        assert oversized == ()

    def test_other_hunks_in_the_same_file_survive(self):
        # Exclusion is per HUNK, not per path: one giant generated block must
        # not blind Elder to a small hand-edited hunk elsewhere in the file.
        small = self._hunk("big.json", 10)
        giant = self._hunk("big.json", 5_000)
        kept, oversized = split_oversized_hunks((small, giant), 1_000)
        assert kept == (small,)
        assert oversized == ("big.json",)

    def test_dedupes_repeated_paths(self):
        hunks = (self._hunk("a.json", 5_000), self._hunk("a.json", 6_000))
        kept, oversized = split_oversized_hunks(hunks, 1_000)
        assert kept == ()
        assert oversized == ("a.json",)

    def test_zero_budget_is_a_no_op(self):
        # A non-positive budget means "unbounded" - never silently drop the
        # whole diff because a config value was missing or misparsed.
        hunks = (self._hunk("a.py", 5_000),)
        assert split_oversized_hunks(hunks, 0) == (hunks, ())


# --- #813: byte-identical whole-file duplicate split ------------------------

from personas.code_reviewer.diff_parser import (  # noqa: E402
    new_file_blob_ids,
    split_duplicate_hunks,
    split_tree_copy_hunks,
)


class TestDuplicateHunkSplit:
    def _new_file(self, path, content):
        """Git's shape for a brand-new file: old side `-0,0`, every content
        line added."""
        body_lines = ["@@ -0,0 +1,{} @@".format(content.count("\n") + 1)]
        body_lines.extend(f"+{line}" for line in content.splitlines())
        return DiffHunk(
            file_path=path, new_start=1,
            new_lines=frozenset(range(1, content.count("\n") + 2)),
            body="\n".join(body_lines),
        )

    def _edit(self, path, content="x" * 250):
        """A hunk that is NOT a whole-new-file (old side non-empty) -
        used to prove edits are never treated as duplicate candidates
        however coincidentally their bytes match."""
        return DiffHunk(
            file_path=path, new_start=5,
            new_lines=frozenset({5}),
            body=f"@@ -5,1 +5,1 @@\n-old\n+{content}",
        )

    def test_second_byte_identical_copy_is_excluded_and_named(self):
        content = "line\n" * 60  # well over _MIN_DUPLICATE_CHARS
        original = self._new_file("production/zippie/travel/bond-agent/x.py", content)
        copy = self._new_file(
            "production/oke/manifests/zippie-home/zippie-pkg/x.py", content,
        )
        kept, duplicates = split_duplicate_hunks((original, copy))

        assert [h.file_path for h in kept] == [original.file_path]
        assert duplicates == ((copy.file_path, original.file_path),)

    def test_first_occurrence_is_always_kept_reviewable(self):
        """The exclusion always points a LATER path at an EARLIER one -
        never the reverse - so there is always exactly one reviewable
        instance of any duplicated content."""
        content = "yyyyyyyyyy\n" * 60  # well over _MIN_DUPLICATE_CHARS
        first = self._new_file("a/one.py", content)
        second = self._new_file("b/two.py", content)
        third = self._new_file("c/three.py", content)

        kept, duplicates = split_duplicate_hunks((first, second, third))

        assert kept == (first,)
        assert duplicates == (
            ("b/two.py", "a/one.py"),
            ("c/three.py", "a/one.py"),
        )

    def test_non_identical_copy_is_reviewed_in_full(self):
        original = self._new_file("a/one.py", "same\n" * 60)
        different = self._new_file("b/two.py", "different\n" * 60)

        kept, duplicates = split_duplicate_hunks((original, different))

        assert set(h.file_path for h in kept) == {"a/one.py", "b/two.py"}
        assert duplicates == ()

    def test_partial_edit_is_never_treated_as_a_duplicate_candidate(self):
        """Two ordinary edits that happen to leave identical bytes on the
        new side must never be excluded - only a WHOLE new file (`-0,0`)
        qualifies. Reviewing a partial edit again is never wasted budget in
        the sense #813 means: it's the actual change."""
        same_text = "z" * 250
        edit_a = self._edit("a.py", same_text)
        edit_b = self._edit("b.py", same_text)

        kept, duplicates = split_duplicate_hunks((edit_a, edit_b))

        assert kept == (edit_a, edit_b)
        assert duplicates == ()

    def test_small_identical_files_are_not_flagged(self):
        """Below `_MIN_DUPLICATE_CHARS`, a coincidental match (two empty
        `__init__.py`) is not worth excluding - and costs nothing to review
        anyway."""
        a = self._new_file("pkg1/__init__.py", "")
        b = self._new_file("pkg2/__init__.py", "")

        kept, duplicates = split_duplicate_hunks((a, b))

        assert kept == (a, b)
        assert duplicates == ()

    def test_no_hunks_is_a_no_op(self):
        assert split_duplicate_hunks(()) == ((), ())


def _new_file_diff(path: str, content: str, blob: str) -> str:
    """GitHub's unified-diff shape for one ADDED file, index line included."""
    lines = content.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        f"index 0000000..{blob}\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


_EDIT_DIFF = """diff --git a/src/edited.py b/src/edited.py
index 1111111..2222222 100644
--- a/src/edited.py
+++ b/src/edited.py
@@ -1,2 +1,2 @@
 keep
-old
+new
"""


class TestNewFileBlobIds:
    def test_maps_each_added_file_to_its_new_side_blob(self):
        diff = (
            _new_file_diff("a/one.py", "x\n" * 5, "abc1234")
            + _EDIT_DIFF
            + _new_file_diff("b/two.py", "y\n" * 5, "def5678")
        )
        assert new_file_blob_ids(diff) == {
            "a/one.py": "abc1234", "b/two.py": "def5678",
        }

    def test_an_edited_file_is_not_an_added_file(self):
        assert new_file_blob_ids(_EDIT_DIFF) == {}

    def test_a_new_file_without_an_index_line_is_left_out(self):
        diff = _new_file_diff("a/one.py", "x\n", "abc1234").replace(
            "index 0000000..abc1234\n", "",
        )
        assert new_file_blob_ids(diff) == {}

    def test_empty_diff(self):
        assert new_file_blob_ids("") == {}


class TestTreeCopyHunkSplit:
    """#813's literal repro: an ADDED file whose bytes equal a
    file that already exists in the repo, untouched by the diff. The
    same-diff split above cannot see the original; the head tree can."""

    _CONTENT = "def transport():\n    return 1\n" * 20  # over the min size
    _FULL = "abc1234" + "0" * 33

    def _hunk(self, path, content=None):
        content = self._CONTENT if content is None else content
        hunks = parse_diff(_new_file_diff(path, content, "abc1234"))
        assert len(hunks) == 1
        return hunks[0]

    def test_copy_of_an_untouched_tree_file_is_excluded_and_named(self):
        copy = self._hunk("manifests/pkg/transport.py")
        kept, copies = split_tree_copy_hunks(
            (copy,),
            {"manifests/pkg/transport.py": "abc1234"},
            {
                "agent/core/transport.py": self._FULL,
                "manifests/pkg/transport.py": self._FULL,
                "README.md": "f" * 40,
            },
        )
        assert kept == ()
        assert copies == (
            ("manifests/pkg/transport.py", "agent/core/transport.py"),
        )

    def test_near_duplicate_is_reviewed_in_full(self):
        """A copy that is NOT byte-identical has a different blob, so it
        never matches - the tree says so even when the diff abbreviation
        would collide."""
        near = self._hunk("manifests/pkg/transport.py", self._CONTENT + "x = 2\n")
        kept, copies = split_tree_copy_hunks(
            (near,),
            {"manifests/pkg/transport.py": "abc1234"},
            {
                "agent/core/transport.py": self._FULL,
                "manifests/pkg/transport.py": "abc1234" + "9" * 33,
            },
        )
        assert kept == (near,)
        assert copies == ()

    def test_tree_that_disagrees_with_the_diff_excludes_nothing(self):
        """The head tree must confirm the diff's own blob id; a tree read at
        some other commit is not proof of anything."""
        copy = self._hunk("manifests/pkg/transport.py")
        other = "fff0000" + "0" * 33
        kept, copies = split_tree_copy_hunks(
            (copy,),
            {"manifests/pkg/transport.py": "abc1234"},
            {"agent/core/transport.py": other, "manifests/pkg/transport.py": other},
        )
        assert kept == (copy,)
        assert copies == ()

    def test_original_must_not_itself_be_added_by_this_diff(self):
        """Two new identical files are the same-diff split's job (first one
        kept). Matching them against each other here would drop BOTH and
        leave no reviewed instance of those bytes."""
        a = self._hunk("a/one.py")
        b = self._hunk("b/two.py")
        kept, copies = split_tree_copy_hunks(
            (a, b),
            {"a/one.py": "abc1234", "b/two.py": "abc1234"},
            {"a/one.py": self._FULL, "b/two.py": self._FULL},
        )
        assert kept == (a, b)
        assert copies == ()

    def test_small_file_and_edits_are_never_candidates(self):
        tiny = self._hunk("pkg/__init__.py", "x = 1\n")
        edit = parse_diff(_EDIT_DIFF)[0]
        kept, copies = split_tree_copy_hunks(
            (tiny, edit),
            {"pkg/__init__.py": "abc1234"},
            {
                "pkg/__init__.py": self._FULL,
                "other/__init__.py": self._FULL,
                "src/edited.py": self._FULL,
            },
        )
        assert kept == (tiny, edit)
        assert copies == ()

    def test_no_tree_means_no_exclusion(self):
        copy = self._hunk("manifests/pkg/transport.py")
        assert split_tree_copy_hunks(
            (copy,), {"manifests/pkg/transport.py": "abc1234"}, {},
        ) == ((copy,), ())

    def test_original_is_chosen_deterministically(self):
        copy = self._hunk("z/copy.py")
        _, copies = split_tree_copy_hunks(
            (copy,),
            {"z/copy.py": "abc1234"},
            {"m/b.py": self._FULL, "m/a.py": self._FULL, "z/copy.py": self._FULL},
        )
        assert copies == (("z/copy.py", "m/a.py"),)
