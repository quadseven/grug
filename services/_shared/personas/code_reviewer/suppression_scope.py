"""Which inline suppression comments a deterministic analyzer may honor (#1579).

Ruff honors `# noqa` and opengrep honors `// nosemgrep` inside the files they
scan. Those files are the PR's head revision, so the comments are
PR-controlled: an added line could carry its own suppression and the engine
would never report it. The engines therefore run with in-file suppression
turned off (`ruff --ignore-noqa`, `opengrep --disable-nosem`), and this module
puts the repo's own suppressions back, one finding at a time:

- a matching suppression on a line the PR did not add is the repo's existing
  decision, so the finding is dropped exactly as the engine would have;
- a matching suppression only on lines the PR added does not hide anything:
  the finding is kept, advisory, with a note that the PR added it;
- a suppression scoped to a different rule never matches.

Kept findings stay advisory on purpose: the false-flag rate is sampled from
the `analyzer_suppression_scoped` log events before any blocking use.
"""
from __future__ import annotations

import logging
import re
from typing import Literal

log = logging.getLogger("grug.code_reviewer.suppression_scope")

Scope = Literal["none", "base", "pr_added"]

PR_ADDED_NOTE = (
    "This PR added the inline suppression comment for this rule, so the "
    "finding is reported as advisory rather than hidden. A suppression "
    "already on the base branch is still honored."
)

# Where a line comment starts, in the languages the rule packs read.
_COMMENT_START = re.compile(r"#|//|/\*|<!--")
# `nosemgrep` / `nosem`, optionally scoped: `nosemgrep: rule-a, rule-b`.
_NOSEM_RE = re.compile(
    r"(?<![A-Za-z0-9_-])nosem(?:grep)?\b"
    r"(?:\s*:\s*(?P<ids>[A-Za-z0-9_.\-]+(?:\s*,\s*[A-Za-z0-9_.\-]+)*))?",
    re.IGNORECASE,
)
# Line-level noqa, blanket or scoped to codes such as S608 and B023.
_NOQA_RE = re.compile(
    r"#\s*noqa\b(?:\s*:\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*))?",
    re.IGNORECASE,
)
# File-level noqa under the ruff or flake8 prefix, blanket or scoped.
_FILE_NOQA_RE = re.compile(
    r"#\s*(?:ruff|flake8)\s*:\s*noqa\b"
    r"(?:\s*:\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*))?",
    re.IGNORECASE,
)


def _ids(raw: str | None) -> tuple[str, ...]:
    return tuple(part for part in re.split(r"[\s,]+", raw or "") if part)


def _nosem_matches(text: str, rule_id: str) -> bool:
    # Only comment text counts: `x = "nosemgrep"` is a string, not a directive.
    start = _COMMENT_START.search(text)
    if start is None:
        return False
    for m in _NOSEM_RE.finditer(text, start.start()):
        ids = _ids(m.group("ids"))
        if not ids:
            return True
        if any(i == rule_id or rule_id.endswith("." + i) for i in ids):
            return True
    return False


def _noqa_matches(regex: re.Pattern[str], text: str, code: str) -> bool:
    for m in regex.finditer(text):
        codes = _ids(m.group("codes"))
        if not codes or code in codes:
            return True
    return False


def _scope(suppressing: list[int], pr_added: set[int]) -> Scope:
    if any(n not in pr_added for n in suppressing):
        return "base"
    return "pr_added" if suppressing else "none"


def _line(lines: list[str], n: int) -> str:
    return lines[n - 1] if 1 <= n <= len(lines) else ""


def opengrep_scope(
    source: str | None, line: int, rule_id: str, pr_added: set[int],
) -> Scope:
    """Scope of a `nosemgrep` for `rule_id` on `line` or the line above it,
    the two places the engine reads one."""
    if source is None:
        return "none"
    lines = source.splitlines()
    suppressing = [n for n in (line, line - 1)
                   if n >= 1 and _nosem_matches(_line(lines, n), rule_id)]
    return _scope(suppressing, pr_added)


def ruff_scope(
    source: str | None, line: int, code: str, pr_added: set[int],
) -> Scope:
    """Scope of a `noqa` for `code` on `line`, or a file-level
    `# ruff: noqa` anywhere in the file."""
    if source is None:
        return "none"
    lines = source.splitlines()
    suppressing = [line] if _noqa_matches(_NOQA_RE, _line(lines, line), code) else []
    suppressing += [n for n, text in enumerate(lines, start=1)
                    if _noqa_matches(_FILE_NOQA_RE, text, code)]
    return _scope(suppressing, pr_added)


def record(engine: str, rule: str, path: str, scope: Scope) -> None:
    """One counter event per suppression this module decided, for the
    false-flag sample."""
    if scope == "none":
        return
    log.info("analyzer_suppression_scoped", extra={
        "engine": engine, "rule": rule, "path": path,
        "outcome": "base_honored" if scope == "base" else "pr_added_kept",
    })
