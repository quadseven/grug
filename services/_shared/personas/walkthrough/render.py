"""Walkthrough comment assembly (#554) - pure Markdown rendering.

One upsert-by-marker comment (same PATCH-else-POST discipline as Chief's
ticket-compliance): AI intent summary, changed-files table, deterministic
mermaid diagram, and a review-effort chip. Assembly is pure so it can be
tested without any network/LLM call; `dispatch.py` supplies the fetched
data and the (possibly degraded) summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from personas.walkthrough.effort import ReviewEffort, effort_label

MARKER = "<!-- grug-teller:walkthrough -->"

# Bound the table + summary text so a sweeping PR can't blow the comment
# body past GitHub's cap; excess files are counted, not silently dropped.
_MAX_TABLE_ROWS = 60
_MAX_SUMMARY_CHARS = 2000
_MAX_FILE_SUMMARY_CHARS = 160

_MENTION_RE = re.compile(r"@(?=\w)")


def _neutralize_mentions(text: str) -> str:
    """Break `@user` into `@<ZWSP>user` before it reaches GitHub's markdown
    renderer. The comment posts with the app's OWN installation-token
    authority, so a live mention in model-authored (or repo-controlled
    path) text would notify a real GitHub user as if Teller/Grug itself
    pinged them - a prompt-injected diff can influence this text (#554
    peer review round 4, codex). Visually identical to a reader; GitHub's
    mention parser requires an unbroken `@word` token."""
    return _MENTION_RE.sub("@\u200b", text)


def _escape_table_cell(text: str) -> str:
    """For a PLAIN-TEXT table cell (the Summary column): a bare `|` or
    newline lets model-authored text break the row into extra columns or
    fake extra rows (round 4, codex). GFM honors a backslash-escaped `\\|`
    as non-delimiting in plain text, so this is a true fix here."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _escape_code_span_cell(text: str) -> str:
    """For a BACKTICK-WRAPPED table cell (the File column, `` `{path}` ``):
    backslash escapes do NOT apply inside a code span (CommonMark) - a
    literal `\\|` would render as two visible characters, not a hidden
    pipe. GFM's Tables extension already treats `|` inside a code span as
    non-delimiting, so pipes need no escaping here. The real risk is a
    raw backtick in the path breaking OUT of the span early (a POSIX
    filename can legally contain almost any byte, including a backtick or
    a newline) - strip both rather than build a dynamic fence length for
    a cosmetic table cell."""
    return text.replace("`", "'").replace("\n", " ").replace("\r", " ")


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
    _MAX_TABLE_ROWS with a visible '+N more' line - never a silent cut.
    `path` and `summary` are escaped for both table-row integrity and
    mention-spoofing before splicing (round 4, codex) - `path` is repo-
    controlled (a crafted filename) and `summary` is model-authored."""
    if not files:
        return ""
    rows = ["| File | Summary | Changes |", "|---|---|---|"]
    for f in files[:_MAX_TABLE_ROWS]:
        path = _escape_code_span_cell(_neutralize_mentions(f.path))
        summary = _escape_table_cell(_neutralize_mentions(
            (f.summary or "")[:_MAX_FILE_SUMMARY_CHARS]
        )) or "-"
        rows.append(f"| `{path}` | {summary} | +{f.additions}/-{f.deletions} |")
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
    files_truncated: bool = False,
) -> str:
    """Assemble the full comment body. `degraded` marks a summary that
    fell back to the deterministic form (LLM call failed/timed out) -
    the comment says so rather than presenting a fallback as the real
    thing. `files_truncated` marks a PR whose changed-file count exceeded
    our fetch cap (GitHub's own /files cap is far higher) - every number
    below is then a floor, not an exact count, and the comment says so.
    `summary` is model-authored: mentions are neutralized before this
    prose posts under the app's own installation-token identity (round 4,
    codex) - a prompt-injected diff could otherwise get a real GitHub
    user pinged as if Teller itself did it."""
    parts = [MARKER, "## Grug Teller walk the PR before the tribe judge it", ""]
    parts.append(_neutralize_mentions(summary[:_MAX_SUMMARY_CHARS]))
    if degraded:
        parts.append(
            "\n_(Teller's voice was quiet this pass - a deterministic "
            "summary stands in; the table and diagram below are unaffected.)_"
        )
    if files_truncated:
        parts.append(
            f"\n_(This hunt sprawl wide - Teller counted only the first "
            f"{len(files)} file(s); the true count runs higher.)_"
        )
    parts.append(f"\n**Effort to review:** {effort_label(effort)}")
    table = changed_files_table(files)
    if table:
        parts.append(f"\n### Changed files\n\n{table}")
    if diagram:
        parts.append(f"\n### Shape of the change\n\n```mermaid\n{diagram}\n```")
    parts.append(f"\n_Last walked at commit `{head_sha[:12]}`._")
    return "\n".join(parts)
