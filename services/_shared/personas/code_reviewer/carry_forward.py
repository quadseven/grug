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
  - resolved is read from the thread's own state first (#1440): the
    `isResolved` flag, or a resolver (`resolvedBy`) on the thread when
    the flag itself is not in the listing. A thread a person resolved
    is left out even when its anchored code did not change; a thread a
    person reopened has neither the flag nor a resolver, so it counts
    as open again;
  - a finding the new review raises again is not carried (it is already
    counted among the fresh findings);
  - a finding a maintainer dismissed (living review, #1187) is not carried;
  - a deferred-ruff thread is not carried: its anchor can be a line the
    fix never touches, and every review measures the rule again (#1477);
  - severity comes from the first line of the inline comment body, which
    `dispatch._inline_comment_body` renders as `<chip> | <category> | ...`
    (read from the original body when living review has annotated it).

Everything here is pure except `list_threads`, which fails open: a listing
error logs `elder_carry_forward_failed` and the check publishes as before.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace

from github_app_auth import with_install_token_retry
from personas.code_reviewer import deferred_ruff, living_review
from personas.code_reviewer.dedup import parse_rule
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.thread_resolver import (
    _NEARBY_LINES,
    _fetch_threads,
    _owned_rule_and_line,
    is_grug,
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


def _standing_severity(body: str) -> str | None:
    """The finding's severity, or None when it no longer stands: a
    maintainer dismissed it (#1187) or its header does not parse. Living
    review's annotated bodies are read from the preserved original."""
    if living_review.parse_state(body) == living_review.DISMISSED:
        return None
    return parse_severity(living_review.original_body(body))


def _thread_is_resolved(thread: dict) -> bool:
    """The thread's resolved state, read before anything else (#1440).

    The listing's `isResolved` flag is the primary signal; a resolver
    (`resolvedBy`, a login or a dict carrying one) is the same state in
    the other shape GitHub reports it, and counts when the flag itself
    is not in the thread dict. A reopened thread has neither, so this
    returns False for it and the finding counts as open again."""
    if thread.get("isResolved"):
        return True
    resolver = thread.get("resolvedBy")
    if isinstance(resolver, dict):
        return bool(resolver.get("login"))
    return bool(resolver)


def _carriable(thread: dict) -> tuple[str, str, int] | None:
    """`(rule, path, original_line)` of an open, unchanged Elder thread whose
    rule is not measured again on every review, else None."""
    if _thread_is_resolved(thread) or thread.get("isOutdated"):
        return None
    owned = _owned_rule_and_line(thread)
    if owned is None or owned[0] == deferred_ruff.RULE:
        return None
    return owned


def select_carried(
    threads: list[dict], findings: tuple[Finding, ...],
) -> tuple[CarriedFinding, ...]:
    """Open, unchanged, Elder-owned threads this review does not re-raise."""
    out: list[CarriedFinding] = []
    for t in threads:
        owned = _carriable(t)
        if owned is None:
            continue
        rule, path, original_line = owned
        first = ((t.get("comments") or {}).get("nodes") or [{}])[0]
        severity = _standing_severity(first.get("body") or "")
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


_KNOWN_SECURITY_RULES = frozenset({
    "cleartext-secret-log",
    "exposed-secret",
    "iac-misconfig",
    "lint-security",
})


def _is_security_or_sast(finding: Finding) -> bool:
    """True when the finding is in the security category or comes from a SAST rule."""
    if getattr(finding, "category", None) == "security":
        return True
    rule = getattr(finding, "rule_name", "") or ""
    if rule in _KNOWN_SECURITY_RULES:
        return True
    return bool(rule.startswith("grug-") or "security" in rule or "sast" in rule)


def _normalize_login(login: str | None) -> str:
    """Normalize a GitHub login for comparison by stripping [bot] and lowercasing."""
    if not login:
        return ""
    return str(login).removesuffix("[bot]").strip().lower()


def _is_app_actor(actor: dict | str | None) -> bool:
    """True when the actor is a GitHub App or Bot."""
    if not actor:
        return False
    if isinstance(actor, dict):
        typename = str(actor.get("__typename") or actor.get("type") or "").strip().lower()
        if typename == "bot":
            return True
        login = str(actor.get("login") or "")
    else:
        login = str(actor)
    login_clean = login.strip().lower()
    return bool(login_clean.endswith("[bot]") or is_grug(login))


def _is_pr_author(actor: dict | str | None, pr_author: str | None) -> bool:
    """True when the actor matches the PR author."""
    if not pr_author or not actor:
        return False
    actor_login = actor.get("login") if isinstance(actor, dict) else actor
    norm_actor = _normalize_login(str(actor_login or ""))
    norm_author = _normalize_login(pr_author)
    return bool(norm_actor and norm_actor == norm_author)


def _thread_resolver_counts(thread: dict, pr_author: str | None = None) -> bool:
    """True when the thread was resolved by a valid maintainer (not author, not App).

    A resolve by the PR author or an App must never hide a finding on re-review (#1582).
    If resolvedBy is not provided on a resolved thread, it is treated as a valid
    maintainer resolve for backwards compatibility with test fixtures.
    """
    if not _thread_is_resolved(thread):
        return False
    resolver = thread.get("resolvedBy")
    if not resolver:
        return True
    if _is_pr_author(resolver, pr_author):
        return False
    if _is_app_actor(resolver):
        return False
    return True


def _thread_is_dismissed(thread: dict) -> bool:
    """True when the first comment has been dismissed via living review."""
    first = ((thread.get("comments") or {}).get("nodes") or [{}])[0]
    return living_review.parse_state(first.get("body") or "") == living_review.DISMISSED


def _thread_is_resolved_or_dismissed(thread: dict) -> bool:
    """True when the thread is resolved (by a person) or dismissed (living review)."""
    if _thread_is_resolved(thread):
        return True
    return _thread_is_dismissed(thread)


def _elder_thread_rule_and_line(thread: dict) -> tuple[str, str, int] | None:
    """`(rule, path, line)` of an Elder-initiated thread, regardless of human replies."""
    comments = thread.get("comments") or {}
    nodes = comments.get("nodes") or []
    if not nodes:
        return None
    first = nodes[0]
    if not is_grug((first.get("author") or {}).get("login")):
        return None
    rule = parse_rule(first.get("body") or "")
    path = first.get("path")
    line = first.get("line") or first.get("originalLine")
    if not rule or not path or not line:
        return None
    return rule, path, int(line)


def _earlier_answer(thread: dict) -> str | None:
    """The text of an earlier reply from a person on this thread, if any."""
    nodes = ((thread.get("comments") or {}).get("nodes") or [])
    for c in nodes[1:]:
        login = (c.get("author") or {}).get("login")
        if not is_grug(login) and c.get("body"):
            return str(c["body"]).strip()
    return None


def _with_reraised_explanation(finding: Finding, earlier_answer: str | None) -> Finding:
    """State what changed since the earlier answer (#1457)."""
    if "changed since earlier answer" in finding.message or "changed since the earlier answer" in finding.message:
        return finding
    if earlier_answer:
        first_line = earlier_answer.splitlines()[0][:100]
        note = (
            f"Re-raised: code changed since earlier answer ({first_line!r}). "
            "The pointed-at code was modified in a later commit."
        )
    else:
        note = (
            "Re-raised: code changed since earlier answer. "
            "The pointed-at code was modified in a later commit."
        )
    return replace(finding, message=f"{finding.message}\n\n{note}")


def filter_findings_against_resolved_threads(
    findings: tuple[Finding, ...],
    installation_id: int, owner: str, repo: str, pull_number: int,
    *,
    threads: list[dict] | None = None,
    pr_author: str | None = None,
) -> tuple[Finding, ...]:
    """Remove findings that match resolved or dismissed Elder threads.

    A finding matches a thread when they share the same rule, file, and
    the finding's line is within `_NEARBY_LINES` of the thread's anchor line.
    This prevents re-raising findings that a human maintainer has already
    addressed, unless the code it points at changed (`thread.isOutdated`): then
    the finding is re-raised and its message explains what changed since the
    earlier answer (#1457).

    A thread resolved by the PR author or an App does not hide a matching
    finding on re-review (#1582). A security or SAST finding is never dropped
    by a thread resolve alone: only code changes or maintainer dismissal
    (living review) can clear it.

    If `threads` is provided, it is used instead of fetching a new listing.
    """
    if threads is None:
        threads = list_threads(installation_id, owner, repo, pull_number)
    if threads is None:
        return findings

    dismissed_unchanged: list[tuple[str, str, int]] = []
    resolved_unchanged: list[tuple[str, str, int]] = []
    resolved_outdated: list[tuple[str, str, int, str | None]] = []

    for t in threads:
        owned = _elder_thread_rule_and_line(t)
        if owned is None:
            continue
        rule, path, line = owned

        if _thread_is_dismissed(t):
            dismissed_unchanged.append((rule, path, line))
            continue

        if not _thread_resolver_counts(t, pr_author=pr_author):
            continue

        if t.get("isOutdated"):
            resolved_outdated.append((rule, path, line, _earlier_answer(t)))
        else:
            resolved_unchanged.append((rule, path, line))

    if not dismissed_unchanged and not resolved_unchanged and not resolved_outdated:
        return findings

    out: list[Finding] = []
    for f in findings:
        # A finding dismissed via living review was evaluated by a maintainer
        # as a false positive (#1187).
        if any(
            f.rule_name == rule and f.file == path and abs(f.line - line) <= _NEARBY_LINES
            for rule, path, line in dismissed_unchanged
        ):
            continue

        # A deferred-ruff finding was measured at this head, and a resolved thread
        # is not evidence of a fix (#1477).
        if f.rule_name == deferred_ruff.RULE:
            out.append(f)
            continue

        # A security or SAST finding was measured at this head; a thread resolve
        # alone is not evidence of a fix and must never hide the finding on
        # re-review (#1582). Only code changes or maintainer dismissal (living
        # review) clear it.
        if _is_security_or_sast(f):
            out.append(f)
            continue

        if any(
            f.rule_name == rule and f.file == path and abs(f.line - line) <= _NEARBY_LINES
            for rule, path, line in resolved_unchanged
        ):
            continue

        matching_outdated = [
            ans for rule, path, line, ans in resolved_outdated
            if f.rule_name == rule and f.file == path and abs(f.line - line) <= _NEARBY_LINES
        ]
        if matching_outdated:
            out.append(_with_reraised_explanation(f, matching_outdated[0]))
        else:
            out.append(f)
    return tuple(out)


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
