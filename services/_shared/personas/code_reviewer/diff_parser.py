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

import hashlib
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


# Paths whose diffs carry no review signal for an LLM and actively degrade
# the arms (#609): a large JSONL/lockfile hunk balloons the prompt past what
# the models answer coherently (observed live: parse_failed on a PR whose
# diff was mostly a data file) and slows every review. Conservative,
# extension/dir-based; excluded paths are NAMED in the check summary so the
# walkthrough stays honest, and full-file context still covers code files.
_REVIEW_EXCLUDED_PATH_RE = re.compile(
    r"(?:^|/)("
    r"node_modules|vendor|__snapshots__|dist"
    r")/"
    r"|\.(?:jsonl|csv|tsv|parquet|min\.js|min\.css|svg|map|lock)$"
    r"|(?:^|/)(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|"
    r"poetry\.lock|Cargo\.lock|go\.sum|Gemfile\.lock|composer\.lock)$"
)


def is_review_excluded_path(path: str) -> bool:
    """True when a changed file is data/generated/vendored - no LLM review
    signal (#609). Pure; the single source the split + tests share."""
    return _REVIEW_EXCLUDED_PATH_RE.search(path) is not None


def split_reviewable_hunks(
    hunks: tuple[DiffHunk, ...],
) -> tuple[tuple[DiffHunk, ...], tuple[str, ...]]:
    """Partition hunks into (reviewable, excluded_paths).

    Excluded paths are deduped, order-preserving, so the check summary can
    name exactly what the LLM did not see. Pure."""
    kept: list[DiffHunk] = []
    excluded: dict[str, None] = {}
    for h in hunks:
        if is_review_excluded_path(h.file_path):
            excluded[h.file_path] = None
        else:
            kept.append(h)
    return tuple(kept), tuple(excluded)


# Matches the `@@` header's OLD side, re-parsed straight from `DiffHunk.body`
# (the parser discards old-start/old-count once new_lines is built, so a
# whole-new-file check has to look at the raw header text again).
_OLD_SIDE_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+")

# Below this many characters, an identical hunk is more likely a coincidence
# (an empty `__init__.py`, a one-line stub) than a real byte-identical copy
# worth excluding - and excluding it buys nothing since it was cheap to
# review anyway.
_MIN_DUPLICATE_CHARS = 200


def _whole_new_file_content(hunk: DiffHunk) -> str | None:
    """The full added-file text when `hunk` is git's shape for a BRAND NEW
    file (`@@ -0,0 +1,N @@`, every content line added) - `None` otherwise.

    `-0,0` is git's convention for "the old side had nothing" and is also
    (ambiguously, a real unified-diff limitation) how a pure insertion at
    the very top of an existing non-empty file can render. Guarded by
    `_MIN_DUPLICATE_CHARS` in the caller and an EXACT content match against
    another hunk in the same diff, so the failure mode of that ambiguity is
    at worst excluding one boilerplate-shaped block from review - never a
    wrong finding, and always named in the board (#813 acceptance: no
    silent exclusion)."""
    m = _OLD_SIDE_RE.match(hunk.body.splitlines()[0]) if hunk.body else None
    if not m or m.group(1) != "0" or m.group(2) != "0":
        return None
    lines: list[str] = []
    for raw in hunk.body.splitlines()[1:]:
        if raw.startswith("\\"):
            continue
        if not raw.startswith("+"):
            return None
        lines.append(raw[1:])
    return "\n".join(lines)


