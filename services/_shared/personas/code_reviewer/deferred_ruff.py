"""Re-check the ruff findings a settle-findings ticket names (#1477).

When Elder passes a pull request with medium findings open, the station files
a follow-up ticket with one line per finding:

    - medium | _general_ | `lint-bug` in `services/api/app.py`: <thread url>

The line carries no rule code; the code is in the linked review comment, which
Elder wrote as "ruff `B904`: ...". A lane can then land the follow-up without
fixing the finding, as grug PR 1470 did: its two B904 findings survived on a
head Elder marked clean. A ruff finding is measurable, so this module measures
it again: the same ruff run as `lint.py`, over the named file at the head.

Only the `lint-*` rule names `lint.py` gives ruff findings are read.
Model-judged findings such as `dead-code` have nothing to measure.

An unfinished re-check never reads as a clean one: dispatch degrades the
review, the shape of the removed-reference check (#1401).

A FINDING OUTSIDE THE DIFF. GitHub refuses a thread on a line outside the PR's
diff, and the named file may not be in the diff at all. So a finding sits on
the firing line when the diff has it, else on another diff line, and its text
names the real location. Such an anchor does not change when the code is
fixed, so a thread here is settled by the next measurement, not by GitHub's
`isOutdated`: `carry_forward` does not carry the rule and `thread_resolver`
closes it once the review stops raising it. With no free diff line left, the
finding stays in the check summary through `outside_diff_reason`.

Pure: ticket text, comment text and ruff results in; findings out.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.lint import RuffHit
from personas.code_reviewer.persona import Finding

RULE = "unmet-criterion"
RULE_CATEGORIES = {RULE: "requirements"}

# Each entry costs one GitHub read and the ticket text is untrusted.
MAX_DEFERRED = 10

_OUTSIDE = "GitHub cannot hold a thread here: this pull request has no free diff line."

# The line the station writes; `| quick win` is the effort chip it may carry.
_DEFERRED_LINE_RE = re.compile(
    r"`lint-(?:bug|security|async|violation)`(?: \| [\w -]+)? in `([^`\n]+\.py)`: "
    r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/\d+#discussion_r(\d+)"
)
_RUFF_CODE_RE = re.compile(r"\bruff `([A-Z]+[0-9]+)`:")


@dataclass(frozen=True, slots=True)
class DeferredLint:
    """One deferred ruff finding as its ticket names it."""

    issue: int
    path: str
    comment_id: int


def parse_deferred_lint(
    issue_body: str, *, issue: int, owner: str, repo: str,
) -> tuple[DeferredLint, ...]:
    """The ticket's deferred lint lines on this repository, one per thread.

    A rewritten ticket repeats each line in its quoted original, hence the
    dedupe on the thread. Threads of another repository are never read, and
    a path that leaves the repository is never measured."""
    found: dict[int, DeferredLint] = {}
    for m in _DEFERRED_LINE_RE.finditer(issue_body or ""):
        if m.group(2).casefold() != owner.casefold():
            continue
        if m.group(3).casefold() != repo.casefold():
            continue
        if m.group(1).startswith("/") or ".." in m.group(1).split("/"):
            continue
        comment_id = int(m.group(4))
        found.setdefault(comment_id, DeferredLint(issue, m.group(1), comment_id))
    return tuple(found.values())[:MAX_DEFERRED]


def rule_code(comment_body: str) -> str | None:
    """The ruff code in Elder's review comment, or None for any other text."""
    m = _RUFF_CODE_RE.search(comment_body or "")
    return m.group(1) if m else None


def _diff_lines(hunks: Iterable[DiffHunk]) -> dict[str, list[int]]:
    lines: dict[str, set[int]] = {}
    for hunk in hunks:
        lines.setdefault(hunk.file_path, set()).update(hunk.new_lines)
    return {path: sorted(rows) for path, rows in sorted(lines.items()) if rows}


def _candidates(
    path: str, rows: list[int], diff: dict[str, list[int]],
) -> Iterator[tuple[str, int]]:
    """Anchors in order: a firing line, the named file, any other file."""
    own = diff.get(path, [])
    yield from ((path, row) for row in rows if row in own)
    yield from ((path, row) for row in own)
    for other, others in diff.items():
        yield from ((other, row) for row in others)


def _message(ref: DeferredLint, code: str, rows: list[int], text: str, on_line: bool) -> str:
    lines = ", ".join(str(row) for row in rows)
    message = (
        f"Ticket #{ref.issue} defers ruff `{code}` in `{ref.path}`, and it still "
        f"fires at this head: {text}. Line(s) {lines}. Fix it, or silence it with "
        f"`# noqa: {code}` and a reason if it is wrong here."
    )
    if on_line:
        return message
    return message + (
        " This thread sits on a changed line only because GitHub holds threads "
        "on diff lines; it closes when the rule stops firing in that file."
    )


def unmet_findings(
    named: Iterable[tuple[DeferredLint, str]], hits: Iterable[RuffHit],
    hunks: tuple[DiffHunk, ...],
) -> tuple[Finding, ...]:
    """One high finding for each named rule still firing in its named file.

    Two findings on one line would dedupe into one, hence the used anchors."""
    fired: dict[tuple[str, str], list[RuffHit]] = {}
    for hit in hits:
        fired.setdefault((hit.path, hit.code), []).append(hit)
    diff = _diff_lines(hunks)
    used: set[tuple[str, int]] = set()
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for ref, code in named:
        key = (ref.path, code)
        if key in seen or key not in fired:
            continue
        seen.add(key)
        rows = sorted(hit.row for hit in fired[key])
        anchor = next((c for c in _candidates(ref.path, rows, diff) if c not in used), None)
        if anchor:
            used.add(anchor)
        file, line = anchor or (ref.path, rows[0])
        text = fired[key][0].message
        findings.append(Finding(
            file=file,
            line=line,
            severity="high",
            rule_name=RULE,
            message=_message(ref, code, rows, text, on_line=file == ref.path and line in rows),
            suggestion=None,
            check_output=f"{ref.path}:{rows[0]}: {code} {text}",
            outside_diff_reason=None if anchor else _OUTSIDE,
        ))
    return tuple(findings)
