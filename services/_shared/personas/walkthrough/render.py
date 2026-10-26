"""Walkthrough rendering primitives for the Hunt Board (#554).

Pure Markdown building blocks (changed-files table + cell sanitization) used
to assemble Teller's walkthrough text. The board's Walkthrough section and
the Teller check-run summary are assembled from these by the walkthrough
dispatch; no advisory comment is posted anymore.
"""

from __future__ import annotations

from dataclasses import dataclass

from markdown_safety import neutralize_mentions as _neutralize_mentions

# Bound the table text so a sweeping PR can't blow the board section up;
# excess files are counted, not silently dropped.
_MAX_TABLE_ROWS = 60
_MAX_FILE_SUMMARY_CHARS = 160


@dataclass(frozen=True, slots=True)
class FileStat:
    """One changed file's diff-stat + (optional, model-supplied) blurb."""

    path: str
    additions: int
    deletions: int
    summary: str | None = None


def _escape_html(text: str) -> str:
    """Neutralize HTML specials in model-authored prose.

    File blurbs sit inside table cells; a raw tag would corrupt rendering.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_table_cell(text: str) -> str:
    """For a PLAIN-TEXT table cell (the Summary column): a bare `|` or
    newline lets model-authored text break the row into extra columns or
    fake extra rows. GFM honors a backslash-escaped `\\|` as non-delimiting
    in plain text."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _escape_code_span_cell(text: str) -> str:
    """For a BACKTICK-WRAPPED table cell (the File column, `` `{path}` ``):
    a raw backtick in the path would close the span early. Strip both
    backticks and newlines rather than build a dynamic fence length."""
    return text.replace("`", "'").replace("\n", " ").replace("\r", " ")


def changed_files_table(files: list[FileStat]) -> str:
    """The changed-files table: path, one-line summary, +X/-Y. Bounded to
    _MAX_TABLE_ROWS with a visible '+N more' line - never a silent cut."""
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
