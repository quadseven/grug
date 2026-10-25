"""Walkthrough comment assembly (#554) - pure Markdown rendering.

One upsert-by-marker comment (same PATCH-else-POST discipline as Chief's
ticket-compliance): AI intent summary, changed-files table, deterministic
mermaid diagram, and a review-effort chip. Assembly is pure so it can be
tested without any network/LLM call; `dispatch.py` supplies the fetched
data and the (possibly degraded) summary.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

from markdown_safety import neutralize_mentions as _neutralize_mentions

from personas.walkthrough.effort import ReviewEffort, effort_label

MARKER = "<!-- grug-teller:walkthrough -->"

# Teller portrait (repo-hosted on grug.lol) - matches Chief's pattern
TELLER_PORTRAIT = "https://grug.lol/assets/grug_smile.png"
TELLER_PORTRAIT_IMG = (
    f'<img src="{TELLER_PORTRAIT}" width="46" align="left" alt="Grug Teller" />'
)

# Badge for the "View full review" button (shields.io style)
REVIEW_BADGE = (
    "https://img.shields.io/badge/View_full_review-181613"
    "?style=for-the-badge&labelColor=181613"
)

# Bound the table + summary text so a sweeping PR can't blow the comment
# body past GitHub's cap; excess files are counted, not silently dropped.
_MAX_TABLE_ROWS = 60
_MAX_SUMMARY_CHARS = 2000
_MAX_FILE_SUMMARY_CHARS = 160

# Lifecycle (#695): one comment is one evolving document. A status line
# (carrying STATUS_TAG) says what the run is doing right now; the final body
# carries none. RECORD_PREFIX opens a hidden record of the run, so the next
# run can show what changed since.
STATUS_TAG = "<!-- grug-teller:status -->"
RECORD_PREFIX = "<!-- grug-teller:record:"
_RECORD_RE = re.compile(r"<!-- grug-teller:record:([A-Za-z0-9_=-]+) -->")
_MAX_RECORD_FILES = 500
_MAX_RECORD_SUMMARY = 600

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
        summary = (
            _escape_html(
                _escape_table_cell(
                    _neutralize_mentions((f.summary or "")[:_MAX_FILE_SUMMARY_CHARS])
                )
            )
            or "-"
        )
        rows.append(f"| `{path}` | {summary} | +{f.additions}/-{f.deletions} |")
    table = "\n".join(rows)
    cut = len(files) - _MAX_TABLE_ROWS
    if cut > 0:
        table += f"\n\n_(+{cut} more file(s) not shown)_"
    return table


@dataclass(frozen=True)
class PreviousRun:
    """What the last walkthrough said: the head it walked, its summary, and
    the files it listed. Read back from the hidden record in its comment."""

    head_sha: str
    summary: str
    paths: tuple[str, ...]


def status_line(text: str) -> str:
    return f"> {text} {STATUS_TAG}"


def with_status(existing: str | None, text: str) -> str:
    """Return `existing` with its status line replaced by `text`, or a fresh
    marker + status body when there is no comment yet. Everything else in an
    existing comment (the last results, the record) stays, so a re-run shows
    'working' on top of the last walkthrough instead of erasing it."""
    line = status_line(text)
    if not existing:
        return f"{MARKER}\n\n{line}"
    kept = [ln for ln in existing.split("\n") if STATUS_TAG not in ln]
    if kept and kept[0].strip() == MARKER:
        return "\n".join([kept[0], "", line, *kept[1:]])
    return "\n".join([MARKER, "", line, *kept])


def _encode_record(head_sha: str, summary: str, paths: list[str]) -> str:
    raw = json.dumps(
        {
            "sha": head_sha[:40],
            "summary": summary[:_MAX_RECORD_SUMMARY],
            "files": paths[:_MAX_RECORD_FILES],
        },
        separators=(",", ":"),
    )
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return f"{RECORD_PREFIX}{token} -->"


def previous_run(body: str | None) -> PreviousRun | None:
    """Read the hidden record out of a walkthrough comment body. None when
    the comment has no record (first run, or a placeholder/failure only) or
    the record does not parse - an unreadable record just means no history."""
    match = _RECORD_RE.search(body or "")
    if not match:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(match.group(1)))
        files = [str(f) for f in data["files"]][:_MAX_RECORD_FILES]
        return PreviousRun(
            head_sha=str(data["sha"]),
            summary=str(data["summary"]),
            paths=tuple(files),
        )
    except (binascii.Error, ValueError, KeyError, TypeError):
        return None


def _previous_section(previous: PreviousRun, current: list[FileStat]) -> str:
    """Collapsed record of the last run. A file it listed that the change no
    longer touches is marked resolved, never silently dropped."""
    now = {f.path for f in current}
    shown = previous.paths[:_MAX_TABLE_ROWS]
    lines = [
        _escape_html(_neutralize_headings(_neutralize_mentions(previous.summary))),
        "",
        "Files in that walkthrough:",
        "",
    ]
    for path in shown:
        mark = "still in the change" if path in now else "resolved (no longer in the change)"
        lines.append(f"- `{_escape_code_span_cell(path)}` - {mark}")
    cut = len(previous.paths) - len(shown)
    if cut > 0:
        lines.append(f"- (+{cut} more not shown)")
    return _details(f"Previous walkthrough (`{previous.head_sha[:12]}`)", "\n".join(lines))


def _details(summary: str, body: str) -> str:
    """Collapsible block. Keeps the default PR view short; expand for detail."""
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def walkthrough_body(
    *,
    summary: str,
    files: list[FileStat],
    diagram: str | None,
    effort: ReviewEffort,
    head_sha: str,
    degraded: bool,
    files_truncated: bool = False,
    related: list | None = None,
    pr_url: str | None = None,
    previous: PreviousRun | None = None,
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
    `pr_url` is where the full review is (the PR's Checks tab); when
    provided it's rendered as a badge-image button after the header.
    `previous` is the last run read from this same comment (#695): it is
    kept as a collapsed section with its files marked resolved or still
    present, and a hidden record of THIS run is appended for the next one.
    """
    parts = [MARKER, TELLER_PORTRAIT_IMG, "", "## Walkthrough", ""]
    parts.append(
        _neutralize_headings(_neutralize_mentions(summary[:_MAX_SUMMARY_CHARS]))
    )
    if pr_url:
        parts.append("")
        parts.append(
            f'<a href="{pr_url}"><img src="{REVIEW_BADGE}" alt="View full review" /></a>'
        )
    notes: list[str] = []
    if degraded:
        notes.append(
            "Summary fell back to a deterministic sketch; file list and "
            "diagram (if any) are still live."
        )
    if files_truncated:
        notes.append(
            f"File list is partial: first {len(files)} files only; the PR has more."
        )
    if notes:
        parts.append("")
        parts.extend(f"- {n}" for n in notes)
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
        parts.append(_details("Shape of the change", f"```mermaid\n{diagram}\n```"))
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

    if previous is not None:
        parts.append("")
        parts.append(_previous_section(previous, files))

    parts.append("")
    parts.append(f"_Last walked at `{head_sha[:12]}`._")
    parts.append(_encode_record(head_sha, summary, [f.path for f in files]))
    return "\n".join(parts)
