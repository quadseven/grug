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
_HEADING_RE = re.compile(r"^(#{1,6})(\s)", re.MULTILINE)


def _neutralize_mentions(text: str) -> str:
    """Break `@user` into `@<ZWSP>user` before it reaches GitHub's markdown
    renderer. The comment posts with the app's OWN installation-token
    authority, so a live mention in model-authored (or repo-controlled
    path) text would notify a real GitHub user as if Teller/Grug itself
    pinged them - a prompt-injected diff can influence this text (#554
    peer review round 4, codex). Visually identical to a reader; GitHub's
    mention parser requires an unbroken `@word` token."""
    return _MENTION_RE.sub("@\u200b", text)


def _neutralize_headings(text: str) -> str:
    """Break a line-leading `#` run (an ATX heading) with a zero-width
    space so model-authored summary prose can never impersonate a NEW
    section of Teller's own comment (round 4, codex) - e.g. a prompt-
    injected diff getting the model to emit a line that reads as a fake
    "## Merge immediately" heading, visually indistinguishable from a
    real Teller section. Deliberately narrow: only leading `#` is
    neutralized, not all Markdown, so legitimate prose (backticks,
    emphasis, inline code) still renders - matching how CodeRabbit/Qodo's
    own AI-authored walkthroughs use normal Markdown formatting."""
    return _HEADING_RE.sub(lambda m: "\u200b" + m.group(1) + m.group(2), text)


def _escape_html(text: str) -> str:
    """Neutralize HTML specials in model-authored prose.

    File blurbs sit inside <details> blocks; a raw </details> (or any
    tag) would close the collapsible early and corrupt the comment.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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
        summary = _escape_html(_escape_table_cell(_neutralize_mentions(
            (f.summary or "")[:_MAX_FILE_SUMMARY_CHARS]
        ))) or "-"
        rows.append(f"| `{path}` | {summary} | +{f.additions}/-{f.deletions} |")
    table = "\n".join(rows)
    cut = len(files) - _MAX_TABLE_ROWS
    if cut > 0:
        table += f"\n\n_(+{cut} more file(s) not shown)_"
    return table


def _details(summary: str, body: str) -> str:
    """Collapsible block. Keeps the default PR view short; expand for detail."""
    return (
        f"<details>\n"
        f"<summary>{summary}</summary>\n\n"
        f"{body}\n\n"
        f"</details>"
    )


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
    """Assemble a short walkthrough comment (default-collapsed detail).

    Layout goal: one screen of useful signal when collapsed, like a review
    summary card - not a wall of tables and mermaid.

      ## Walkthrough
      {short summary}
      Review effort: ...
      <details> Changed files (N) </details>
      <details> Shape of the change </details>
      _Last walked at ..._

    `degraded` marks a summary that fell back to the deterministic form
    (LLM call failed/timed out) - the comment says so rather than
    presenting a fallback as the real thing. `files_truncated` marks a PR
    whose changed-file count exceeded our fetch cap - every number below
    is then a floor, not an exact count. `summary` is model-authored:
    mentions and line-leading `#` headings are neutralized before this
    prose posts under the app's own installation-token identity.
    """
    parts = [MARKER, "## Walkthrough", ""]
    parts.append(
        _neutralize_headings(_neutralize_mentions(summary[:_MAX_SUMMARY_CHARS]))
    )
    notes: list[str] = []
    if degraded:
        notes.append(
            "Summary fell back to a deterministic sketch; file list and "
            "diagram (if any) are still live."
        )
    if files_truncated:
        notes.append(
            f"File list is partial: first {len(files)} files only; "
            "the PR has more."
        )
    if notes:
        parts.append("")
        parts.extend(f"- {n}" for n in notes)
    parts.append("")
    parts.append(f"Review effort: {effort_label(effort)}")

    table = changed_files_table(files)
    if table:
        n = len(files)
        label = f"Changed files ({n})"
        if files_truncated:
            label = f"Changed files (first {n})"
        parts.append("")
        parts.append(_details(label, table))

    if diagram:
        parts.append("")
        parts.append(
            _details("Shape of the change", f"```mermaid\n{diagram}\n```")
        )
    elif files:
        # Stated reason when files exist but diagram could not be drawn
        # (too many top-level dirs, balance check failed, and so on).
        parts.append("")
        parts.append(
            _details(
                "Shape of the change",
                "No diagram this pass - the change was too large or "
                "complex to visualize cleanly.",
            )
        )

    parts.append("")
    parts.append(f"_Last walked at `{head_sha[:12]}`._")
    return "\n".join(parts)
