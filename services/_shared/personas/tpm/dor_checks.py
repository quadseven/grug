"""Static DoR checks for PR bodies.

Ported from scripts/tpm.py with the bullet-count regex tightened per
quadseven/grug#20: empty `- [ ]` placeholders no longer count toward the
>=3 minimum (security: an unfilled template should NOT pass).

6 checks (per PRD #21 + memory `feedback_health_endpoint_standard`):
  why          - ## Why >=5 words
  acceptance   - ## Acceptance criteria (or ## Test plan) >=3 NON-EMPTY bullets
  estimate     - Size: XS|S|M|L|XL anywhere in body
  scope-fence  - ## Out of scope present
  issue-link   - closes #N OR Part of #N OR fixes #N
  linked-issue-completeness - linked issue(s) have all non-exempt checkboxes ticked
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from personas.tpm.ticket_compliance import strip_code_spans


log = logging.getLogger("grug.persona.tpm.dor_checks")


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    # True when the check was NOT actually evaluated and passed by
    # fail-open (#782: linked issue could not be fetched, or no fetcher
    # was wired). A skipped check never blocks - fail-open is the whole
    # point - but the rollup must not count it as a pass either: a title
    # reading "all 6 checks" over a check that never ran is a lie the
    # required_status_checks context then repeats to the merge button.
    skipped: bool = False

    def __post_init__(self) -> None:
        # Skipped implies passed: fail-open is by definition a pass that
        # was not earned. A failed-AND-skipped result has no meaning and
        # would render as both `fail` and `skipped` in the same row.
        if self.skipped and not self.passed:
            raise ValueError(
                f"CheckResult {self.name!r} incoherent: skipped=True requires passed=True",
            )


# Bullet pattern that REQUIRES non-empty content after marker (closes #20).
# Two branches: checkbox-then-content, OR no-checkbox + not-a-bracket-char.
# Matches: `- [x] foo`, `- [ ] foo`, `- foo`. Rejects: `- [ ]`, `- `, `- [x]`.
_BULLET_PAT = re.compile(
    r"^[ \t]*-[ \t]+(?:\[[ x]\][ \t]+\S|(?!\[)\S)",
    re.MULTILINE,
)
# Match `## Heading` only -- NOT `### Sub`. Earlier `##+` ate H3+
# headings as section dividers, so any H3 inside `## Acceptance
# criteria` truncated the section to empty and the bullet check failed
# on legitimate PR bodies. Closes #45.
_SECTION_PAT = re.compile(r"^##(?!#)\s+(.+?)\s*$", re.MULTILINE)
# Seer MED on PR #40 -- earlier `(?:Size:?\s*)?` made the prefix
# OPTIONAL, so a body like "use the M&Ms" would match `M` and falsely
# satisfy the estimate check. Require an explicit `Size` token followed
# by punctuation/whitespace/markdown-emphasis (`:` `**` `_` etc.) and
# then the value. Prefix `(?:^|[^A-Za-z])` stops `mySize` from matching.
_SIZE_PAT = re.compile(
    r"(?:^|[^A-Za-z])Size[:\s\*_]+(XS|S|M|L|XL)\b",
    re.IGNORECASE,
)
# Accept the closing keywords + reference keywords + bare `#N` at line
# start (the legacy gate's behavior). Codex post-review #49 -- earlier
# regex regressed valid PR bodies using `Refs #N` / `Blocked by #N`.
_ISSUE_LINK_PAT = re.compile(
    r"(?:\b(?:closes|fixes|resolves|part\s+of|refs|relates\s+to|blocked\s+by)\s+#\d+"
    r"|^\s*#\d+\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Closing keywords per GitHub's spec: close(s|d)?, fix(es|ed)?, resolve(s|d)?
# followed by #N. Case-insensitive. Captures the issue number.
# v1 scope: same-repo only (Closes owner/repo#N is out of scope per #564).
_CLOSING_KEYWORD_PAT = re.compile(
    r"\b(?:close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?)\s+#(\d+)",
    re.IGNORECASE,
)

# Exempt-section regex copied VERBATIM from infra-public's
# check.issue-close-completeness.yml (commit cd83e57). JS webhook script vs
# this Python DoR check can't literally share the constant across runtimes,
# so it is duplicated with this cross-reference comment.
#
# Matches: out of scope | blocked by | reverse-if-wrong | candidate |
#          further notes | notes$
# Applied against the nearest preceding heading or bold-label line.
_EXEMPT_SECTION_PAT = re.compile(
    r"out of scope|blocked by|reverse-if-wrong|candidate|further notes|notes$",
    re.IGNORECASE,
)

# Type alias for the issue-fetcher callable injected into
# check_linked_issue_completeness. Returns the issue body string, or
# raises on fetch failure (network/auth/404).
IssueFetcher = Callable[[int], str]


def _heading_matches(heading: str, name: str) -> bool:
    """True if a `## ` heading IS the canonical `name`, tolerating a trailing
    qualifier separated by a non-word boundary.

    So `## Out of scope (=> #828)`, `## Test plan - manual`, and `## Why this
    matters` all match their canonical names, while `## Summarytext` does NOT --
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
            f"## Why has {word_count} words; need >=5",
        )
    return CheckResult("why", True, f"## Why has {word_count} words")


