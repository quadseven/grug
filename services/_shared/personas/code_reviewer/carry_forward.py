"""Carry Elder's still-open earlier findings into a re-review's check.

Elder reviews incrementally (Living Hunt): a push is reviewed as a delta.
A delta that touches other lines finds nothing, and the check went green
while Elder's own earlier inline threads, on untouched code, stayed open.

This module finds those threads from the one review-thread listing the
thread resolver already needs, and folds them into the check verdict:

  - a carried finding is a thread Elder opened (wholly Elder's, with the
    hidden rule marker, first comment not collapsed), not resolved, and
    not outdated (GitHub flips `isOutdated` only when the anchored lines
    changed, so an untouched line means the finding still stands);
  - a finding the new review raises again is not carried (it is already
    counted among the fresh findings);
  - severity comes from the first line of the inline comment body, which
    `dispatch._inline_comment_body` renders as `<chip> | <category> | ...`.

Everything here is pure except `list_threads`, which fails open: a listing
error logs `elder_carry_forward_failed` and the check publishes as before.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from github_app_auth import with_install_token_retry
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.thread_resolver import (
    _NEARBY_LINES,
    _fetch_threads,
    _owned_rule_and_line,
)

log = logging.getLogger(
    f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.carry_forward"
)

MAX_LISTED = 10
_BLOCKING = ("critical", "high")
_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CLEAR_TITLE_RE = re.compile(r"Elder clear - .*$")


@dataclass(frozen=True)
class CarriedFinding:
    path: str
    line: int
    rule: str
    severity: str
    url: str

    @property
    def blocking(self) -> bool:
        return self.severity in _BLOCKING


def parse_severity(body: str) -> str | None:
    """Severity word from the inline body's header line, else None.

    The header is `<emoji> <severity> | _category_ | `rule` | effort`, so
    the severity is the last word before the first pipe.
    """
    first = (body or "").lstrip().split("\n", 1)[0]
    head = first.split("|", 1)[0].strip().split()
    word = head[-1].lower() if head else ""
    return word if word in _RANK else None


def select_carried(
    threads: list[dict], findings: tuple[Finding, ...],
) -> tuple[CarriedFinding, ...]:
    """Open, unchanged, Elder-owned threads this review does not re-raise."""
    out: list[CarriedFinding] = []
    for t in threads:
        if t.get("isResolved") or t.get("isOutdated"):
            continue
        owned = _owned_rule_and_line(t)
        if owned is None:
            continue
        rule, path, original_line = owned
        first = ((t.get("comments") or {}).get("nodes") or [{}])[0]
        severity = parse_severity(first.get("body") or "")
        if severity is None:
            continue
        line = int(first.get("line") or original_line)
        if any(
            f.rule_name == rule and f.file == path
            and abs(f.line - line) <= _NEARBY_LINES
            for f in findings
        ):
            continue
        out.append(CarriedFinding(path, line, rule, severity, first.get("url") or ""))
    out.sort(key=lambda c: (_RANK[c.severity], c.path, c.line))
    return tuple(out)


def list_threads(
    installation_id: int, owner: str, repo: str, pull_number: int,
) -> list[dict] | None:
    """The PR's review threads, or None (logged) when listing fails."""
    try:
        return with_install_token_retry(
            installation_id,
            lambda tok: _fetch_threads(tok, owner, repo, pull_number),
        )
    except Exception as e:  # noqa: BLE001 - fail open: publish as before
        log.warning(
            "elder_carry_forward_failed",
            extra={"pr": f"{owner}/{repo}#{pull_number}", "kind": type(e).__name__},
        )
        return None


def _summary_block(carried: tuple[CarriedFinding, ...]) -> str:
    high = sum(1 for c in carried if c.blocking)
    lines = [
        "## Earlier findings still open",
        "",
        f"{len(carried)} earlier Elder finding(s) sit on code this push did not "
        f"change ({high} high/critical). The delta review above did not look "
        "at them again.",
        "",
    ]
    for c in carried[:MAX_LISTED]:
        where = f"`{c.path.replace('`', '')}:{c.line}`"
        link = f" [thread]({c.url})" if c.url else ""
        lines.append(f"- {where} `{c.rule}` {c.severity}{link}")
    if len(carried) > MAX_LISTED:
        lines.append(f"- ... and {len(carried) - MAX_LISTED} more")
    return "\n".join(lines)


def apply_to_check(
    carried: tuple[CarriedFinding, ...],
    *, conclusion: str, title: str, summary: str, blocking_mode: bool,
) -> tuple[str, str, str]:
    """Fold carried findings into the check's `(conclusion, title, summary)`.

    Only a carried high/critical finding in blocking mode turns a
    non-failing conclusion into `failure`; advisory mode keeps the
    conclusion `_publish_shape` chose, and medium/low never change it.
    """
    if not carried:
        return conclusion, title, summary
    high = sum(1 for c in carried if c.blocking)
    note = f"{len(carried)} earlier finding(s) still open ({high} high/critical)"
    if _CLEAR_TITLE_RE.search(title):
        title = _CLEAR_TITLE_RE.sub(f"Elder: {note}", title)
    else:
        title = f"{title}; {note}"
    summary = f"{summary}\n\n{_summary_block(carried)}"
    if blocking_mode and high and conclusion != "failure":
        conclusion = "failure"
    return conclusion, title, summary
