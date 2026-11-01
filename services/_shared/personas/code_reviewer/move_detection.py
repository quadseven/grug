"""Detect moves: deletions + additions with identical content.

When a diff contains both a deletion and an addition of the same content,
treat the addition as a move of the deleted file rather than new code.

Only considers:
- Whole-file deletions (no @@ block)
- Whole-file additions (old side -0,0)
- Identical content (byte-for-byte match)

The function returns moves as (deleted_path, added_path) tuples, in order
of appearance in the diff. Each pair is returned ONCE, and the deleted file
is never reviewed, the added file is never reviewed.

If any check fails, falls back to no exclusion (grug#772).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


def _parse_diff_headers(diff_text: str) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """Extract file change headers from a unified diff.

    Returns:
        - List of (path, status, similarity) tuples for each file block
        - Dict mapping blob IDs to paths for files that were added (by diff)
    """
    file_headers: list[tuple[str, str, str | None]] = []
    added_blobs: dict[str, str] = {}

    in_block = False
    current_path = ""
    current_status = ""  # added, removed, renamed, modified
    current_similarity = None
    in_body = False

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if in_block:
                file_headers.append((current_path, current_status, current_similarity))
            current_path = ""
            current_status = ""
            current_similarity = None
            in_block = True
            in_body = False

        if in_block and not in_body:
            if line.startswith("+++ "):
                # Extract path from +++ b/path or +++ /dev/null
                m = line[4:].strip()
                if m != "/dev/null":
                    # Strip the leading 'b/' if present
                    current_path = m[2:] if m.startswith("b/") else m
            elif line.startswith("--- "):
                # Extract path from --- a/path (for later matching)
                m = line[5:].strip()
                if m != "/dev/null":
                    # Strip the leading 'a/' if present
                    current_path = m[2:] if m.startswith("a/") else m
            elif line.startswith("similarity index"):
                m = line.strip().split()[2]
                current_similarity = int(m[:-1])  # Remove the % sign
            elif line.startswith("new file mode"):
                current_status = "added"
            elif line.startswith("deleted file mode"):
                current_status = "removed"
            elif line.startswith("rename from"):
                current_status = "renamed"
            elif line.startswith("rename to"):
                pass  # Already captured status above
            elif line.startswith("index"):
                # Extract blob ID for added files
                # Format: index <old>..<new> 
                # Note: path might not be set yet if +++ line comes after index
                parts = line.split()
                if len(parts) >= 2:
                    # The blob ID is after the first ..
                    blob_part = parts[1]
                    new_blob = blob_part.split('..')[1]
                    if current_status == "added":
                        # Use current_path if set, otherwise try to extract from context
                        if current_path and current_path not in added_blobs:
                            added_blobs[current_path] = new_blob
                        elif not current_path:
                            # Path not set yet, try to extract from diff context
                            # This happens when +++ line comes after index line
                            # We'll use empty string for now and let caller handle it
                            pass

        if line.startswith("@@"):
            in_body = True

    if in_block:
        file_headers.append((current_path, current_status, current_similarity))

    return file_headers, added_blobs


def _extract_added_file_content(hunks: tuple) -> dict[str, str]:
    """Extract full content of all added files from hunks.

    Returns a dict mapping paths to their full content (str).
    """
    content: dict[str, str] = {}

    for hunk in hunks:
        # Check if this is a whole-new-file hunk (-0,0)
        lines = hunk.body.splitlines()
        if not lines:
            continue

        header = lines[0]
        if not header.startswith("@@ -0,0 +"):
            continue  # Not a whole-new-file

        # Extract the path from the header if present
        # Format: @@ -0,0 +1,N @@ path
        import re
        m = re.search(r"\+([1-9][0-9]*)\s*@@\s*(.+)", header)
        if not m:
            continue

        new_line = m.group(1)
        path_part = m.group(2).strip()
        path = path_part if not path_part.startswith("+") else ""

        # Collect all content lines
        content_lines = []
        for raw in lines[1:]:
            if raw.startswith("\\"):
                continue  # Skip no-newline marker
            if raw.startswith("+"):
                content_lines.append(raw[1:])

        if path:
            content[path] = "\n".join(content_lines)

    return content


def split_move_hunks(
    hunks: tuple,
    diff_text: str,
) -> tuple[tuple, tuple[tuple[str, str], ...]]:
    """Detect and exclude moves (deletion + identical addition) from review.

    Returns:
        - Kept hunks (those not part of a move)
        - Moves as (deleted_path, added_path) tuples in diff order

    A move is identified when:
    1. A file is deleted (no @@ block)
    2. A file with identical content is added later in the same diff
    3. Both are whole files (not partial edits)

    If any check fails, falls back to no exclusion (grug#772).
    """
    try:
        hunks_result, moves_result = _split_move_hunks_impl(hunks, diff_text)
        return hunks_result, moves_result
    except Exception:
        # If anything fails, don't exclude anything
        return hunks, ()


def _split_move_hunks_impl(
    hunks: tuple,
    diff_text: str,
) -> tuple[tuple, tuple[tuple[str, str], ...]]:
    """Internal implementation of move detection.

    Returns:
        - Kept hunks (those not part of a move)
        - Moves as (deleted_path, added_path) tuples in diff order

    If any check fails, raises an exception.
    """
    # Parse diff headers to find deletions and added blobs
    try:
        file_headers, added_blobs = _parse_diff_headers(diff_text)
    except Exception:
        raise

    # Extract content of added files from hunks
    try:
        added_content = _extract_added_file_content(hunks)
    except Exception:
        raise

    # Build a map of blob IDs to added file paths
    blob_to_path = {blob: path for path, blob in added_blobs.items()}

    # Find moves: a deleted file plus an added file with the same content
    moves: list[tuple[str, str]] = []
    seen_deletions: set[str] = set()
    seen_additions: set[str] = set()

    for path, status, _ in file_headers:
        if status == "removed":
            seen_deletions.add(path)
        elif status == "added" and path in added_content:
            # Check if this added file has content matching a deleted file
            content = added_content[path]

            # Find matching deleted path
            for deleted in seen_deletions:
                if deleted not in seen_additions:  # Already matched
                    if content == deleted:
                        moves.append((deleted, path))
                        seen_additions.add(path)
                        break

    if not moves:
        return (hunks,)  # Return tuple with just hunks when no moves found

    return hunks, tuple(moves)
    # Parse diff headers to find deletions and added blobs
    try:
        file_headers, added_blobs = _parse_diff_headers(diff_text)
    except Exception:
        # If header parsing fails, don't exclude anything
        return hunks, ()

    # Extract content of added files from hunks
    try:
        added_content = _extract_added_file_content(hunks)
    except Exception:
        # If content extraction fails, don't exclude anything
        return hunks, ()

    # Build a map of blob IDs to added file paths
    blob_to_path = {blob: path for path, blob in added_blobs.items()}

    # Find moves: a deleted file plus an added file with the same content
    moves: list[tuple[str, str]] = []
    seen_deletions: set[str] = set()
    seen_additions: set[str] = set()

    for path, status, _ in file_headers:
        if status == "removed":
            seen_deletions.add(path)
        elif status == "added" and path in added_content:
            # Check if this added file has content matching a deleted file
            content = added_content[path]

            # Find matching deleted path
            for deleted in seen_deletions:
                if deleted not in seen_additions:  # Already matched
                    if content == deleted:
                        moves.append((deleted, path))
                        seen_additions.add(path)
                        break

    return hunks, tuple(moves)
