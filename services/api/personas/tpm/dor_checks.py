# MIRRORED — sibling at services/webhook/personas/tpm/dor_checks.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Static DoR checks for PR bodies.

Ported from scripts/tpm.py with the bullet-count regex tightened per
githumps/grug#20: empty `- [ ]` placeholders no longer count toward the
≥3 minimum (security: an unfilled template should NOT pass).

5 checks (per PRD #21 + memory `feedback_health_endpoint_standard`):
  why          — ## Why ≥5 words
  acceptance   — ## Acceptance criteria (or ## Test plan) ≥3 NON-EMPTY bullets
  estimate     — Size: XS|S|M|L|XL anywhere in body
  scope-fence  — ## Out of scope present
  issue-link   — closes #N OR Part of #N OR fixes #N
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


# Bullet pattern that REQUIRES non-empty content after marker (closes #20).
# Two branches: checkbox-then-content, OR no-checkbox + not-a-bracket-char.
# Matches: `- [x] foo`, `- [ ] foo`, `- foo`. Rejects: `- [ ]`, `- `, `- [x]`.
_BULLET_PAT = re.compile(
    r"^[ \t]*-[ \t]+(?:\[[ x]\][ \t]+\S|(?!\[)\S)",
    re.MULTILINE,
)
# Match `## Heading` only — NOT `### Sub`. Earlier `##+` ate H3+
# headings as section dividers, so any H3 inside `## Acceptance
# criteria` truncated the section to empty and the bullet check failed
# on legitimate PR bodies. Closes #45.
_SECTION_PAT = re.compile(r"^##(?!#)\s+(.+?)\s*$", re.MULTILINE)
# Sentry MED on PR #40 — earlier `(?:Size:?\s*)?` made the prefix
# OPTIONAL, so a body like "use the M&Ms" would match `M` and falsely
# satisfy the estimate check. Require an explicit `Size` token followed
# by punctuation/whitespace/markdown-emphasis (`:` `**` `_` etc.) and
# then the value. Prefix `(?:^|[^A-Za-z])` stops `mySize` from matching.
_SIZE_PAT = re.compile(
    r"(?:^|[^A-Za-z])Size[:\s\*_]+(XS|S|M|L|XL)\b",
    re.IGNORECASE,
)
# Accept the closing keywords + reference keywords + bare `#N` at line
# start (the legacy gate's behavior). Codex post-review #49 — earlier
# regex regressed valid PR bodies using `Refs #N` / `Blocked by #N`.
_ISSUE_LINK_PAT = re.compile(
    r"(?:"
    r"\b(?:closes|fixes|resolves|part\s+of|refs|relates\s+to|blocked\s+by)\s+#\d+"
    r"|^\s*#\d+\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def _heading_matches(heading: str, name: str) -> bool:
    """True if a `## ` heading IS the canonical `name`, tolerating a trailing
    qualifier separated by a non-word boundary.

    So `## Out of scope (→ #828)`, `## Test plan — manual`, and `## Why this
    matters` all match their canonical names, while `## Summarytext` does NOT —
    the char right after the name must be non-alphanumeric. This replaces the
    brittle exact-equality match that failed otherwise-correct PRs whose authors
    appended a parenthetical/qualifier to a required header.
    """
    h = heading.lower().strip()
    n = name.lower().strip()
    if h == n:
        return True
    return h.startswith(n) and len(h) > len(n) and not h[len(n)].isalnum()


def _section_text(body: str, *names: str) -> str | None:
    """Return text under the first matching ## section, or None.

    Matches case-insensitively and tolerates a trailing qualifier on the heading
    (see `_heading_matches`); `names` provided in priority order.
    """
    sections = list(_SECTION_PAT.finditer(body))
    for name in names:
        for i, m in enumerate(sections):
            if _heading_matches(m.group(1), name):
                start = m.end()
                end = sections[i + 1].start() if i + 1 < len(sections) else len(body)
                return body[start:end].strip()
    return None


def check_why(body: str) -> CheckResult:
    text = _section_text(body, "Why", "Summary")
    if text is None:
        return CheckResult("why", False, "missing ## Why (or ## Summary) section")
    word_count = len(text.split())
    if word_count < 5:
        return CheckResult(
            "why", False,
            f"## Why has {word_count} words; need ≥5",
        )
    return CheckResult("why", True, f"## Why has {word_count} words")


def check_acceptance(body: str) -> CheckResult:
    # Track which heading name actually matched so the failure message
    # references the section the author used. external-review P2 on PR #40 —
    # earlier code always said "Acceptance criteria" even when only
    # "Test plan" was present, sending users hunting for a section that
    # doesn't exist.
    matched_name = "Acceptance criteria"
    text = _section_text(body, "Acceptance criteria")
    if text is None:
        text = _section_text(body, "Test plan")
        matched_name = "Test plan"
    if text is None:
        return CheckResult(
            "acceptance", False,
            "missing ## Acceptance criteria (or ## Test plan) section",
        )
    bullets = _BULLET_PAT.findall(text)
    if len(bullets) < 3:
        return CheckResult(
            "acceptance", False,
            f"## {matched_name} has {len(bullets)} non-empty bullets; need ≥3",
        )
    return CheckResult("acceptance", True, f"{len(bullets)} bullets")


def check_estimate(body: str) -> CheckResult:
    m = _SIZE_PAT.search(body)
    if not m:
        return CheckResult(
            "estimate", False, "no Size: XS/S/M/L/XL in body",
        )
    size = m.group(1).upper()
    if size == "XL":
        return CheckResult(
            "estimate", False,
            "Size XL — split into multiple PRs before review",
        )
    return CheckResult("estimate", True, f"Size: {size}")


def check_scope_fence(body: str) -> CheckResult:
    if _section_text(body, "Out of scope") is not None:
        return CheckResult("scope-fence", True, "## Out of scope present")
    return CheckResult("scope-fence", False, "missing ## Out of scope section")


def check_issue_link(body: str) -> CheckResult:
    if _ISSUE_LINK_PAT.search(body):
        return CheckResult("issue-link", True, "issue link present")
    return CheckResult(
        "issue-link", False,
        "no `closes #N` / `fixes #N` / `Part of #N` link in body",
    )


ALL_CHECKS = (check_why, check_acceptance, check_estimate, check_scope_fence, check_issue_link)


def run_all(body: str) -> list[CheckResult]:
    return [check(body) for check in ALL_CHECKS]
