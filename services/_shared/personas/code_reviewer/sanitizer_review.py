"""Deterministic check for a text sanitiser that eats ordinary words (#1497).

A lane PR added a chat sanitiser that stripped the client's link and colour
markup.  Its tests covered the well-formed markup only, so nothing asserted
that ordinary text comes back unchanged.  The fallback that removed a bare
link opener matched any `|H` or `|h`, so "|Hello there, friend" became an
empty string, "a |hop" became "a op", and "the help|hint" became
"the helpint".  Elder passed the PR.

Two rules, anchored on a new-side line of the sanitiser:

- `sanitizer-eats-plain-text` (high): a regex the function uses changes a
  probe of ordinary text that carries a literal pipe.  The finding names
  the function, the pattern, the input and what the input became.
- `sanitizer-missing-negative-test` (medium): the diff adds or changes a
  text sanitiser and no added test asserts that an ordinary string comes
  back unchanged (the same string appears as the call argument and as the
  expected value).

A sanitiser is a function whose name carries a scrubbing verb and whose
name, body or regex carries a text/markup word (or a markup delimiter).
The check reads the new side of the diff, so it needs no file fetch.
Patterns are compiled with Python's `re`; a pattern it cannot compile is
not evaluated.  Probes are short, so a pattern cannot backtrack far.

Pure: file changes and hunks in; findings out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from personas.code_reviewer.diff_parser import DiffHunk, FileChange
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

RULE_EATS_TEXT = "sanitizer-eats-plain-text"
RULE_MISSING_TEST = "sanitizer-missing-negative-test"
RULE_CATEGORIES = {
    RULE_EATS_TEXT: "correctness",
    RULE_MISSING_TEST: "test fidelity",
}

# Ordinary text with a literal pipe.  A sanitiser that knows the client
# markup leaves every one of these alone; one that treats a bare `|h` or
# `|H` as an opener eats the word behind it.
_PROBES: tuple[str, ...] = (
    "a |hop",
    "|Hello there, friend",
    "the help|hint",
)

_SANITIZER_VERB_RE = re.compile(
    r"(?:saniti[sz]|strip|scrub|clean|redact|neutrali[sz]|escape|unescape|deescape)",
    re.IGNORECASE,
)
_TEXT_HINT_RE = re.compile(
    r"(?:markup|tag|html|chat|message|text|color|colour|link|font|pipe"
    r"|[|<>\[\]])",
    re.IGNORECASE,
)
_DEF_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")
_HEADER_DEF_RE = re.compile(
    r"^@@[^@]*@@\s*(?:async\s+)?def\s+(\w+)\s*\(",
)
_REGEX_CALL_RE = re.compile(
    r"\bre\.(?:subn?|compile|search|match|fullmatch|findall|finditer)\s*\(",
)
_ASSERT_RE = re.compile(r"\b(?:assert|assertEqual|assertEquals)\b")
_COMPARE_RE = re.compile(r"==")

# A sanitiser with more functions than this in one diff is not evaluated.
MAX_FUNCTIONS = 200
# Longest pattern or probe the check feeds to `re` (bounds backtracking).
_MAX_PATTERN = 400


@dataclass
class _Function:
    """One function read from the new side of a diff."""

    path: str
    name: str
    def_line: int
    indent: int
    def_added: bool
    lines: list[str] = field(default_factory=list)
    added: list[tuple[int, str]] = field(default_factory=list)

    @property
    def first_added_line(self) -> int | None:
        return min((line for line, _ in self.added), default=None)

    @property
    def body(self) -> str:
        return "\n".join(self.lines)


def _new_side_lines(hunk: DiffHunk) -> Iterable[tuple[int, str, bool]]:
    """(new-side line number, text, added) for each line of one hunk."""
    new_line = hunk.new_start
    for raw in hunk.body.splitlines()[1:]:
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        added = raw.startswith("+")
        text = raw[1:] if raw.startswith(("+", " ")) else raw
        yield new_line, text, added
        new_line += 1


def _string_literals(text: str) -> list[tuple[int, str]]:
    """(offset, source text) of each string literal on a line.

    The value is the SOURCE between the quotes, so a raw regex keeps its
    backslashes.  A literal with no closing quote is ignored.
    """
    found: list[tuple[int, str]] = []
    i = 0
    while i < len(text):
        quote = text[i]
        if quote not in "'\"":
            i += 1
            continue
        triple = text.startswith(quote * 3, i)
        width = 3 if triple else 1
        end = i + width
        while end < len(text):
            if text[end] == "\\" and not triple:
                end += 2
                continue
            if triple:
                if text.startswith(quote * 3, end):
                    break
            elif text[end] == quote:
                break
            end += 1
        if end >= len(text) or (not triple and text[end] != quote):
            i += width
            continue
        value = text[i + width:end]
        found.append((i, value))
        i = end + width
    return found


def _first_string_after(text: str, offset: int) -> tuple[str, int] | None:
    """The first string literal at or after `offset`."""
    for at, value in _string_literals(text[offset:]):
        return value, offset + at
    return None


def _line_of(text: str, offset: int, start_line: int) -> int:
    return start_line + text.count("\n", 0, offset)


def _patterns_in(body: str, start_line: int) -> list[tuple[str, int]]:
    """(pattern, line) for each `re.*(` call in `body` whose first arg is a
    string literal.  Calls whose first arg is a name are left to the file's
    constants (see `_constant_patterns`)."""
    out: list[tuple[str, int]] = []
    for match in _REGEX_CALL_RE.finditer(body):
        first = _first_string_after(body, match.end())
        if first is None:
            continue
        pattern, at = first
        if len(pattern) > _MAX_PATTERN:
            continue
        out.append((pattern, _line_of(body, at, start_line)))
    return out


def _constant_patterns(lines: Iterable[tuple[int, str, bool]]) -> dict[str, str]:
    """Name -> pattern for a `NAME = re.compile(...)` added or context line."""
    out: dict[str, str] = {}
    for _line, text, _added in lines:
        match = re.match(
            r"^\s*(\w+)\s*=\s*re\.compile\s*\(\s*(['\"])", text,
        )
        if not match:
            continue
        first = _first_string_after(text, match.end() - len(match.group(2)))
        if first and len(first[0]) <= _MAX_PATTERN:
            out[match.group(1)] = first[0]
    return out


def _read_functions(
    path: str, hunks: list[DiffHunk],
) -> tuple[list[_Function], dict[str, str]]:
    """Functions on the new side of one file, plus its regex constants."""
    functions: list[_Function] = []
    current: _Function | None = None
    constants: dict[str, str] = {}
    for hunk in hunks:
        header = _HEADER_DEF_RE.match(hunk.body.splitlines()[0] if hunk.body else "")
        current = None
        if header:
            current = _Function(
                path=path, name=header.group(1), def_line=hunk.new_start,
                indent=0, def_added=False,
            )
            functions.append(current)
        for line, text, added in _new_side_lines(hunk):
            def_match = _DEF_RE.match(text)
            if def_match:
                indent = len(def_match.group(1))
                if current is None or indent <= current.indent:
                    current = _Function(
                        path=path, name=def_match.group(2), def_line=line,
                        indent=indent, def_added=added,
                    )
                    functions.append(current)
            if current is not None:
                current.lines.append(text)
                if added:
                    current.added.append((line, text))
        constants.update(_constant_patterns(_new_side_lines(hunk)))
    return functions, constants


def _is_sanitizer(function: _Function, constants: dict[str, str]) -> bool:
    """A scrubbing verb plus a text/markup signal in its name, body or
    patterns."""
    if not _SANITIZER_VERB_RE.search(function.name):
        return False
    patterns = [pattern for pattern, _line in _patterns_in(
        function.body, function.def_line,
    )]
    for name, pattern in constants.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", function.body):
            patterns.append(pattern)
    if _TEXT_HINT_RE.search(function.name):
        return True
    return bool(_TEXT_HINT_RE.search(function.body)) or any(
        _TEXT_HINT_RE.search(pattern) for pattern in patterns
    )


def _plain_text_change(pattern: str) -> tuple[str, str] | None:
    """(probe, result) for the first ordinary probe the pattern changes."""
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    for probe in _PROBES:
        result = compiled.sub("", probe)
        if result != probe:
            return probe, result
    return None


def _anchor(function: _Function) -> tuple[str, int] | None:
    line = function.first_added_line
    if line is None:
        return None
    return function.path, line


def _pattern_for(function: _Function, constants: dict[str, str]) -> tuple[str, int] | None:
    """The first pattern used by `function` that changes ordinary text."""
    candidates = _patterns_in(function.body, function.def_line)
    for name, pattern in constants.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", function.body):
            candidates.append((pattern, function.def_line))
    for pattern, line in candidates:
        change = _plain_text_change(pattern)
        if change is not None:
            return pattern, line
    return None


def _eats_text_findings(
    functions: Iterable[_Function], constants: dict[str, str],
) -> list[Finding]:
    out: list[Finding] = []
    for function in functions:
        if not function.added:
            continue
        found = _pattern_for(function, constants)
        if found is None:
            continue
        pattern, pattern_line = found
        probe, result = _plain_text_change(pattern)
        anchor = _anchor(function)
        if anchor is None:
            continue
        out.append(Finding(
            file=anchor[0], line=anchor[1], severity="high",
            rule_name=RULE_EATS_TEXT,
            message=(
                f"`{function.name}` uses the regex `{pattern}` (line "
                f"{pattern_line}), which changes ordinary text: `{probe}` "
                f"becomes `{result}`. An alternative that matches a bare "
                "pipe and letter is not link markup. Match only a complete "
                "markup construct, or the client's escaped literal, and "
                "assert ordinary text comes back unchanged."
            ),
            suggestion=None, effort="quick-win",
        ))
    return out


def _test_side(hunks: Iterable[DiffHunk]) -> dict[str, list[tuple[int, str]]]:
    """Added lines per test file."""
    out: dict[str, list[tuple[int, str]]] = {}
    for hunk in hunks:
        if not is_test_file(hunk.file_path):
            continue
        for line, text, added in _new_side_lines(hunk):
            if added:
                out.setdefault(hunk.file_path, []).append((line, text))
    return out


def _has_passthrough_test(
    function_name: str, tests: dict[str, list[tuple[int, str]]],
) -> bool:
    """True when an added test calls `function_name` with a string and its
    expected value is the same string."""
    call_re = re.compile(rf"(?<!\w){re.escape(function_name)}\s*\(")
    inputs: set[str] = set()
    expected: set[str] = set()
    for lines in tests.values():
        for _line, text in lines:
            compares = list(_COMPARE_RE.finditer(text))
            if compares:
                left, right = text[:compares[0].start()], text[compares[0].end():]
            else:
                left, right = text, ""
            if call_re.search(left):
                inputs.update(value for _at, value in _string_literals(left))
            if _ASSERT_RE.search(text) and right:
                expected.update(value for _at, value in _string_literals(right))
    return bool(inputs & expected)


def _missing_test_findings(
    functions: Iterable[_Function], tests: dict[str, list[tuple[int, str]]],
) -> list[Finding]:
    out: list[Finding] = []
    for function in functions:
        if not function.added:
            continue
        if _has_passthrough_test(function.name, tests):
            continue
        anchor = _anchor(function)
        if anchor is None:
            continue
        out.append(Finding(
            file=anchor[0], line=anchor[1], severity="medium",
            rule_name=RULE_MISSING_TEST,
            message=(
                f"`{function.name}` strips text and this diff adds no test "
                "that asserts ordinary text comes back unchanged. A "
                "sanitiser can eat words it did not mean to (a bare pipe and "
                "letter are not markup); add the negative case beside the "
                "well-formed ones."
            ),
            suggestion=None, effort="quick-win",
        ))
    return out


def scan_sanitizers(
    changes: Iterable[FileChange], hunks: tuple[DiffHunk, ...],
) -> tuple[Finding, ...]:
    """The sanitiser findings, or none when the diff has no candidate."""
    by_file: dict[str, list[DiffHunk]] = {}
    for hunk in hunks:
        if not is_test_file(hunk.file_path):
            by_file.setdefault(hunk.file_path, []).append(hunk)
    if not by_file:
        return ()
    tests = _test_side(hunks)
    findings: list[Finding] = []
    seen = 0
    for path, own in by_file.items():
        functions, constants = _read_functions(path, own)
        sanitizers = [f for f in functions if f.added and _is_sanitizer(f, constants)]
        if not sanitizers:
            continue
        seen += len(sanitizers)
        if seen > MAX_FUNCTIONS:
            return ()
        findings.extend(_eats_text_findings(sanitizers, constants))
        findings.extend(_missing_test_findings(sanitizers, tests))
    return tuple(findings)
