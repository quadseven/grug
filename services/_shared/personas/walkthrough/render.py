"""Walkthrough assembly (#554, T1432) - pure Markdown rendering.

One body for two surfaces, neither a comment of Teller's own (every new
comment is an email): the Hunt Board's Walkthrough section and the Teller
check-run summary. It holds the AI intent summary (absent when the model
was quiet), a review-effort line and the changed-files list; no mermaid
diagram. Assembly is pure so it can be tested without any network/LLM
call; `dispatch.py` supplies the fetched data and the summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_safety import neutralize_mentions as _neutralize_mentions

from personas.walkthrough.effort import ReviewEffort, effort_label

# Bound the table + summary text so a sweeping PR can't blow the check-run summary
# body past GitHub's cap; excess files are counted, not silently dropped.
_MAX_TABLE_ROWS = 60
_MAX_SUMMARY_CHARS = 2000
_MAX_FILE_SUMMARY_CHARS = 160

_HEADING_RE = re.compile(r"^(#{1,6})(\s)", re.MULTILINE)


def _neutralize_headings(text: str) -> str:
    """Break a line-leading `#` run (an ATX heading) with a zero-width
    space so model-authored summary prose can never impersonate a NEW
    section of Teller's own comment (round 4, codex) - e.g. a prompt-
    injected diff getting the model to emit a line that reads as a fake
    "## Merge immediately" heading, visually indistinguishable from a
    real Teller section. Deliberately narrow: only leading `#` is
    neutralized, not all Markdown, so legitimate prose (backticks,
    emphasis, inline code) still renders - matching how FLINT/LORE's
    own AI-authored walkthroughs use normal Markdown formatting."""
    return _HEADING_RE.sub(lambda m: "\u200b" + m.group(1) + m.group(2), text)


def _escape_html(text: str) -> str:
    """Neutralize HTML specials in model-authored prose.

    File blurbs sit inside <details> blocks; a raw </details> (or any
    tag) would close the collapsible early and corrupt the comment.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def changed_files_list(files: list[FileStat]) -> str:
    """The changed-files list (#1386): path, changes (+X/-Y), summary. Bounded
    to _MAX_TABLE_ROWS with a visible '+N more' line - never a silent cut.
    `path` and `summary` are sanitized before splicing."""
    if not files:
        return ""
    lines: list[str] = []
    for f in files[:_MAX_TABLE_ROWS]:
        path = _escape_code_span_cell(_neutralize_mentions(f.path))
        raw_summary = _neutralize_mentions(
            (f.summary or "")[:_MAX_FILE_SUMMARY_CHARS]
        ).replace("\n", " ").replace("\r", " ").strip()
        summary = _escape_html(raw_summary)
        changes = f"+{f.additions}/-{f.deletions}"
        if summary:
            lines.append(f"- `{path}` ({changes}) - {summary}")
        else:
            lines.append(f"- `{path}` ({changes})")
    out = "\n".join(lines)
    cut = len(files) - _MAX_TABLE_ROWS
    if cut > 0:
        out += f"\n\n_(+{cut} more file(s) not shown)_"
    return out


# Backward-compatible alias
changed_files_table = changed_files_list


def _details(summary: str, body: str) -> str:
    """Collapsible block. Keeps the default PR view short; expand for detail."""
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def walkthrough_body(
    *,
    summary: str | None,
    files: list[FileStat],
    diagram: str | None = None,
    effort: ReviewEffort,
    head_sha: str,
    files_truncated: bool = False,
    related: list | None = None,
) -> str:
    """Assemble the walkthrough (default-collapsed detail).

    Layout goal: one screen of useful signal when collapsed, like a review
    summary card - not a wall of tables and mermaid.

      {short summary, when the model gave one}
      Review effort: ...
      **Possibly related hunts**   (only when `related` is non-empty)
      <details> Changed files (N) </details>
      _Last walked at ..._

    `summary=None` is the deterministic fallback: no prose paragraph at all,
    the file list below carries the walkthrough. `files_truncated` marks a
    PR whose changed-file count exceeded our fetch cap - every number below
    is then a floor, not an exact count. `summary` is model-authored:
    mentions and line-leading `#` headings are neutralized before this
    prose posts under the app's own installation-token identity. `diagram`
    is accepted and never rendered (T1386: a mermaid fence reads as raw
    source in phone email clients, and the graph has had no edges, T1432).
    `related` is for the check-run summary; the Hunt Board renders its own
    Related context list, so its body leaves it out.
    """
    parts: list[str] = []
    if summary:
        parts.append(
            _neutralize_headings(_neutralize_mentions(summary[:_MAX_SUMMARY_CHARS]))
        )
        parts.append("")
    if files_truncated:
        parts.append(
            f"- File list is partial: first {len(files)} files only; the PR has more."
        )
        parts.append("")
    parts.append(f"Review effort: {effort_label(effort)}")

    if related:
        # #675: possibly-related prior hunts. Omitted entirely when nothing
        # cleared the relevance bar (honest empty - no filler).
        parts.append("")
        parts.append("**Possibly related hunts**")
        parts.append("")
        for r in related[:3]:
            title = _neutralize_mentions(r.title)
            reason = _neutralize_mentions(r.reason)
            parts.append(f"- #{r.number} {title} — {reason}")

    files_list = changed_files_list(files)
    if files_list:
        n = len(files)
        label = f"Changed files ({n})"
        if files_truncated:
            label = f"Changed files (first {n})"
        parts.append("")
        parts.append(_details(label, files_list))

    parts.append("")
    parts.append(f"_Last walked at `{head_sha[:12]}`._")
    return "\n".join(parts)
