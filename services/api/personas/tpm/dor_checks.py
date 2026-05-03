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


@dataclass
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
_SECTION_PAT = re.compile(r"^##+\s+(.+?)\s*$", re.MULTILINE)
_SIZE_PAT = re.compile(r"\b(?:Size:?\s*)?(XS|S|M|L|XL)\b", re.IGNORECASE)
_ISSUE_LINK_PAT = re.compile(
    r"\b(?:closes|fixes|resolves|part\s+of)\s+#\d+", re.IGNORECASE,
)


def _section_text(body: str, *names: str) -> str | None:
    """Return text under the first matching ## section, or None.

    Matches case-insensitively; `names` provided in priority order.
    """
    sections = list(_SECTION_PAT.finditer(body))
    for name in names:
        for i, m in enumerate(sections):
            if m.group(1).lower().strip() == name.lower().strip():
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
    text = _section_text(body, "Acceptance criteria", "Test plan")
    if text is None:
        return CheckResult(
            "acceptance", False,
            "missing ## Acceptance criteria (or ## Test plan) section",
        )
    bullets = _BULLET_PAT.findall(text)
    if len(bullets) < 3:
        return CheckResult(
            "acceptance", False,
            f"## Acceptance criteria has {len(bullets)} non-empty bullets; need ≥3",
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