def split_duplicate_hunks(
    hunks: tuple[DiffHunk, ...],
) -> tuple[tuple[DiffHunk, ...], tuple[tuple[str, str], ...]]:
    """Partition hunks into (reviewable, duplicates).

    `duplicates` is `((path, original_path), ...)` in encounter order: a
    byte-identical whole-file copy of `original_path`, ADDED earlier in the
    SAME diff. Reviewing the same bytes twice teaches the LLM nothing the
    first pass didn't already establish, and it is not free - #813 measured
    a PR losing coverage on its genuinely novel logic because ~60KB of
    provably-unchanged copies ate the cohort budget before Elder ever
    reached the real change.

    Scope: this catches only SAME-DIFF duplicates (a file copied to two
    locations in one PR - the exact #813 shape, common when a build tool
    like kustomize can't symlink outside its root). A copy of a file that
    already exists elsewhere in the repo, untouched by this diff, is
    invisible here - detecting that needs the rest of the tree, which this
    pure diff-only function does not have access to (tracked separately;
    would need a repo-tree fetch this module deliberately does not make).

    Only whole-new-file hunks (see `_whole_new_file_content`) at or above
    `_MIN_DUPLICATE_CHARS` are compared; a partial edit is never excluded,
    however coincidentally its bytes match another hunk. The FIRST
    occurrence of any given content is always kept reviewable - only later
    occurrences are named as duplicates of it. Pure - no IO."""
    kept: list[DiffHunk] = []
    duplicates: list[tuple[str, str]] = []
    first_path_for_hash: dict[str, str] = {}
    for h in hunks:
        content = _whole_new_file_content(h)
        if content is None or len(content) < _MIN_DUPLICATE_CHARS:
            kept.append(h)
            continue
        digest = hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()
        original = first_path_for_hash.get(digest)
        if original is None:
            first_path_for_hash[digest] = h.file_path
            kept.append(h)
        else:
            duplicates.append((h.file_path, original))
    return tuple(kept), tuple(duplicates)


# `index <old>..<new>[ <mode>]` - git's abbreviated blob ids for the file.
_INDEX_RE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)(?: \d+)?$")
# Below this, an abbreviated blob id is too short to trust as a prefix.
_MIN_BLOB_ABBREV = 7


def new_file_blob_ids(unified_diff: str) -> dict[str, str]:
    """`{path: abbreviated new-side blob id}` for each file the diff ADDS.

    Read from the per-file header (`new file mode` + `index 0000000..<id>`),
    keyed by the `+++ b/<path>` path when present so it lines up with
    `DiffHunk.file_path`. An added file with no index line is left out, so
    it can never be matched against anything. Pure."""
    out: dict[str, str] = {}
    for header in _file_headers(unified_diff):
        found = _added_file_blob(header)
        if found:
            out[found[0]] = found[1]
    return out


def _file_headers(unified_diff: str) -> list[list[str]]:
    """Each file's header lines: `diff --git` up to its first `@@`. Body
    lines never land here, so a content line that happens to read like
    `+++ b/x` or `index 0..1` cannot be mistaken for a header. Pure."""
    headers: list[list[str]] = []
    collecting = False
    for line in unified_diff.splitlines():
        if line.startswith("diff --git "):
            headers.append([line])
            collecting = True
        elif line.startswith("@@"):
            collecting = False
        elif collecting:
            headers[-1].append(line)
    return headers


def _added_file_blob(header: list[str]) -> tuple[str, str] | None:
    """`(path, abbreviated blob id)` when this header ADDS a file and carries
    its index line; `None` for an edit, rename, deletion or a header with no
    index line. Pure."""
    if not any(line.startswith("new file mode ") for line in header):
        return None
    m = _DIFF_GIT_RE.match(header[0])
    path = m.group(2) if m else ""
    blob = ""
    for line in header[1:]:
        plus = _NEW_FILE_RE.match(line) if line.startswith("+++ ") else None
        if plus and plus.group(1) != "/dev/null":
            path = plus.group(1)
        index = _INDEX_RE.match(line)
        if index and set(index.group(1)) == {"0"}:
            blob = index.group(2)
    return (path, blob) if path and blob else None


