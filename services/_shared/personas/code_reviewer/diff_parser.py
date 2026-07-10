"""Pure unified-diff parser for the Elder (code-reviewer) persona.

Takes a unified-diff string fetched from `GET .../pulls/{n}` (Accept:
application/vnd.github.diff) and extracts structured hunks. Pure: no IO,
no logging side-effects, no hidden globals. Spec 0015 §Parse contract.

Why a hand-rolled parser and not unidiff/pypatch:
- Both services ship as Lambda images; every dep adds cold-start weight.
  The unified-diff subset we actually need is small (~80 lines) — fewer
  than the dep would add to requirements.
- We need to track new-side line numbers for hallucination filtering
  (see `new_lines`), which most third-party parsers expose awkwardly.
- Pure-function purity is attestable by spec; an opaque dep wouldn't be.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class DiffParseError(ValueError):
    """Parser refused to silently swallow malformed diff input.

    Distinct from `parse_diff("")` returning `()` — empty input is a
    valid "no changes" case (e.g. PR body that touches nothing). This
    exception fires only when the input *looks* like a diff but a
    header (`diff --git ...` or `@@ ... @@`) failed to match the
    expected shape. Silently skipping would let an upstream fetcher
    bug or a GitHub format change masquerade as "clean PR" — caller
    should catch and treat as `parse_failed` (advisory neutral)."""


@dataclass(frozen=True, slots=True)
class DiffHunk:
    """One @@ ... @@ block in one file's portion of the diff.

    `new_lines` is the set of new-side line numbers that are added OR
    context-with-a-removed-neighbor. The Elder persona's anti-
    hallucination filter (`evaluate_diff`) rejects LLM findings whose
    `(file, line)` is not in any hunk's `new_lines` — a finding on a
    line the LLM couldn't have seen is almost certainly invented.

    `body` retains the raw @@-prefixed hunk text for feeding back to
    the LLM as review context (matches `llm_client.Hunk(path, body)`).

    Invariants enforced in `__post_init__`: non-empty `file_path`,
    `new_start >= 1` (1-based line numbers per unified-diff spec),
    `body` starts with `@@`. Each invariant catches a parser regression
    at the boundary rather than letting a malformed hunk reach the LLM.
    They raise `DiffParseError` — NOT `assert` — because the dispatch
    degrade contract only catches DiffParseError: an AssertionError here
    escaped both personas' catch clauses and crash-looped the consumer
    into the rerun DLQ (grug PR #577 emptied-file hunk, 2026-07-10).
    """

    file_path: str
    new_start: int
    new_lines: frozenset[int]
    body: str

    def __post_init__(self) -> None:
        if not self.file_path:
            raise DiffParseError("DiffHunk.file_path must be non-empty")
        if self.new_start < 1:
            raise DiffParseError(
                f"DiffHunk.new_start must be >= 1 (got {self.new_start}); "
                "unified-diff line numbers are 1-based"
            )
        if not self.body.startswith("@@"):
            raise DiffParseError(
                "DiffHunk.body must start with the @@ hunk header"
            )


# Captures `+++ b/<path>` or `+++ /dev/null` (deletion). Group 1 is the
# path with the leading `b/` stripped, or "/dev/null" verbatim.
_NEW_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")
# `diff --git a/<old> b/<new>` — fallback when --- / +++ are absent
# (pure renames have no @@ block at all).
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
# `@@ -<old>,<n> +<new>,<m> @@` — captures the new-side start + count.
# Count is optional (defaults to 1 when absent per the diff spec).
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
# `Binary files a/... and b/... differ` — skip these entirely.
_BINARY_RE = re.compile(r"^Binary files .+ and .+ differ$")

# Prefixes that end one hunk's body and start the next file / hunk /
# header section. Tuple so a single `startswith(_BOUNDARY)` call covers all four.
_HUNK_BOUNDARY_PREFIXES: tuple[str, ...] = (
    "diff --git ", "@@", "+++ ", "--- ",
)


def parse_diff(unified_diff: str) -> tuple[DiffHunk, ...]:
    """Parse a unified diff into structured hunks.

    Pure: no logging, no IO. Empty input → empty tuple. Binary file
    blocks produce no hunks. Pure renames (no @@ block) produce no
    hunks. The new path is used as `file_path` for rename+edit cases."""
    if not unified_diff:
        return ()

    lines = unified_diff.splitlines()
    hunks: list[DiffHunk] = []
    current_file: str | None = None
    binary_skip = False
    deletion_skip = False
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(line)
            if not m:
                # `diff --git` header that doesn't match the standard
                # `a/<old> b/<new>` shape — refuse to silently swallow.
                # Earlier behavior set `current_file=None`, which
                # silently dropped every subsequent hunk in this file:
                # the LLM saw nothing, evaluate_diff returned a clean
                # "success" verdict — a false pass. Raise instead so
                # the caller treats this as parse_failed.
                raise DiffParseError(
                    f"malformed `diff --git` header at line {i + 1}: "
                    f"{line!r}"
                )
            # Default to the post-rename / 'b/' side; +++ overrides
            # below if it disagrees (e.g. mode-only files have no +++).
            current_file = m.group(2)
            binary_skip = False
            deletion_skip = False
            i += 1
            continue

        if _BINARY_RE.match(line):
            binary_skip = True
            i += 1
            continue

        if line.startswith("+++ "):
            m = _NEW_FILE_RE.match(line)
            if m and m.group(1) == "/dev/null":
                # File deletion. Hunks follow with `@@ ... +0,0 @@`
                # (new-side start = 0). There's nothing on the new
                # side to review — skip the hunk block entirely.
                # Without this, DiffHunk.__post_init__ asserts on
                # new_start >= 1 and parse_diff crashes on real
                # GitHub-emitted deletion diffs.
                deletion_skip = True
            elif m:
                current_file = m.group(1)
            i += 1
            continue

        if binary_skip or deletion_skip or current_file is None:
            i += 1
            continue

        if line.startswith("@@"):
            m = _HUNK_HEADER_RE.match(line)
            if not m:
                # Malformed @@ header — refuse to silently skip. A
                # garbled hunk header is most likely a parser drift
                # (GitHub format change) or upstream-fetcher corruption.
                # Silently skipping let every hunk in the PR vanish
                # → evaluate_diff returned clean success → false pass.
                # Caller catches DiffParseError → advisory neutral.
                raise DiffParseError(
                    f"malformed `@@` hunk header at line {i + 1}: {line!r}"
                )
            new_start = int(m.group(1))
            if new_start == 0:
                # `+0,0` — the change leaves NOTHING on the new side to
                # review. `+++ /dev/null` deletions are skipped above, but
                # a file EMPTIED to zero bytes keeps its `+++ b/<path>`
                # line and still emits `@@ -1,N +0,0 @@` (GitHub does this
                # for truncate-to-empty commits). Consume the hunk body and
                # move on, exactly like the deletion case.
                i += 1
                while i < len(lines):
                    hline = lines[i]
                    if hline.startswith(_HUNK_BOUNDARY_PREFIXES):
                        break
                    i += 1
                continue
            # Walk the hunk body collecting added + context-with-removed
            # lines. Body capture starts at the @@ header so the LLM
            # gets full context.
            body_lines: list[str] = [line]
            new_lines_set: set[int] = set()
            new_cursor = new_start
            i += 1
            while i < len(lines):
                hline = lines[i]
                if hline.startswith(_HUNK_BOUNDARY_PREFIXES):
                    break
                body_lines.append(hline)
                if hline.startswith("+") and not hline.startswith("+++"):
                    new_lines_set.add(new_cursor)
                    new_cursor += 1
                elif hline.startswith("-") and not hline.startswith("---"):
                    pass  # removed — no new-side advance
                elif hline.startswith("\\"):
                    # `\ No newline at end of file` — a unified-diff
                    # annotation, NOT a content line. Including it in
                    # the cursor advance shifts every subsequent +line's
                    # number by 1, so the hallucination filter would
                    # then reject real findings as "outside the diff."
                    pass
                else:
                    # Context line. Advance the new cursor; don't mark
                    # as reviewable since it wasn't changed.
                    new_cursor += 1
                i += 1
            hunks.append(
                DiffHunk(
                    file_path=current_file,
                    new_start=new_start,
                    new_lines=frozenset(new_lines_set),
                    body="\n".join(body_lines),
                )
            )
            continue

        i += 1

    return tuple(hunks)