def check_acceptance(body: str) -> CheckResult:
    # Track which heading name actually matched so the failure message
    # references the section the author used. external-review P2 on PR #40 --
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
            f"## {matched_name} has {len(bullets)} non-empty bullets; need >=3",
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
            "Size XL -- split into multiple PRs before review",
        )
    return CheckResult("estimate", True, f"Size: {size}")


def check_scope_fence(body: str) -> CheckResult:
    """`## Out of scope` must actually FENCE something - i.e. not be empty.

    This used to accept the heading alone, so pasting the template and leaving
    the section blank passed the gate: the same "an unfilled template should
    NOT pass" defect already fixed for bullets in #20 and enforced by
    `check_why`'s word floor. Scope-fence was the one check in this module
    still testing for presence rather than content. An empty fence is worse
    than no fence - it reads as the author having considered scope and found
    nothing to exclude.

    NON-EMPTY is the whole bar, deliberately. A first pass required 3+ words
    and the existing suite rejected it: `Deferred.` and `Not now.` are real
    answers to "what is out of scope", and a gate that demands padding gets
    padding. `## Why` earns its 5-word floor because a one-word why is never a
    why; a one-word fence often is a fence.
    """
    text = _section_text(body, "Out of scope")
    if text is None:
        return CheckResult("scope-fence", False, "missing ## Out of scope section")
    words = len(text.split())
    if not words:
        return CheckResult(
            "scope-fence", False,
            "## Out of scope is empty - name what this does NOT touch",
        )
    return CheckResult("scope-fence", True, f"## Out of scope has {words} words")


def check_issue_link(body: str) -> CheckResult:
    if _ISSUE_LINK_PAT.search(body):
        return CheckResult("issue-link", True, "issue link present")
    return CheckResult(
        "issue-link", False,
        "no `closes #N` / `fixes #N` / `Part of #N` link in body",
    )


def _scan_checklist(issue_body: str) -> tuple[list[str], int]:
    """Scan an issue body for checkbox criteria: (unchecked items, total items).

    Mirrors the algorithm in infra-public's check.issue-close-completeness.yml:
    - Heading lines (## ... or **bold**) toggle the exempt-section skip flag.
    - `- [ ]` / `* [ ]` (unchecked) and `- [x]` (checked) items under a
      non-exempt section are counted; only the unchecked ones are returned.
    - Items under an exempt heading/bold-label, and inside fenced code blocks,
      count for neither.

    `total` is what separates "every criterion is met" from "there were no
    criteria": with zero checkboxes nothing is unchecked either, and reading
    that as a pass is the vacuous-truth bug (zero of zero criteria met).

    Unchecked item texts are stripped and truncated to 100 chars.
    """
    skipping = False
    in_fence = False
    open_items: list[str] = []
    total = 0
    for raw in issue_body.split("\n"):
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Heading or bold-label line: update exempt-section skip flag.
        if re.match(r"^#{1,6}\s+", line) or re.match(r"^\*\*[^*]+\*\*\s*$", line):
            label_text = line.replace("#", "").replace("*", "").strip()
            skipping = bool(_EXEMPT_SECTION_PAT.search(label_text))
            continue
        if skipping:
            continue
        # Unchecked checkbox item.
        if re.match(r"^[-*]\s+\[\s\]\s+", line):
            item_text = re.sub(r"^[-*]\s+\[\s\]\s+", "", line)
            open_items.append(item_text[:100])
            total += 1
        elif re.match(r"^[-*]\s+\[[xX]\]\s+", line):
            total += 1
    return open_items, total


def _scan_unchecked_items(issue_body: str) -> list[str]:
    """The unchecked non-exempt items only (see `_scan_checklist`)."""
    return _scan_checklist(issue_body)[0]


