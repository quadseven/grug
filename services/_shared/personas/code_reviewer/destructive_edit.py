"""Deterministic check for a diff that wipes out a file nobody asked to change (#1365).

A lane PR replaced a repository's 101-line root README with a write-up of
its ticket. Others rewrote a doc until it contradicted the recorded result
below it, or deleted a deploy doc's working steps. Elder reads a docs
change as harmless, so none of these drew a finding.

One high `destructive-edit` finding per file when the diff deletes at
least `REPLACED_SHARE` of the file's base lines (a rename counts from the
old path, and a rename git scores as mostly dissimilar counts too), the
deletion clears a size floor, and neither the PR title nor any linked
criterion names the file. Top-level README, AGENTS and CLAUDE files are
the repository's front door and agent instructions, so they are checked
from a lower floor.

Not checked: test files (weakened tests are their own check), data,
lock and generated files (`is_review_excluded_path`), and files whose
base size is unknown, which are skipped rather than guessed at.

Pure: file changes, base line counts and PR text in; findings out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import (
    DiffHunk,
    FileChange,
    is_review_excluded_path,
)
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

RULE = "destructive-edit"
# Share of the base file's lines the diff deletes.
REPLACED_SHARE = 0.6
# Fewest deleted lines that count, so trimming a short file is not a finding.
MIN_DELETED = 25
MIN_DELETED_FRONT_DOOR = 8
# Share of the deleted lines the diff adds back elsewhere at or above which
# the edit is a move or refactor, not a loss. Measured on the lane corpus: a
# handler refactor re-added 32% of what it deleted, a README replaced by a
# ticket write-up 2%.
MOVED_SHARE = 0.2
_FRONT_DOOR_STEMS =frozenset({"readme", "agents", "claude"})
# A stem this short or generic names too much prose to prove a mention.
_MIN_STEM = 4


def _is_front_door(path: str) -> bool:
    if "/" in path:
        return False
    return path.rsplit(".", 1)[0].lower() in _FRONT_DOOR_STEMS


def _named(path: str, text: str) -> bool:
    """The title or a criterion names this file: its path, basename, or a
    stem of at least `_MIN_STEM` characters (`README` names `README.md`)."""
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    tokens = {path, name}
    if len(stem) >= _MIN_STEM:
        tokens.add(stem)
        tokens.add(stem.replace("-", " ").replace("_", " "))
    lowered = text.lower()
    return any(
        re.search(rf"(?<![\w-]){re.escape(t.lower())}(?![\w-])", lowered) for t in tokens
    )


def _deleted_share(change: FileChange, base_lines: int | None) -> float | None:
    if base_lines:
        return min(1.0, change.removed / base_lines)
    if change.status == "renamed" and change.similarity is not None:
        return 1.0 - change.similarity / 100
    return None


def _moved_share(path: str, hunks: tuple[DiffHunk, ...]) -> float:
    """Share of the file's deleted lines that the diff adds back somewhere
    (moved into another file or reordered): a refactor relocates content,
    a replacement loses it. Blank lines and lone brackets are ignored."""
    removed: list[str] = []
    added: set[str] = set()
    for hunk in hunks:
        for raw in hunk.body.splitlines()[1:]:
            text = raw[1:].strip()
            if len(text) < 3:
                continue
            if raw.startswith("-") and hunk.file_path == path:
                removed.append(text)
            elif raw.startswith("+"):
                added.add(text)
    if not removed:
        return 0.0
    return sum(1 for line in removed if line in added) / len(removed)


def _anchor(path: str, hunks: Iterable[DiffHunk]) -> tuple[str, int] | None:
    own = [h for h in hunks if h.file_path == path]
    for hunk in own:
        if hunk.new_lines:
            return path, min(hunk.new_lines)
    if own:
        return path, own[0].new_start
    return None


def _finding(
    change: FileChange, anchor: tuple[str, int], base_lines: int | None, share: float,
) -> Finding:
    if base_lines:
        lost = f"deletes {change.removed} of the {base_lines} lines"
    else:
        lost = f"keeps only {round((1 - share) * 100)}% of the content"
    where = f"`{change.path}`"
    if change.status == "renamed" and change.previous_path:
        where = f"`{change.previous_path}` (renamed to `{change.path}`)"
    added = f" and adds {change.added} new ones" if change.added else ""
    return Finding(
        file=anchor[0],
        line=anchor[1],
        severity="high",
        rule_name=RULE,
        message=(
            f"This diff {lost} in {where}{added} ({round(share * 100)}% of the "
            "file), but neither the PR title nor the linked criteria name this "
            "file. Restore what the ticket did not ask to remove, or name the "
            "file in the ticket if replacing it is the point."
        ),
        suggestion=None,
        effort="quick-win",
    )


def _check(
    change: FileChange, base_line_counts: dict[str, int], asked: str,
    hunks: tuple[DiffHunk, ...],
) -> Finding | None:
    """The finding for one changed file, or None."""
    old = change.previous_path or change.path
    if change.status not in ("modified", "renamed"):
        return None
    if is_test_file(old) or is_review_excluded_path(old):
        return None
    floor = MIN_DELETED_FRONT_DOOR if _is_front_door(old) else MIN_DELETED
    base_lines = base_line_counts.get(old)
    share = _deleted_share(change, base_lines)
    if change.removed < floor or share is None or share < REPLACED_SHARE:
        return None
    if _named(old, asked) or _named(change.path, asked):
        return None
    if _moved_share(change.path, hunks) >= MOVED_SHARE:
        return None
    anchor = _anchor(change.path, hunks)
    return _finding(change, anchor, base_lines, share) if anchor else None


def scan_destructive_edits(
    changes: Iterable[FileChange],
    base_line_counts: dict[str, int],
    *,
    criteria: tuple[LinkedCriterion, ...],
    pr_title: str,
    hunks: tuple[DiffHunk, ...],
) -> tuple[Finding, ...]:
    """One finding per mostly-deleted file the title and criteria do not name."""
    asked = "\n".join([pr_title or "", *(c.text for c in criteria)])
    found = (_check(change, base_line_counts, asked, hunks) for change in changes)
    return tuple(f for f in found if f is not None)
