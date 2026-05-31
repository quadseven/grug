# MIRRORED — sibling at services/webhook/personas/code_reviewer/dedup.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
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
appended to each inline-comment body (`rule_marker`). The marker is
invisible in GitHub's rendered web view (so it doesn't clutter the
developer's PR) AND preserved verbatim in the REST `body` field — which
is the surface dedup actually reads (`_fetch_pr_review_comments`).
Parsing our OWN marker — not scraping human-readable markdown — keeps
the rule extraction unambiguous and robust to body-format changes. A PR
comment without the marker is a human comment and contributes no key.
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
    the comment carries no Grug marker (i.e. a human comment).

    Returns the LAST marker match: `_inline_comment_body` always appends
    the real marker at the very end, so if a finding's message text
    happens to quote a literal `<!-- grug-rule:X -->` (e.g. Elder
    flagging a prior comment), the trailing real marker still wins."""
    matches = _MARKER_RE.findall(body or "")
    return matches[-1] if matches else None


def finding_key(file: str, line: int, rule: str) -> str:
    """Canonical dedup key. `rule@file:line` — stable, human-readable
    in logs, unique per (file, line, rule) triple, only ever used for
    set membership (never re-parsed, so a `:` in a path is harmless).

    Both sides build the key via THIS function, so they agree as long as
    `rule` agrees. The prior side gets `rule` from the marker regex
    (`[A-Za-z0-9_-]+`); `code_review_prompt.ReviewRule` enforces the
    same charset on every real rule name, so they match. A hallucinated
    LLM rule outside that charset would mismatch — which fails SAFE
    (posts a duplicate comment, never skips a real finding)."""
    return f"{rule}@{file}:{line}"


def prior_keys_from_comments(comments: list[dict]) -> set[str]:
    """Build the set of finding-keys already posted, from the PR's
    review comments. Each comment dict carries `path`, `line`, `side`,
    `body` (the GitHub PR-review-comment shape). Comments are skipped
    when they: lack a Grug marker; lack a usable `line` (file-level /
    outdated comments report `line: null`); are LEFT-side (Grug only
    ever posts RIGHT-side new-file comments, so a LEFT comment with a
    coincidental marker is not ours); or carry a non-numeric `line`
    (malformed payload). Every skip biases toward a SMALLER key set →
    post-extra, never skip-a-real-finding."""
    keys: set[str] = set()
    for c in comments:
        line = c.get("line")
        path = c.get("path")
        if line is None or path is None:
            continue
        # Grug posts RIGHT-side (new-file) comments; `side` defaults to
        # RIGHT when absent. A LEFT comment can't be one of ours.
        if c.get("side", "RIGHT") == "LEFT":
            continue
        rule = parse_rule(c.get("body", ""))
        if rule is None:
            continue
        try:
            line_int = int(line)
        except (TypeError, ValueError):
            # Malformed comment dict — don't let it escape best-effort
            # dedup (the caller only catches httpx errors).
            continue
        keys.add(finding_key(path, line_int, rule))
    return keys


def dedup_findings(
    findings: tuple[Finding, ...], prior_keys: frozenset[str] | set[str],
) -> tuple[Finding, ...]:
    """Drop findings whose (file, line, rule) key is already present in
    `prior_keys` (a Grug comment exists on that exact line for that
    rule). Returns the findings to post as NEW inline comments."""
    return tuple(
        f for f in findings
        if finding_key(f.file, f.line, f.rule_name) not in prior_keys
    )
