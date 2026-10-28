"""Make a finding's prose line numbers agree with its anchor (grug#1398).

A finding is anchored to a head-commit line (`Finding.line`, the new side
of the diff), and the `**Where:**` footer is rendered from that same
number. The model's free-form `message`, though, sometimes quotes a line
by its pre-change number: a finding anchored at 130 says "at line 103".

The repair is narrow on purpose. A message may name more than one real
line ("the guard at line 90 does not cover the call at line 130"), and
every such number must survive. So a number is rewritten ONLY when the
hunk's old-to-new line map says it is a pre-change alias of the anchor
line (`diff_parser.pre_change_aliases`) AND it cannot be a line of the
head file at all: it lies past the head file's last line, which the diff
proves only when it shows that end (a backslash "No newline at end of
file" marker on the new side). Any number that is, or may be, a real head
line - shown by the diff or not - is left as written.

Pure: no IO. Chunked reviews need nothing extra: each cohort keeps the
whole hunks with their original `@@` headers, so the map is the same
whichever call raised the finding.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from personas.code_reviewer.diff_parser import DiffHunk, pre_change_aliases

# "line 103", "lines 119 and 116", "lines 10, 12 or 14", "lines 10-14".
_LINE_WORD_REF_RE = re.compile(
    r"\b(?P<word>[Ll]ines?)\s+(?P<nums>\d+(?:\s*(?:,|-|and|or|to|through)\s*\d+)*)\b"
)
# "`todos.js:151`" or "src/todos.js:151" - a path-qualified reference.
_PATH_REF_RE = re.compile(r"(?P<path>[\w./-]+):(?P<num>\d+)\b")
_NUM_RE = re.compile(r"\d+")


def _head_line_count(hunks: Iterable[DiffHunk]) -> int | None:
    """The head file's exact line count, or None when the hunks do not show it.

    The diff shows the new side's end only through a backslash
    "No newline at end of file" marker right after an added or context
    line; without one, any number past the shown lines may still be a real
    head line."""
    count: int | None = None
    for h in hunks:
        new_cursor = h.new_start
        prev = ""
        for raw in h.body.splitlines()[1:]:
            if raw.startswith(chr(92)):
                if prev and not prev.startswith("-"):
                    count = new_cursor - 1
                continue
            prev = raw
            if not raw.startswith("-"):
                new_cursor += 1
    return count


def _same_file(cited: str, path: str) -> bool:
    """`cited` names `path` itself or its trailing components."""
    cited = cited.removeprefix("./")
    return bool(cited) and (path == cited or path.endswith("/" + cited))


def align_line_refs(
    message: str, path: str, line: int, hunks: Iterable[DiffHunk],
) -> str:
    """`message` with each pre-change alias of `line` rewritten to `line`.

    `path`/`line` are the finding's anchor. A number is rewritten only when
    it is a pre-change alias of the anchor in one of `path`'s hunks and the
    hunks prove it is past the head file's last line. A number that is or
    may be a real head line (grug#1398 operator ruling: a finding citing
    two lines keeps both), a number that is not a line reference, and a
    reference to another file are returned unchanged."""
    file_hunks = tuple(h for h in hunks if h.file_path == path)
    if not file_hunks or not message:
        return message
    head_count = _head_line_count(file_hunks)
    if head_count is None:
        return message
    aliases: set[int] = set()
    for h in file_hunks:
        aliases.update(pre_change_aliases(h, line))
    stale = {n for n in aliases if n > head_count} - {line}
    if not stale:
        return message

    def fix(num: str) -> str:
        return str(line) if int(num) in stale else num

    def word_ref(m: re.Match[str]) -> str:
        nums = _NUM_RE.sub(lambda n: fix(n.group(0)), m.group("nums"))
        return m.group(0)[: m.start("nums") - m.start()] + nums

    def path_ref(m: re.Match[str]) -> str:
        if not _same_file(m.group("path"), path):
            return m.group(0)
        return f"{m.group('path')}:{fix(m.group('num'))}"

    return _PATH_REF_RE.sub(path_ref, _LINE_WORD_REF_RE.sub(word_ref, message))


# A model-written location label: "Where:", "**Where:**", "**Where**:".
_WHERE_LABEL_RE = re.compile(r"\*{0,2}Where\*{0,2}:\*{0,2}\s*")
# The reference right after the label: "`path:N`", "path:N", or "line N".
_WHERE_HEAD_REF_RE = re.compile(
    r"(?P<tick>`?)(?:(?P<path>[\w./-]+):(?P<num>\d+)|(?P<word>[Ll]ine)\s+(?P<lnum>\d+))"
    r"(?P=tick)"
)


def _ref_count(text: str) -> int:
    """How many line locations `text` names (`path:N`, `line N`, `lines A and B`)."""
    count = len(_PATH_REF_RE.findall(text))
    for m in _LINE_WORD_REF_RE.finditer(text):
        count += len(_NUM_RE.findall(m.group("nums")))
    return count


def repair_where_refs(message: str, path: str, line: int) -> str:
    """`message` with a model-written single-location Where set to the anchor.

    grug#1398 publish-time check: the rendered `**Where:**` footer names the
    anchor, so a Where the model wrote into its own text that names ONE
    other line of the anchor's own file contradicts it, and its number is
    set to the anchor. Everything else is left as written: a Where that
    names two or more locations (operator ruling: a finding citing two
    lines keeps both), a Where naming another file, and a Where that
    already names the anchor. A Where's scope is the rest of its line, up
    to the next Where label. Pure and idempotent."""
    if not message:
        return message
    labels = list(_WHERE_LABEL_RE.finditer(message))
    if not labels:
        return message
    out: list[str] = []
    pos = 0
    for i, lab in enumerate(labels):
        scope_end = labels[i + 1].start() if i + 1 < len(labels) else len(message)
        newline = message.find("\n", lab.end(), scope_end)
        if newline != -1:
            scope_end = newline
        scope = message[lab.end():scope_end]
        out.append(message[pos:lab.end()])
        out.append(_repair_scope(scope, path, line))
        pos = scope_end
    out.append(message[pos:])
    return "".join(out)


def _repair_scope(scope: str, path: str, line: int) -> str:
    m = _WHERE_HEAD_REF_RE.match(scope)
    if m is None or _ref_count(scope) != 1:
        return scope
    tick = m.group("tick")
    if m.group("word"):
        if int(m.group("lnum")) == line:
            return scope
        ref = f"{m.group('word')} {line}"
    else:
        cited = m.group("path")
        if not _same_file(cited, path) or int(m.group("num")) == line:
            return scope
        ref = f"{cited}:{line}"
    return f"{tick}{ref}{tick}{scope[m.end():]}"
