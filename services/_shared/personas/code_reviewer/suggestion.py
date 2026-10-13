"""Checked one-line fixes for committable suggestion blocks (#1149, part of #1143).

A GitHub ```suggestion block replaces the single anchored line in one click.
That makes it the fastest fix a lane agent or an author can take, and also
the most dangerous: nothing used to check that the offered line still parses
once applied, so a one-click commit could break the file.

Measured 2026-10-12: 7 of 85 bot threads (8%) carried a block. The model
fills the optional `suggestion` field rarely, while real findings still spell
the exact one-line fix out in prose (a null-deref finding gave
`option = next((o for o in options if ...), None)` with no block).

`checked_suggestion` is the single gate every block passes through:

  (a) the finding's own single-line suggestion, kept only if it changes the
      anchored line and, for Python, the file still parses with it applied;
  (b) when there is none, the message's ONE inline code span, promoted only
      if it starts with the anchored line's assignment target or leading
      statement keyword, and it passes the same check;
  (c) ruff findings carry ruff's own fix as their suggestion when that fix
      is marked safe and confined to the anchored line (`ruff_fix_line`,
      used by lint.py), then pass the same check as (a).

Pure: no I/O, no logging. A None answer means "render the fix as prose",
which is what every finding did before this module existed.
"""
from __future__ import annotations

import ast
import io
import keyword
import re
import tokenize
from typing import Any

from personas.code_reviewer.persona import Finding

_PYTHON_SUFFIXES = (".py", ".pyi")

# Single-backtick inline code spans. A double-backtick span or a fenced block
# is not a one-line replacement the message is offering, so neither counts.
_CODE_SPAN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")

# `name = ...`, `obj.attr: T = ...`, `x += ...`. Captures the target only.
_ASSIGN_TARGET = re.compile(
    r"^([A-Za-z_][\w.]*(?:\[[^\]]*\])?)\s*(?::[^=]+)?(?:[-+*/%&|^@]|//|>>|<<|\*\*)?=(?!=)"
)
_LEADING_WORD = re.compile(r"^([A-Za-z_]\w*)")


def checked_suggestion(
    finding: Finding, anchored_line: int, file_text: str,
) -> str | None:
    """The exact replacement for `anchored_line` (1-based) in `file_text`,
    or None when no fix survives the checks. See the module docstring."""
    lines = file_text.splitlines()
    if not 1 <= anchored_line <= len(lines):
        return None
    original = lines[anchored_line - 1]
    # A filled field, even blank or fenced, is the model's own answer; the
    # prose span is read only when the field is None. A span that merely
    # quotes the anchored line is dropped by the unchanged-line check below.
    if finding.suggestion is not None:
        candidate = _single_line(finding.suggestion)
    else:
        candidate = _promoted_from_prose(finding.message, original)
    if candidate is None:
        return None
    candidate = _with_indent_of(candidate, original)
    if candidate.rstrip() == original.rstrip():
        return None
    if finding.file.endswith(_PYTHON_SUFFIXES) and not _still_parses(
        lines, anchored_line, candidate,
    ):
        return None
    return candidate


def ruff_fix_line(fix: Any, row: int, line_text: str) -> str | None:
    """Apply ruff's reported fix to the one line it was reported on.

    None unless the fix is marked `safe` and every edit starts and ends on
    `row` without inserting a newline: an unsafe fix can change behaviour,
    and an edit that spans lines cannot be a one-line suggestion. Tolerant of
    shape drift in ruff's JSON, like lint._map_results."""
    if not isinstance(fix, dict) or fix.get("applicability") != "safe":
        return None
    edits = fix.get("edits")
    if not isinstance(edits, list) or not edits:
        return None
    spans: list[tuple[int, int, str]] = []
    for edit in edits:
        span = _edit_span(edit, row)
        if span is None:
            return None
        spans.append(span)
    spans.sort(reverse=True)
    out = line_text
    floor = len(line_text)
    for start, end, content in spans:
        # Overlapping edits have no single well-defined result.
        if not 0 <= start <= end <= floor:
            return None
        out = out[:start] + content + out[end:]
        floor = start
    return out


