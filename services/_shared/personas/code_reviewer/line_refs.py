"""Make a finding's prose line numbers agree with its anchor (grug#1398).

A finding is anchored to a head-commit line (`Finding.line`, the new side
of the diff), and the `**Where:**` footer is rendered from that same
number. The model's free-form `message`, though, sometimes quotes a line
by its pre-change number: a finding anchored at 130 says "at line 103".

The repair is narrow on purpose. A message may name more than one real
line ("the guard at line 90 does not cover the call at line 130"), and
every such number must survive. So a number is rewritten ONLY when the
hunk's old-to-new line map says it is a pre-change alias of the anchor
line (`diff_parser.pre_change_aliases`), and it is not itself a head line
the diff shows for that file. Every other number is left as written.

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


def _shown_head_lines(hunk: DiffHunk) -> set[int]:
    """Every head-commit line the hunk shows: added AND context lines."""
    count = sum(
        1 for raw in hunk.body.splitlines()[1:]
        if not raw.startswith(("-", chr(92)))
    )
    return set(range(hunk.new_start, hunk.new_start + count))


def _same_file(cited: str, path: str) -> bool:
    """`cited` names `path` itself or its trailing components."""
    cited = cited.removeprefix("./")
    return bool(cited) and (path == cited or path.endswith("/" + cited))


def align_line_refs(
    message: str, path: str, line: int, hunks: Iterable[DiffHunk],
) -> str:
    """`message` with each pre-change alias of `line` rewritten to `line`.

    `path`/`line` are the finding's anchor. A number is rewritten when it
    is a pre-change alias of the anchor in one of `path`'s hunks and is
    not a head line any of `path`'s hunks shows. Everything else - other
    real lines, numbers that are not line references, references to other
    files - is returned unchanged."""
    file_hunks = tuple(h for h in hunks if h.file_path == path)
    if not file_hunks or not message:
        return message
    head_lines: set[int] = set()
    aliases: set[int] = set()
    for h in file_hunks:
        head_lines.update(_shown_head_lines(h))
        aliases.update(pre_change_aliases(h, line))
    stale = aliases - head_lines - {line}
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


# A model-written location label: "Where:", "**Where:**", "**Where**:",
# then the reference it names - "`path:N`", "path:N", or "line N".
_WHERE_REF_RE = re.compile(
    r"(?P<label>\*{0,2}Where\*{0,2}:\*{0,2}\s*)"
    r"(?P<tick>`?)(?:(?P<path>[\w./-]+):(?P<num>\d+)|(?P<word>[Ll]ine)\s+(?P<lnum>\d+))"
    r"(?P=tick)"
)


def repair_where_refs(message: str, path: str, line: int) -> str:
    """`message` with every model-written Where reference set to the anchor.

    grug#1398 publish-time check: the rendered `**Where:**` footer names the
    anchor, so a Where the model wrote into its own text that names any
    other location contradicts it. Unlike `align_line_refs`, which keeps
    any number that might be a real line, a Where label is a claim about
    THIS finding's location, so it is repaired to `path:line` (or `line N`
    for the `line N` form) whatever it said. Pure and idempotent."""
    if not message:
        return message

    def fix(m: re.Match[str]) -> str:
        tick = m.group("tick")
        if m.group("word"):
            return f"{m.group('label')}{tick}{m.group('word')} {line}{tick}"
        cited = m.group("path")
        ref = f"{cited}:{line}" if _same_file(cited, path) else f"{path}:{line}"
        return f"{m.group('label')}{tick}{ref}{tick}"

    return _WHERE_REF_RE.sub(fix, message)
