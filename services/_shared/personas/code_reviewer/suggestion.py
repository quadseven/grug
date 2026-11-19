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

Only Python is parse-checked. For every other file the gate still refuses a
candidate that reads as English prose: the review's "So speaks Grug." sign-off,
or three or more distinct common English words outside string literals and
the comment tail. Live instance: mod-overseer PR 1083 offered a one-click
block that would have replaced a C++ line with the sentence describing the
fix. Document formats (markdown, rst, plain text) are exempt from the word
count, since a fix there is prose by nature; the sign-off is refused anywhere.

Multi-line spans (#1759). `checked_span_suggestion` extends the gate to a
bounded local rewrite: the finding's suggestion holds two to six lines and
replaces the Python statement that contains the anchored line. GitHub anchors
such a comment with `start_line` and `line` on the same side, and every line
between them must be visible in ONE diff hunk with no deleted line between two
of them, so the span is checked against the PR diff (`diff_right_side`). A
span that leaves the hunk, crosses a deleted line, is longer than six lines or
sits in a file the gate cannot parse gets no block: the finding renders as
prose, never as a malformed suggestion.

Pure: no I/O, no logging. A None answer means "render the fix as prose",
which is what every finding did before this module existed.
"""
from __future__ import annotations

import ast
import io
import keyword
import re
import tokenize
from dataclasses import dataclass
from typing import Any

from personas.code_reviewer.diff_parser import DiffParseError, parse_diff
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

# Prose detection for files the gate cannot parse-check (mod-overseer PR
# 1083). The sign-off closes every review message, so it never belongs in
# code. The stopwords are common in English instructions and rare as bare
# words in code; three distinct ones outside strings and comments is prose.
_SIGN_OFF = "so speaks grug"
_PROSE_STOPWORDS = frozenset({
    "the", "and", "when", "it", "only", "should", "call", "set", "in", "of",
    "for", "then",
})
_PROSE_STOPWORD_MIN = 3
# Document formats, where a one-line fix is itself prose. Not .txt:
# CMakeLists.txt is build code (same call as verify._PROSE_SUFFIXES).
_DOC_SUFFIXES = (".md", ".markdown", ".rst", ".adoc")
_STRING_LITERAL = re.compile(
    r""""(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`[^`\n]*`"""
)
_COMMENT_TAIL = re.compile(r"(?://|/\*|#).*$")
# A bare word: not part of a dotted name and not the name of a call, so
# `set(item)` and `obj.then` stay code.
_BARE_WORD = re.compile(r"(?<![\w.])([A-Za-z]+)(?![\w(])")


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
    if finding.file.endswith(_PYTHON_SUFFIXES):
        if not _still_parses(lines, anchored_line, candidate):
            return None
    elif _reads_as_prose(candidate, finding.file):
        return None
    return candidate


