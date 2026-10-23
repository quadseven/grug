"""Deterministic check for a health fix that changes the measurement, not the cause (#1370).

A health finding reported a guild bank holding under a gold while its
members held under a gold each. The lane PR on that ticket changed only
the health endpoint: it added gold a second account carried to the
measured balance, so the finding went away while the members stayed
broke. Elder passed it.

One high `symptom-only` finding when the PR answers a health ticket and
every file of code it changes is measurement code: a health endpoint or
check, a metric, a monitor, a probe or an alert (a path segment says so).
Tests and docs do not count either way. A ticket whose change criteria
ask for the measurement itself (they name a health endpoint or check, a
metric, a monitor, a probe, an alert, a threshold or a query, or a changed
file) is not held to this: there the measurement is the work.

A health ticket is one `linked_issue` rendered as health evidence (its
title starts `Health:` or its body carries the health-intake marker),
passed in as `health_issues`, or, when that is not known, a PR title that
starts `Health:` (a lane PR copies its ticket's title).

Not checked here (left to the reviewer's `symptom-only` rule, which reads
the finding's evidence): a fix in the acting code that only moves the
number, such as a reserve held back so a balance never reads low, or a
guard widened until a test passes. Telling those from a real fix needs
what the finding measures, which a path does not show.

Pure: file changes, hunks and PR text in; findings out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import (
    DiffHunk,
    FileChange,
    is_review_excluded_path,
)
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

RULE = "symptom-only"
# Past this many changed files the check is not evaluated (no finding).
MAX_FILES = 3000

_MEASUREMENT_WORDS = frozenset({
    "health", "healthcheck", "healthchecks", "healthz", "metric", "metrics",
    "monitor", "monitors", "monitoring", "probe", "probes", "alert", "alerts",
    "alerting", "slo", "slos",
})
_PROSE_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_TEST_NAME_RE = re.compile(r"(\.test\.|\.spec\.|Test\.\w+$|_test\.\w+$)")
_WORD_SPLIT_RE = re.compile(r"[/_.\-]+")
_HEALTH_TITLE_RE = re.compile(r"^\s*health\s*:", re.IGNORECASE)
_LIVE_RE = re.compile(r"^\W*(live\b|live proof|verified\b)", re.IGNORECASE)
# Criteria that ask for the measurement itself.
_ASKS_FOR_MEASUREMENT_RE = re.compile(
    r"\bhealth\s+(endpoint|check|read|report|finding|rule)s?\b|\bmetrics?\b|\bmonitor"
    r"|\bprobes?\b|\balerts?\b|\bthresholds?\b|\bquer(y|ies)\b|\bdashboards?\b",
    re.IGNORECASE,
)


def is_measurement_path(path: str) -> bool:
    """A path segment or word says the file measures or reports health."""
    return any(w in _MEASUREMENT_WORDS for w in _WORD_SPLIT_RE.split(path.lower()))


def _counts_as_code(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return not (
        is_test_file(path)
        or _TEST_NAME_RE.search(name)
        or path.lower().endswith(_PROSE_SUFFIXES)
        or is_review_excluded_path(path)
    )


def _asks_for_measurement(texts: Iterable[str], files: Iterable[str]) -> bool:
    # A file's path or basename names it; so does a stem that is not itself
    # a measurement word (`health` alone is every health ticket's title).
    names = {f.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() for f in files}
    stems = {s for s in names if len(s) >= 4 and s not in _MEASUREMENT_WORDS}
    stems |= {f.lower() for f in files} | {f.rsplit("/", 1)[-1].lower() for f in files}
    for text in texts:
        if _LIVE_RE.search(text):
            continue
        if _ASKS_FOR_MEASUREMENT_RE.search(text):
            return True
        lowered = text.lower()
        if any(re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", lowered) for s in stems):
            return True
    return False


def _anchor(path: str, hunks: Iterable[DiffHunk]) -> tuple[str, int] | None:
    for hunk in hunks:
        if hunk.file_path == path and hunk.new_lines:
            return path, min(hunk.new_lines)
    return None


def _answers_health(
    criteria: tuple[LinkedCriterion, ...], pr_title: str, health: set[int],
) -> bool:
    """A linked ticket is health evidence, or the PR title says so."""
    in_scope = {c.issue for c in criteria}
    return bool(health & in_scope or _HEALTH_TITLE_RE.search(pr_title or ""))


def _measurement_code(changes: Iterable[FileChange]) -> list[str]:
    """The changed code files when every one is measurement code, else []."""
    changes = tuple(changes)
    if len(changes) > MAX_FILES:
        return []
    code = [c.path for c in changes if c.status != "removed" and _counts_as_code(c.path)]
    if not all(is_measurement_path(p) for p in code):
        return []
    return code


def _measurement_finding(
    anchor: tuple[str, int], issues: list[int], code: list[str],
) -> Finding:
    refs = ", ".join(f"#{n}" for n in issues)
    files = ", ".join(f"`{p}`" for p in code[:4])
    return Finding(
        file=anchor[0],
        line=anchor[1],
        severity="high",
        rule_name=RULE,
        message=(
            f"This PR answers a health finding ({refs}), and the only code it changes "
            f"is the code that measures or reports it ({files}). Changing how the "
            "finding is computed makes it go quiet without changing what it measured. "
            "Fix the code that acts on the problem, or show in the ticket that the "
            "measurement itself was wrong."
        ),
        suggestion=None,
        effort="heavy-lift",
    )


def scan_measurement_only(
    changes: Iterable[FileChange],
    hunks: tuple[DiffHunk, ...],
    *,
    criteria: tuple[LinkedCriterion, ...],
    pr_title: str,
    health_issues: Iterable[int] = (),
) -> tuple[Finding, ...]:
    """One finding when a health ticket's fix changes only measurement code."""
    if not criteria:
        return ()
    health = set(health_issues)
    if not _answers_health(criteria, pr_title, health):
        return ()
    code = _measurement_code(changes)
    if not code:
        return ()
    title = _HEALTH_TITLE_RE.sub("", pr_title or "")
    if _asks_for_measurement([title, *(c.text for c in criteria)], code):
        return ()
    anchor = next((a for a in (_anchor(p, hunks) for p in code) if a), None)
    if anchor is None:
        return ()
    in_scope = {c.issue for c in criteria}
    issues = sorted(health & in_scope) or sorted(in_scope)
    return (_measurement_finding(anchor, issues, code),)