def _edit_span(edit: Any, row: int) -> tuple[int, int, str] | None:
    """(start, end, content) as 0-based columns, or None when the edit is
    malformed or reaches beyond `row`."""
    if not isinstance(edit, dict):
        return None
    start, end = edit.get("location"), edit.get("end_location")
    content = edit.get("content")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    if not isinstance(content, str) or "\n" in content or "\r" in content:
        return None
    if start.get("row") != row or end.get("row") != row:
        return None
    start_col, end_col = start.get("column"), end.get("column")
    if not isinstance(start_col, int) or not isinstance(end_col, int):
        return None
    # ruff columns are 1-based.
    return start_col - 1, end_col - 1, content


def _single_line(suggestion: str) -> str | None:
    """The suggestion as one line, or None for a multi-line, fence-bearing
    or blank one. Same rule as dispatch._inline_comment_body: strip wrapping
    newlines only, because leading indentation is part of the line."""
    stripped = suggestion.strip("\n\r")
    if not stripped.strip() or "\n" in stripped or "\r" in stripped:
        return None
    if "```" in stripped:
        return None
    return stripped


def _promoted_from_prose(message: str, original: str) -> str | None:
    """The message's single inline code span, when it reads as a rewrite of
    `original`: it opens with the same assignment target or the same leading
    statement keyword. Two or more spans is ambiguous, so None."""
    spans = _CODE_SPAN.findall(message)
    if len(spans) != 1:
        return None
    span = spans[0].strip()
    anchor = _line_anchor(original.strip())
    if not anchor or not span.startswith(anchor):
        return None
    rest = span[len(anchor):]
    # `option` must not match `optional = ...`.
    if rest and (rest[0].isalnum() or rest[0] == "_"):
        return None
    return span


def _line_anchor(stripped_line: str) -> str | None:
    """Assignment target, else the leading keyword, of one source line."""
    target = _ASSIGN_TARGET.match(stripped_line)
    if target:
        return target.group(1)
    word = _LEADING_WORD.match(stripped_line)
    if word and keyword.iskeyword(word.group(1)):
        return word.group(1)
    return None


def _with_indent_of(candidate: str, original: str) -> str:
    """Give an unindented candidate the anchored line's indentation. Models
    and prose drop it; GitHub commits the block verbatim as the whole line."""
    if candidate[:1] in (" ", "\t"):
        return candidate
    indent = original[: len(original) - len(original.lstrip())]
    return indent + candidate


def _still_parses(lines: list[str], anchored_line: int, candidate: str) -> bool:
    """True when the file parses both as it stands and with the line
    replaced. A file that does not parse today gives no evidence either way,
    so it offers no block."""
    if lines[anchored_line - 1].rstrip().endswith("\\"):
        # The line continues into the next one. Swapping only this line can
        # leave the tail parsing as a separate, different statement.
        return False
    if _leaves_open(candidate) and not _leaves_open(lines[anchored_line - 1]):
        # A trailing backslash, unclosed bracket or unclosed triple quote
        # joins the next line. The result can still parse, but the commit
        # then rewrites two statements while the block shows one line.
        return False
    try:
        ast.parse("\n".join(lines))
        replaced = lines[: anchored_line - 1] + [candidate] + lines[anchored_line:]
        ast.parse("\n".join(replaced))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return True


def _leaves_open(line: str) -> bool:
    """True when `line` ends inside a multi-line construct: a backslash
    continuation, an unclosed bracket or an unclosed triple-quoted string."""
    try:
        for _ in tokenize.generate_tokens(io.StringIO(line.lstrip()).readline):
            pass
    except tokenize.TokenError:
        return True
    except SyntaxError:
        # Unbalanced closer or bad token; the parse check decides those.
        return False
    return False