def split_tree_copy_hunks(
    hunks: tuple[DiffHunk, ...],
    new_blob_ids: dict[str, str],
    head_blobs: dict[str, str],
) -> tuple[tuple[DiffHunk, ...], tuple[tuple[str, str], ...]]:
    """Partition hunks into (reviewable, copies) using the HEAD tree.

    `copies` is `((path, original_path), ...)`: an ADDED file whose blob is
    byte-identical to `original_path`, a path that exists at head and is
    NOT itself added by this diff - so it was already in the repo, either
    untouched (pre-existing bytes) or edited here (its edit is reviewed
    through its own hunks). This is #813's literal repro: six modules copied under a build
    tool's root, the originals untouched, and the copies ate the budget
    the novel logic needed.

    `new_blob_ids` is `new_file_blob_ids(diff)`; `head_blobs` is
    `{path: full blob sha}` from the head commit's tree. Byte identity is
    git's own blob hash, never a rebuild of the content from diff lines.
    The head tree must CONFIRM the diff's abbreviated id for the added
    path itself, so a tree read at any other commit excludes nothing.
    Only whole-new-file hunks at or above `_MIN_DUPLICATE_CHARS` qualify,
    like `split_duplicate_hunks`. The original is the lexically first
    match, so the board names the same path on every run. Pure - no IO."""
    if not (new_blob_ids and head_blobs):
        return hunks, ()
    originals_by_blob: dict[str, list[str]] = {}
    for path, blob in head_blobs.items():
        if path not in new_blob_ids:
            originals_by_blob.setdefault(blob, []).append(path)
    kept: list[DiffHunk] = []
    copies: list[tuple[str, str]] = []
    for h in hunks:
        originals = (
            originals_by_blob.get(head_blobs.get(h.file_path, ""))
            if is_tree_copy_candidate(h, new_blob_ids, head_blobs)
            else None
        )
        if originals:
            copies.append((h.file_path, min(originals)))
        else:
            kept.append(h)
    return tuple(kept), tuple(copies)


def is_tree_copy_candidate(
    hunk: DiffHunk,
    new_blob_ids: dict[str, str],
    head_blobs: dict[str, str] | None = None,
) -> bool:
    """Whether `hunk` is an added whole file big enough to be worth matching
    against the head tree. With `head_blobs`, also require that the tree
    confirms the diff's own abbreviated blob id for this path. The caller
    uses the tree-free form to skip the tree fetch for PRs with nothing to
    match. Pure."""
    abbrev = new_blob_ids.get(hunk.file_path, "")
    if len(abbrev) < _MIN_BLOB_ABBREV:
        return False
    if head_blobs is not None and not head_blobs.get(hunk.file_path, "").startswith(abbrev):
        return False
    content = _whole_new_file_content(hunk)
    return content is not None and len(content) >= _MIN_DUPLICATE_CHARS


def split_oversized_hunks(
    hunks: tuple[DiffHunk, ...], max_hunk_chars: int,
) -> tuple[tuple[DiffHunk, ...], tuple[str, ...]]:
    """Partition hunks into (reviewable, oversized_paths).

    A hunk larger than one whole cohort cannot be reviewed: the planner
    refuses to truncate it (truncation corrupts the line anchors the
    hallucination filter depends on), so it becomes a solo cohort that
    `_oversized_cohort_failure` fails WITHOUT ever calling a model. That
    turned an unreviewable blob into three separate harms - a guaranteed
    cohort failure, a `partial_review` flag that suppressed the whole
    check, and an inflated `total_diff_chars` that split the rest of the
    diff into more cohorts than the wall-clock budget could run.

    Measured live: one generated `.json` rewritten by 22,602
    lines produced a 1,004,156-char hunk against a 48,000 cap. It cost
    cohorts 5 and 9 (auto-failed) AND cohorts 15-18 (budget exhausted,
    never attempted) - four healthy cohorts lost to one blob.

    Naming it here instead reuses the honest-omission channel
    `split_reviewable_hunks` already established: what Elder could not
    read is listed in the check summary rather than silently degrading
    the verdict for the paths it read fine.

    Per HUNK, not per path - a hand-edited hunk in a file that also
    carries a generated block still gets reviewed. Paths are deduped and
    order-preserving. A non-positive budget means unbounded, so a missing
    or misparsed config value can never drop the whole diff. Pure."""
    if max_hunk_chars <= 0:
        return hunks, ()
    kept: list[DiffHunk] = []
    oversized: dict[str, None] = {}
    for h in hunks:
        if len(h.body) > max_hunk_chars:
            oversized[h.file_path] = None
        else:
            kept.append(h)
    return tuple(kept), tuple(oversized)


