"""Walkthrough comment assembly (#554) - pure Markdown rendering.

One upsert-by-marker comment (same PATCH-else-POST discipline as Chief's
ticket-compliance): AI intent summary, changed-files table, deterministic
mermaid diagram, and a review-effort chip. Assembly is pure so it can be
tested without any network/LLM call; `dispatch.py` supplies the fetched
data and the (possibly degraded) summary.
"""

from __future__ import annotations

from dataclasses import dataclass

from personas.walkthrough.effort import ReviewEffort, effort_label

MARKER = "<!-- grug-teller:walkthrough -->"

# Bound the table + summary text so a sweeping PR can't blow the comment
# body past GitHub's cap; excess files are counted, not silently dropped.
_MAX_TABLE_ROWS = 60
_MAX_SUMMARY_CHARS = 2000
_MAX_FILE_SUMMARY_CHARS = 160


@dataclass(frozen=True, slots=True)
class FileStat:
    """One changed file's diff-stat + (optional, model-supplied) blurb.
    `summary=None` (not "") for absence - the one convention this whole
    PR uses everywhere else (WalkthroughSummary.effort, build_diagram)."""

    path: str
    additions: int
    deletions: int
    summary: str | None = None


def changed_files_table(files: list[FileStat]) -> str:
    """The changed-files table: path, one-line summary, +X/-Y. Bounded to
    _MAX_TABLE_ROWS with a visible '+N more' line - never a silent cut."""
    if not files:
        return ""
    rows = ["| File | Summary | Changes |", "|---|---|---|"]
    for f in files[:_MAX_TABLE_ROWS]:
        summary = (f.summary or "")[:_MAX_FILE_SUMMARY_CHARS] or "-"
        rows.append(f"| `{f.path}` | {summary} | +{f.additions}/-{f.deletions} |")
    table = "\n".join(rows)
    cut = len(files) - _MAX_TABLE_ROWS
    if cut > 0:
        table += f"\n\n_(+{cut} more file(s) not shown)_"
    return table


def walkthrough_body(
    *,
    summary: str,
    files: list[FileStat],
    diagram: str | None,
    effort: ReviewEffort,
    head_sha: str,
    degraded: bool,
) -> str:
    """Assemble the full comment body. `degraded` marks a summary that
    fell back to the deterministic form (LLM call failed/timed out) - the
    comment says so rather than presenting a fallback as the real thing."""
    parts = [MARKER, "## Grug Teller walk the PR before the tribe judge it", ""]
    parts.append(summary[:_MAX_SUMMARY_CHARS])
    if degraded:
        parts.append(
            "\n_(Teller's voice was quiet this pass - a deterministic "
            "summary stands in; the table and diagram below are unaffected.)_"
        )
    parts.append(f"\n**Effort to review:** {effort_label(effort)}")
    table = changed_files_table(files)
    if table:
        parts.append(f"\n### Changed files\n\n{table}")
    if diagram:
        parts.append(f"\n### Shape of the change\n\n```mermaid\n{diagram}\n```")
    parts.append(f"\n_Last walked at commit `{head_sha[:12]}`._")
    return "\n".join(parts)