def _reads_as_prose(candidate: str, path: str) -> bool:
    """True when a candidate for a file the gate cannot parse is English
    prose rather than code (see the module docstring)."""
    if _SIGN_OFF in candidate.lower():
        return True
    if path.lower().endswith(_DOC_SUFFIXES):
        return False
    code = _COMMENT_TAIL.sub("", _STRING_LITERAL.sub('""', candidate))
    words = {w.lower() for w in _BARE_WORD.findall(code)}
    return len(words & _PROSE_STOPWORDS) >= _PROSE_STOPWORD_MIN


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
    malformed or touches any row other than `row`, before or after it."""
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
    except (SyntaxError, ValueError):
        # Unbalanced closer, bad token, or a lone surrogate the tokenizer
        # cannot encode (UnicodeEncodeError); the parse check decides those.
        return False
    return False


# --- multi-line spans (#1759) ------------------------------------------------

# Both the replaced span and the replacement stay this short: a rewrite longer
# than a handful of lines is a refactor, which stays prose.
MAX_SPAN_LINES = 6

_HUNK_START = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


@dataclass(frozen=True, slots=True)
class HunkView:
    """The right (new) side of one hunk, as GitHub lets a comment anchor to it.

    `visible` is every new-side line the hunk shows, added or context.
    `cuts` holds each visible line that a deleted line follows, so a span that
    would run from that line to the next crosses the deletion."""

    visible: frozenset[int]
    cuts: frozenset[int]

    def holds(self, start: int, end: int) -> bool:
        """True when start..end are all visible with no deletion between."""
        if not all(n in self.visible for n in range(start, end + 1)):
            return False
        return not any(n in self.cuts for n in range(start, end))


@dataclass(frozen=True, slots=True)
class SpanSuggestion:
    """A checked multi-line replacement of `start_line`..`end_line` (1-based,
    inclusive, new side). `text` is the exact block content."""

    start_line: int
    end_line: int
    text: str


def diff_right_side(diff_text: str) -> dict[str, tuple[HunkView, ...]]:
    """`{path: hunk views}` for the unified diff. Empty for no diff or one
    the parser refuses, which leaves every multi-line fix as prose."""
    try:
        hunks = parse_diff(diff_text)
    except DiffParseError:
        return {}
    out: dict[str, list[HunkView]] = {}
    for hunk in hunks:
        view = _hunk_view(hunk.body)
        if view is not None:
            out.setdefault(hunk.file_path, []).append(view)
    return {path: tuple(views) for path, views in out.items()}


def _hunk_view(body: str) -> HunkView | None:
    rows = body.split("\n")
    header = _HUNK_START.match(rows[0]) if rows else None
    if header is None:
        return None
    cursor = int(header.group(1))
    visible: set[int] = set()
    cuts: set[int] = set()
    for raw in rows[1:]:
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            if cursor - 1 in visible:
                cuts.add(cursor - 1)
            continue
        # Added, context, or an empty context line some tools trim.
        visible.add(cursor)
        cursor += 1
    return HunkView(frozenset(visible), frozenset(cuts))


def checked_span_suggestion(
    finding: Finding,
    file_text: str,
    hunks: tuple[HunkView, ...],
) -> SpanSuggestion | None:
    """The multi-line replacement for the statement holding the finding's
    line, or None when no such fix survives the checks.

    Only a Python file is offered one: the span is the innermost statement
    containing `finding.line`, and the gate needs it to be exact. The span
    must fit one hunk of `hunks`, hold at most `MAX_SPAN_LINES` lines, and
    the replacement must be two to `MAX_SPAN_LINES` lines that change the
    span and leave the file parsing."""
    if finding.suggestion is None or not finding.file.endswith(_PYTHON_SUFFIXES):
        return None
    replacement = _lines_of(finding.suggestion)
    if replacement is None or not 2 <= len(replacement) <= MAX_SPAN_LINES:
        return None
    lines = file_text.splitlines()
    span = _statement_span(file_text, finding.line)
    if span is None or span[1] > len(lines):
        return None
    start, end = span
    if end - start + 1 > MAX_SPAN_LINES:
        return None
    if not any(view.holds(start, end) for view in hunks):
        return None
    original = lines[start - 1:end]
    replacement = _indented_like(replacement, original[0])
    if [r.rstrip() for r in replacement] == [o.rstrip() for o in original]:
        return None
    if original[-1].rstrip().endswith("\\"):
        return None
    text = "\n".join(replacement)
    if _leaves_open(text) and not _leaves_open("\n".join(original)):
        return None
    if _SIGN_OFF in text.lower():
        return None
    try:
        ast.parse("\n".join(lines))
        ast.parse("\n".join(lines[: start - 1] + replacement + lines[end:]))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    return SpanSuggestion(start, end, text)


def _lines_of(suggestion: str) -> list[str] | None:
    """The suggestion split into lines, or None for a blank one, one that
    carries a fence, or one with a bare carriage return. Leading indentation
    is kept: GitHub commits the block verbatim."""
    text = suggestion.replace("\r\n", "\n").strip("\n")
    if not text.strip() or "\r" in text or "```" in text:
        return None
    return text.split("\n")


def _indented_like(replacement: list[str], first_original: str) -> list[str]:
    """Give a replacement written without indentation the indentation of the
    first replaced line. One that already starts indented is left verbatim."""
    if replacement[0][:1] in (" ", "\t"):
        return replacement
    indent = first_original[: len(first_original) - len(first_original.lstrip())]
    return [indent + r if r.strip() else r for r in replacement]


def _statement_span(file_text: str, line: int) -> tuple[int, int] | None:
    """(first, last) line of the innermost statement containing `line`."""
    try:
        tree = ast.parse(file_text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    best: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        first, last = node.lineno, node.end_lineno
        if last is None or not first <= line <= last:
            continue
        if best is None or last - first < best[1] - best[0]:
            best = (first, last)
    return best
