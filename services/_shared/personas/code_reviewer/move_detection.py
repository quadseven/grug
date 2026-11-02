"""Relocations Elder must not review as new code (#772).

A pull request that moves files without changing them carries no new code,
so reviewing it as new spends the cohort budget and the author's replies on
lines nobody wrote. Three shapes are provably unchanged and are dropped
before cohort planning, then named in one summary line:

- a rename git reports at 100% similarity (it has no hunks to review);
- a whole-file delete plus a whole-file add whose git blob ids match;
- a file added under the directory a `git subtree add` commit names in its
  `git-subtree-dir` trailer, whose blob id equals the blob at the same
  relative path in the tree of its `git-subtree-split` commit.

Proof is always git's own blob id, never a rebuild of content from diff
lines. A file edited after it moved has a different blob id, so it stays in
review; a rename under 100% similarity keeps its hunks, which hold only the
changed lines. Anything missing (a tree that was not fetched, a short blob
id) means no proof, and the file is reviewed as usual.

Pure: text in, pairs out. The fetches live in dispatch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from personas.code_reviewer.diff_parser import (
    DiffHunk, deleted_file_blob_ids, file_changes, new_file_blob_ids,
)

# Below this, an abbreviated blob id is too short to trust as a prefix.
_MIN_BLOB_ABBREV = 7
_SUBTREE_DIR_RE = re.compile(r"^git-subtree-dir:[ \t]*(\S.*?)[ \t]*$", re.M)
_SUBTREE_SPLIT_RE = re.compile(r"^git-subtree-split:[ \t]*([0-9a-f]{40})[ \t]*$", re.M)


@dataclass(frozen=True, slots=True)
class SubtreeImport:
    """One `git subtree add` or `merge`: the directory it fills and the
    commit of the source repo whose tree it copied."""

    directory: str
    split_sha: str


def subtree_imports(messages: tuple[str, ...]) -> tuple[SubtreeImport, ...]:
    """The subtree imports named by commit-message trailers, in order.

    A message needs both `git-subtree-dir` and a 40-hex
    `git-subtree-split`. A directory that is absolute or climbs out with
    `..` is dropped, so a trailer cannot claim a path outside the repo."""
    out: list[SubtreeImport] = []
    for message in messages:
        d, s = _SUBTREE_DIR_RE.search(message), _SUBTREE_SPLIT_RE.search(message)
        if not (d and s):
            continue
        directory = d.group(1).strip("/")
        if directory and ".." not in directory.split("/"):
            imp = SubtreeImport(directory, s.group(1))
            if imp not in out:
                out.append(imp)
    return tuple(out)


def _same_blob(abbrev: str, other: str) -> bool:
    """Whether two blob ids, either possibly abbreviated, name one blob."""
    short = min(len(abbrev), len(other))
    return short >= _MIN_BLOB_ABBREV and abbrev[:short] == other[:short]


def _pure_renames(diff_text: str) -> list[tuple[str, str]]:
    return [
        (c.previous_path, c.path)
        for c in file_changes(diff_text)
        if c.status == "renamed" and c.similarity == 100 and c.previous_path
    ]


def _delete_add_pairs(diff_text: str) -> list[tuple[str, str]]:
    """Each deleted file paired with at most one added file of the same
    blob; an added file is used once, in diff order."""
    deleted = deleted_file_blob_ids(diff_text)
    added = new_file_blob_ids(diff_text)
    used: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for old_path, old_blob in deleted.items():
        match = next(
            (p for p, b in added.items() if p not in used and _same_blob(old_blob, b)),
            None,
        )
        if match:
            used.add(match)
            pairs.append((old_path, match))
    return pairs


def _subtree_pairs(
    diff_text: str,
    imports: tuple[SubtreeImport, ...],
    split_trees: dict[str, dict[str, str]],
    skip: set[str],
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for path, blob in new_file_blob_ids(diff_text).items():
        if path in skip:
            continue
        for imp in imports:
            prefix = imp.directory + "/"
            tree = split_trees.get(imp.split_sha, {})
            source = tree.get(path[len(prefix):]) if path.startswith(prefix) else None
            if source and _same_blob(blob, source):
                pairs.append((f"{imp.split_sha[:7]}:{path[len(prefix):]}", path))
                break
    return pairs


def find_moves(
    diff_text: str,
    imports: tuple[SubtreeImport, ...] = (),
    split_trees: dict[str, dict[str, str]] | None = None,
) -> tuple[tuple[str, str], ...]:
    """`((source, destination), ...)` for every provable relocation in the
    diff, in the order: 100% renames, delete+add pairs, subtree imports.
    `split_trees` is `{split sha: {path: blob sha}}`; a split commit absent
    from it proves nothing. Pure."""
    renames = _pure_renames(diff_text)
    pairs = _delete_add_pairs(diff_text)
    taken = {dest for _, dest in pairs}
    subtree = _subtree_pairs(diff_text, imports, split_trees or {}, taken)
    return tuple(renames + pairs + subtree)


def split_move_hunks(
    hunks: tuple[DiffHunk, ...],
    diff_text: str,
    imports: tuple[SubtreeImport, ...] = (),
    split_trees: dict[str, dict[str, str]] | None = None,
) -> tuple[tuple[DiffHunk, ...], tuple[tuple[str, str], ...]]:
    """Partition hunks into (reviewable, moves).

    `moves` is `find_moves(...)`. Hunks of a moved destination are dropped;
    every other hunk, including those of a renamed file with edits, is kept
    unchanged. Pure."""
    moves = find_moves(diff_text, imports, split_trees)
    moved = {dest for _, dest in moves}
    return tuple(h for h in hunks if h.file_path not in moved), moves
