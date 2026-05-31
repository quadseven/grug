# MIRRORED — sibling at services/api/personas/code_reviewer/dedup.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Finding deduplication across `synchronize` pushes (#189).

When a developer pushes new commits to an open PR, the Elder persona
re-reviews the whole diff and would re-post every finding — flooding
the PR with duplicate inline comments on lines that haven't changed.
This module dedups: a finding already commented on an UNCHANGED line is
skipped; a finding on a moved/changed line (different line number) is
treated as NEW and re-posted.

The dedup key is (file, line, rule_name) — exactly the AC's
(file, line_range, rule_name). Two consequences fall out of including
`line` in the key, both desired:
  - moved-line detection: a finding whose line shifted has a different
    key → not matched → re-posted as new.
  - same-line-different-rule: two distinct rules can both comment one
    line; only the exact (line, rule) pair dedups.

Prior Grug findings are identified by a hidden HTML-comment marker
appended to each inline-comment body (`rule_marker`). Parsing our OWN
marker — not scraping human-readable markdown — keeps the rule
extraction unambiguous and robust to body-format changes. A PR comment
without the marker is a human comment and contributes no key.
"""
from __future__ import annotations

import re

from personas.code_reviewer.persona import Finding

# Hidden machine-readable marker carrying the rule name. GitHub renders
# HTML comments invisibly, so it doesn't clutter the developer's view.
_MARKER_RE = re.compile(r"<!--\s*grug-rule:([A-Za-z0-9_-]+)\s*-->")


def rule_marker(rule_name: str) -> str:
    """The hidden marker to append to an inline-comment body so a later
    synchronize can recognise this comment as a Grug finding for `rule`."""
    return f"<!-- grug-rule:{rule_name} -->"


def parse_rule(body: str) -> str | None:
    """Extract the rule name from a comment body's marker, or None if
    the comment carries no Grug marker (i.e. a human comment)."""
    m = _MARKER_RE.search(body or "")
    return m.group(1) if m else None


def finding_key(file: str, line: int, rule: str) -> str:
    """Canonical dedup key. `rule@file:line` — stable, human-readable
    in logs, and unique per (file, line, rule) triple."""
    return f"{rule}@{file}:{line}"


def prior_keys_from_comments(comments: list[dict]) -> set[str]:
    """Build the set of finding-keys already posted, from the PR's
    review comments. Each comment dict carries `path`, `line`, `body`
    (the GitHub PR-review-comment shape). Comments without a Grug
    marker, or without a usable `line` (file-level / outdated comments
    report `line: null`), contribute nothing."""
    keys: set[str] = set()
    for c in comments:
        line = c.get("line")
        if line is None:
            continue
        rule = parse_rule(c.get("body", ""))
        if rule is None:
            continue
        keys.add(finding_key(c["path"], int(line), rule))
    return keys


def dedup_findings(
    findings: tuple[Finding, ...], prior_keys: set[str],
) -> tuple[Finding, ...]:
    """Drop findings whose (file, line, rule) key is already present in
    `prior_keys` (a Grug comment exists on that exact line for that
    rule). Returns the findings to post as NEW inline comments."""
    return tuple(
        f for f in findings
        if finding_key(f.file, f.line, f.rule_name) not in prior_keys
    )