def check_linked_issue_completeness(
    body: str,
    *,
    fetch_issue: IssueFetcher | None = None,
) -> CheckResult:
    """Check that linked issues (via Closes/Fixes/Resolves #N) have all
    non-exempt acceptance/test checkboxes ticked.

    This is the only check that needs IO (fetching the issue body). The fetcher
    is injected as a keyword arg so the parsing/scan logic stays unit-testable
    without a live GitHub call.

    Fetch failure (network/auth/404) fails OPEN (pass with a warning) -- a live
    GH API blip must never freeze merges repo-wide.
    """
    # Step 1: parse closing keywords from the PR body.
    #
    # Code spans stripped first: a keyword inside backticks is a MENTION, not
    # a claim, and GitHub does not close from `#N` inside code either. This is
    # the BLOCKING check, so its false positives cost the most - a PR body
    # explaining the gate by quoting `Closes #775` was blocked by the gate it
    # was documenting, having closed nothing.
    issue_numbers = [
        int(n) for n in _CLOSING_KEYWORD_PAT.findall(strip_code_spans(body))
    ]
    if not issue_numbers:
        return CheckResult(
            "linked-issue-completeness", True,
            "no linked issues (no Closes/Fixes/Resolves #N)",
        )

    # Step 2: if no fetcher is provided, fail open - and say so. Marked
    # `skipped` so the rollup title cannot count it as a pass (#782).
    if fetch_issue is None:
        log.warning(
            "linked_issue_completeness_no_fetcher",
            extra={"issue_numbers": issue_numbers},
        )
        return CheckResult(
            "linked-issue-completeness", True,
            f"linked issues #{issue_numbers} but no fetcher provided (fail-open)",
            skipped=True,
        )

    # Step 3: fetch each issue body and scan for unchecked items.
    failures: list[str] = []
    fetch_failures: list[int] = []
    no_criteria: list[int] = []
    for num in issue_numbers:
        try:
            issue_body = fetch_issue(num)
        except Exception as exc:
            # Fetch failure fails OPEN: pass with a warning.
            log.warning(
                "linked_issue_completeness_fetch_failed",
                extra={"issue_number": num, "error": str(exc)},
            )
            fetch_failures.append(num)
            continue
        open_items, total_items = _scan_checklist(issue_body)
        if open_items:
            items_str = "; ".join(open_items)
            failures.append(f"#{num}: {items_str}")
        elif total_items == 0:
            no_criteria.append(num)

    if failures:
        detail = "linked issue(s) have unchecked items: " + "; ".join(failures)
        return CheckResult("linked-issue-completeness", False, detail)

    # No failures found. An issue with NO checkbox criteria was not evaluated
    # either: nothing was unchecked because nothing was there to check, and
    # "all checkboxes ticked" over zero checkboxes is a lie. Pass
    # (blocking every PR that closes a prose issue is a policy change, not a
    # bug fix) but mark it `skipped`, so the rollup names it and drops it from
    # the "all N checks" claim.
    if no_criteria:
        checked = [n for n in issue_numbers if n not in no_criteria and n not in fetch_failures]
        detail = (
            f"linked issue(s) #{no_criteria} have no acceptance criteria to check "
            "(no checkboxes); nothing was verified"
        )
        if checked:
            detail += f"; #{checked} all checkboxes ticked"
        if fetch_failures:
            detail += f"; fetch failed for #{fetch_failures} (fail-open)"
        return CheckResult("linked-issue-completeness", True, detail, skipped=True)

    # If any fetches failed, we fail open (pass) but
    # mark the check `skipped`: a partly-fetched issue list was not fully
    # evaluated, and the rollup must not count it toward "all N checks".
    if fetch_failures:
        return CheckResult(
            "linked-issue-completeness", True,
            f"linked issues #{issue_numbers} checked; "
            f"fetch failed for #{fetch_failures} (fail-open)",
            skipped=True,
        )

    return CheckResult(
        "linked-issue-completeness", True,
        f"linked issue(s) #{issue_numbers} all checkboxes ticked",
    )


ALL_CHECKS = (
    check_why,
    check_acceptance,
    check_estimate,
    check_scope_fence,
    check_issue_link,
    check_linked_issue_completeness,
)


def run_all(
    body: str,
    *,
    fetch_issue: IssueFetcher | None = None,
) -> list[CheckResult]:
    """Run all DoR checks over the PR body.

    `fetch_issue` is passed through to check_linked_issue_completeness.
    When None, that check fails open (pass).
    """
    # The first 5 checks are pure (body -> CheckResult).
    # The last check (check_linked_issue_completeness) needs IO via fetch_issue.
    pure_checks = ALL_CHECKS[:-1]
    io_check = ALL_CHECKS[-1]  # check_linked_issue_completeness
    results = [check(body) for check in pure_checks]
    results.append(io_check(body, fetch_issue=fetch_issue))
    return results
