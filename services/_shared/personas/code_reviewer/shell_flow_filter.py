"""Deterministic post-filter for shell `missing-error-handling` findings (#771).

The Elder flagged `if ! dig ...; then exit 1; fi` under `set -euo pipefail`
as an unchecked command. The `if !` IS the exit-status check, and the
errexit flag backs it, so the finding was a false positive from matching
the invocation without reading its control flow.

This module is the deterministic backstop: it runs on the LLM findings
right after ``evaluate_diff`` (see ``_filter_checked_shell_commands`` in
``dispatch.py``) and drops a ``missing-error-handling`` finding on a shell
file when the flagged command's exit status is already consumed:

- the logical line opens a condition: ``if``, ``elif``, ``while``,
  ``until`` (with or without ``!``), or is negated with a leading ``!``;
- the logical line chains with ``&&`` or ``||`` (which covers
  ``|| true``, ``|| exit N`` and ``|| return N``);
- the next non-blank line reads ``$?`` (``case "$?" in``, ``rc=$?``,
  ``[ $? -ne 0 ]``);
- errexit (``set -e``, ``set -euo pipefail``, ``set -o errexit`` or a
  ``-e`` shebang flag) is on at that line and the command is not masked:
  not a pipeline without ``pipefail``, and not a ``local``/``export``/
  ``declare``/``readonly`` assignment, whose own status hides the
  substitution's.

Pure: findings in, findings out. No IO. Fail-open: a finding on a file
whose text is unknown, a non-shell file, or a line out of range is kept.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from personas.code_reviewer.persona import Finding

_RULE = "missing-error-handling"

_SHELL_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh", ".dash", ".bats")
_SHEBANG_RE = re.compile(r"^#!\s*\S*(?:/|\s)(?:env\s+)?(?:ba|z|k|da)?sh\b")

_CONDITION_RE = re.compile(r"^(?:(?:if|elif|while|until)\s+|!\s)")
_CHAIN_RE = re.compile(r"&&|\|\|")
# A single `|` that is not half of `||`.
_PIPE_RE = re.compile(r"(?<!\|)\|(?!\|)")
_STATUS_READ_RE = re.compile(r"\$\?|\$\{\?\}")
_MASKING_DECL_RE = re.compile(
    r"^(?:local|export|declare|typeset|readonly)\s+[^=\s]+=.*\$\(",
)
_SET_RE = re.compile(r"^set\s+(.*)$")
_FLAG_TOKEN_RE = re.compile(r"[-+][A-Za-z]+")
_SHEBANG_FLAGS_RE = re.compile(r"^#!\S+(?:\s+\S+)*?\s+-([A-Za-z]*)")
# Strip quoted text and trailing comments before looking for operators, so
# a `||` inside a string or a comment does not count as a check.
_SINGLE_QUOTED_RE = re.compile(r"'[^']*'")
_DOUBLE_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")


def is_shell_file(path: str, text: str) -> bool:
    """True when `path` is a shell script by suffix or by shebang."""
    if path.lower().endswith(_SHELL_SUFFIXES):
        return True
    first = text.split("\n", 1)[0]
    return bool(_SHEBANG_RE.match(first))


def _code(line: str) -> str:
    """The line with quoted spans and a trailing comment removed."""
    line = _SINGLE_QUOTED_RE.sub("''", line)
    line = _DOUBLE_QUOTED_RE.sub('""', line)
    return _COMMENT_RE.sub("", line).strip()


def _logical_span(lines: list[str], index: int) -> tuple[int, int]:
    """Start and end (inclusive) indexes of the backslash-continued command
    that contains `lines[index]`."""
    start = index
    while start > 0 and lines[start - 1].rstrip().endswith("\\"):
        start -= 1
    end = index
    while end < len(lines) - 1 and lines[end].rstrip().endswith("\\"):
        end += 1
    return start, end


def _set_flags(lines: list[str], before: int) -> tuple[bool, bool]:
    """(errexit, pipefail) as the script leaves them before line `before`.

    Last `set` wins, so a later `set +e` turns errexit back off."""
    errexit = pipefail = False
    if lines:
        shebang = _SHEBANG_FLAGS_RE.match(lines[0])
        if shebang:
            errexit = "e" in shebang.group(1)
    for raw in lines[:before]:
        match = _SET_RE.match(_code(raw))
        if not match:
            continue
        tokens = match.group(1).split()
        for i, token in enumerate(tokens):
            if not _FLAG_TOKEN_RE.fullmatch(token):
                continue
            on = token[0] == "-"
            if "e" in token:
                errexit = on
            # `-o NAME` / `-euo NAME`: the next token names a long option.
            if "o" in token and i + 1 < len(tokens):
                if tokens[i + 1] == "errexit":
                    errexit = on
                elif tokens[i + 1] == "pipefail":
                    pipefail = on
    return errexit, pipefail


def _next_code_line(lines: list[str], after: int) -> str:
    """The next non-blank, non-comment line after `after`. Double-quoted
    text is kept: `"$?"` still expands inside double quotes."""
    for raw in lines[after + 1:]:
        code = _COMMENT_RE.sub("", _SINGLE_QUOTED_RE.sub("''", raw)).strip()
        if code:
            return code
    return ""


def status_is_checked(text: str, line: int) -> bool:
    """True when the shell command on 1-based `line` of `text` has its exit
    status consumed by control flow or by errexit."""
    lines = text.split("\n")
    index = line - 1
    if not 0 <= index < len(lines):
        return False
    start, end = _logical_span(lines, index)
    command = " ".join(_code(raw).rstrip("\\") for raw in lines[start:end + 1])
    command = command.strip()
    if not command:
        return False
    if _CONDITION_RE.match(command) or _CHAIN_RE.search(command):
        return True
    if _STATUS_READ_RE.search(_next_code_line(lines, end)):
        return True
    errexit, pipefail = _set_flags(lines, start)
    if not errexit:
        return False
    if _MASKING_DECL_RE.match(command):
        return False
    return pipefail or not _PIPE_RE.search(command)


def filter_checked_shell_findings(
    findings: Iterable[Finding], file_contents: Mapping[str, str],
) -> tuple[tuple[Finding, ...], int]:
    """Drop `missing-error-handling` findings on shell commands whose exit
    status is already checked. Returns (kept, dropped_count)."""
    kept: list[Finding] = []
    dropped = 0
    for finding in findings:
        text = file_contents.get(finding.file)
        if (
            finding.rule_name == _RULE
            and isinstance(text, str)
            and is_shell_file(finding.file, text)
            and status_is_checked(text, finding.line)
        ):
            dropped += 1
            continue
        kept.append(finding)
    return tuple(kept), dropped