def added_lines(hunk: DiffHunk) -> list[tuple[int, str]]:
    """New-side (lineno, text) for each ADDED line in a hunk.

    Unified-diff walk: advance only on added/context lines. Annotation
    lines (backslash + " No newline at end of file") never advance. A raw
    line starting with ``+`` is always an addition (including content that
    itself begins with ``++``) - do not special-case ``+++`` as a header
    inside the hunk body (file headers live outside DiffHunk.body).
    """
    out: list[tuple[int, str]] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith("@@") or raw.startswith(chr(92)):
            continue  # header / no-newline marker - no new-side advance
        if raw.startswith("+"):
            out.append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue
        else:
            lineno += 1  # context (leading space or empty)
    return out


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
                # A zero START is only legal with a zero COUNT: `+0,0`.
                # `+0,N` / a bare `+0` (implied count 1) are malformed -
                # fall through to fail loudly rather than silently
                # swallowing hunks (FLINT PR #580).
                if m.group(2) != "0":
                    raise DiffParseError(
                        f"malformed zero-start hunk header at line {i + 1}: {line!r}"
                    )
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


@dataclass(frozen=True, slots=True)
class FileChange:
    """One file's place in a diff: its status and how many lines moved.

    `status` is "added", "removed", "renamed" or "modified". `similarity`
    is git's rename similarity (0-100), None when the diff gives none.
    `added`/`removed` count content lines, not headers."""

    path: str
    status: str
    previous_path: str | None
    similarity: int | None
    added: int
    removed: int


_SIMILARITY_RE = re.compile(r"^similarity index (\d+)%$")


def _block_header(
    lines: list[str], old: str, new: str,
) -> tuple[str, str | None, int | None, str, int]:
    """(status, previous path, similarity, new path, index of the first
    hunk line) from one file block's extended header."""
    status, previous, similarity = "modified", None, None
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            return status, previous, similarity, new, i
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "removed"
        elif line.startswith("rename from "):
            status, previous = "renamed", line[len("rename from "):]
        elif line.startswith("+++ "):
            target = _NEW_FILE_RE.match(line)
            if target and target.group(1) != "/dev/null":
                new = target.group(1)
        elif sim := _SIMILARITY_RE.match(line):
            similarity = int(sim.group(1))
    if status == "renamed" and previous is None:
        previous = old
    return status, previous, similarity, new, len(lines)


def file_changes(unified_diff: str) -> tuple[FileChange, ...]:
    """Per-file status and line counts, in diff order.

    Unlike `parse_diff`, deleted files and pure renames are kept: a check
    that asks what a diff removed has to see them. Pure; a block without a
    `diff --git` header is skipped, never raised on, since `parse_diff`
    already refuses malformed input on the same text."""
    out: list[FileChange] = []
    for block in re.split(r"(?m)^(?=diff --git )", unified_diff or ""):
        lines = block.splitlines()
        m = _DIFF_GIT_RE.match(lines[0]) if lines else None
        if not m:
            continue
        old = m.group(1)
        status, previous, similarity, new, start = _block_header(lines[1:], old, m.group(2))
        body = lines[1 + start:]
        added = sum(1 for line in body if line.startswith("+"))
        removed = sum(1 for line in body if line.startswith("-"))
        path = old if status == "removed" else new
        out.append(FileChange(path, status, previous, similarity, added, removed))
    return tuple(out)
