"""Static DoR checks for PR bodies.

Ported from scripts/tpm.py with the bullet-count regex tightened per
quadseven/grug#20: empty `- [ ]` placeholders no longer count toward the
>=3 minimum (security: an unfilled template should NOT pass). Text inside
HTML comments counts for nothing either (#1330): a template's guidance and
example bullets live in comments the rendered PR never shows.

9 checks (per PRD #21 + memory `feedback_health_endpoint_standard`):
  why          - ## Why >=5 words
  acceptance   - ## Acceptance criteria (or ## Test plan) >=3 NON-EMPTY bullets
  estimate     - Size: XS|S|M|L|XL anywhere in body
  scope-fence  - ## Out of scope present
  issue-link   - Refs #N / Refs owner/name#N / Part of #N / closes #N ... (advisory)
  loom-footer  - a Loom-ticket PR carries the Loom audit footer (advisory)
  linked-issue-completeness - linked issue(s) have all non-exempt checkboxes ticked
  linked-issue-epic - every ticket the PR names is an epic or belongs to one (grug#1034)
  in-scope     - all changed files trace to the linked issue's scope (advisory)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from personas.tpm.ticket_compliance import strip_code_spans
from personas.tpm.in_scope import check_in_scope
from personas.tpm.sibling_evidence import SiblingFetcher, delivered_criteria


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
# An HTML comment, or an unclosed one running to the end of the body: GitHub
# hides both on the rendered PR, so neither is content the author wrote (#1330).
# Code is matched first and kept: a `<!--` quoted in backticks or a fence is
# literal text, and an unclosed one there must not swallow the rest of the body.
_HTML_COMMENT_PAT = re.compile(
    r"(?P<code>```.*?(?:```|\Z)|`[^`\n]+`)|<!--.*?(?:-->|\Z)", re.DOTALL,
)
# A line that is only a list marker, optionally with an empty or ticked box:
# `-`, `*`, `- [ ]`, `- [x]`. The template's placeholder, not a word of text.
_PLACEHOLDER_LINE_PAT = re.compile(r"^[ \t]*[-*+][ \t]*(?:\[[ xX]\][ \t]*)?$", re.MULTILINE)
# A line holding only the size label (`**Size:** S`, or bare `Size:` once its
# comment is gone). Templates put it after the last section, so it lands in
# `## Out of scope`; it is the estimate, not a fence.
_SIZE_LINE_PAT = re.compile(
    r"^[ \t]*[*_]*Size[*_]*:?[*_]*[ \t]*(?:XS|S|M|L|XL)?[*_]*[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
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
# #1214: a keyword may also name the ticket as `owner/name#N` - lane PRs
# write `Refs owner/name#N`, and some operators never use a closing
# keyword because a ticket closes only after its boxes are ticked.
_ISSUE_LINK_PAT = re.compile(
    r"(?:\b(?:closes|fixes|resolves|part\s+of|refs|relates\s+to|blocked\s+by)\s+"
    r"(?:[A-Za-z0-9][\w.-]*/[\w.-]+)?#\d+"
    r"|^\s*#\d+\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
# A Loom factory PR names its ticket, lane, and model in an audit footer so
# the dashboard can attribute the work later. Advisory only: the footer is
# an audit nicety, never a merge gate.
_LOOM_FOOTER_PAT = re.compile(r"^Loom ticket ", re.MULTILINE)
# The lane the footer names (`Loom ticket #N, lane <name>, model ...`).
# Chief reads it to tell an operator session (lane `operator-session`)
# from a fleet lane (#1318).
_LOOM_FOOTER_LANE_PAT = re.compile(
    r"^Loom ticket [^\n]*?,\s*lane\s+([\w.-]+)", re.MULTILINE,
)
# The model the footer names (`..., model <name>`). Elder reads it to pick a
# reviewer of another model family (#1437).
_LOOM_FOOTER_MODEL_PAT = re.compile(
    r"^Loom ticket [^\n]*?,\s*model\s+([\w.:/-]+)", re.MULTILINE,
)
_LOOM_TICKET_REF_PAT = re.compile(
    r"\b(?:refs|part\s+of|fixes|closes)\s+quadseven/loom#\d+",
    re.IGNORECASE,
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


def strip_html_comments(body: str) -> str:
    """`body` without its HTML comments (#1330).

    The Loom footer and agent-marker checks read the RAW body on purpose:
    their markers live in comments. Only the content checks strip them.
    """
    return _HTML_COMMENT_PAT.sub(lambda m: m.group("code") or "", body)


def _content_word_count(text: str) -> int:
    """Words in `text`, not counting placeholder-only lines (`-`, `- [ ]`)
    or a size-label line."""
    text = _SIZE_LINE_PAT.sub("", text)
    return len(_PLACEHOLDER_LINE_PAT.sub("", text).split())


def section_text(body: str, *names: str) -> str | None:
    """Return text under the first matching ## section, or None.

    Matches case-insensitively and tolerates a trailing qualifier on the heading
    (see `_heading_matches`); `names` provided in priority order. HTML comments
    are stripped first, so a `## Heading` inside one is no section and the
    guidance inside one is no content (#1330).
    """
    body = strip_html_comments(body)
    sections = list(_SECTION_PAT.finditer(body))
    for name in names:
        for i, m in enumerate(sections):
            if _heading_matches(m.group(1), name):
                start = m.end()
                end = sections[i + 1].start() if i + 1 < len(sections) else len(body)
                return body[start:end].strip()
    return None


def check_why(body: str) -> CheckResult:
    text = section_text(body, "Why", "Summary")
    if text is None:
        return CheckResult("why", False, "missing ## Why (or ## Summary) section")
    word_count = _content_word_count(text)
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
    text = section_text(body, "Acceptance criteria")
    if text is None:
        text = section_text(body, "Test plan")
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
    m = _SIZE_PAT.search(strip_html_comments(body))
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
    why; a one-word fence often is a fence. A bare `-` placeholder is no word
    at all (#1330).
    """
    text = section_text(body, "Out of scope")
    if text is None:
        return CheckResult("scope-fence", False, "missing ## Out of scope section")
    words = _content_word_count(text)
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
        "no issue link in body - any of these counts: `Refs #N`, "
        "`Refs owner/name#N`, `Part of #N`, `Relates to #N`, `Blocked by #N`, "
        "`closes #N` / `fixes #N` / `resolves #N`, or a bare `#N` line",
    )


def has_loom_footer(body: str) -> bool:
    """True when the body carries the Loom audit footer (`Loom ticket ...`
    at the start of a line). Chief's persona uses the same predicate to tell
    a fleet lane PR from an operator PR (grug#1251), so the two cannot
    drift."""
    return bool(_LOOM_FOOTER_PAT.search(body or ""))


def loom_footer_lane(body: str) -> str | None:
    """The lane the Loom audit footer names, or None when there is no
    footer or it names no lane. Lives next to `has_loom_footer` so Chief's
    persona and the footer check share one parser (#1318)."""
    match = _LOOM_FOOTER_LANE_PAT.search(body or "")
    # A sentence-final footer (`..., lane x.`) keeps its dot off the name.
    return match.group(1).rstrip(".") if match else None


def loom_footer_model(body: str) -> str | None:
    """The model the Loom audit footer names, or None when there is no footer
    or it names no model. Same parser home as `loom_footer_lane`."""
    match = _LOOM_FOOTER_MODEL_PAT.search(body or "")
    return match.group(1).rstrip(".") if match else None


def check_loom_footer(body: str) -> CheckResult:
    if has_loom_footer(body):
        return CheckResult("loom-footer", True, "Loom audit footer present")
    if _LOOM_TICKET_REF_PAT.search(body):
        return CheckResult(
            "loom-footer", False,
            "references a quadseven/loom ticket but has no Loom audit footer "
            "(`Loom ticket ..., lane ..., model ...`) - the dashboard cannot "
            "attribute this PR to a model",
        )
    # Not skipped: the check DID run and found nothing to require. `skipped`
    # means "could not be evaluated" (#782), and marking every non-Loom PR
    # skipped would strip "all N checks" from nearly every Chief title.
    return CheckResult(
        "loom-footer", True, "not a Loom ticket PR; footer not required",
    )


def _scan_checklist(
    issue_body: str, *, max_chars: int | None = 100,
) -> tuple[list[str], int]:
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

    Unchecked item texts are stripped and truncated to `max_chars`.
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
            open_items.append(item_text[:max_chars])
            total += 1
        elif re.match(r"^[-*]\s+\[[xX]\]\s+", line):
            total += 1
    return open_items, total


def _scan_unchecked_items(issue_body: str) -> list[str]:
    """The unchecked non-exempt items only (see `_scan_checklist`)."""
    return _scan_checklist(issue_body)[0]


def _met_by_siblings(
    number: int, open_items: list[str], issue_body: str,
    fetch_siblings: SiblingFetcher | None,
) -> dict[str, int]:
    """The open items a merged sibling PR delivered. A failed read is no
    evidence, which leaves the plain check (grug#1530)."""
    if fetch_siblings is None:
        return {}
    try:
        siblings = fetch_siblings(number)
    except Exception as exc:  # noqa: BLE001 - evidence must not change the check's fail-open
        log.warning(
            "linked_issue_siblings_fetch_failed",
            extra={"issue_number": number, "kind": type(exc).__name__},
        )
        return {}
    return delivered_criteria(open_items, siblings, issue_body)


def check_linked_issue_completeness(
    body: str,
    *,
    fetch_issue: IssueFetcher | None = None,
    fetch_siblings: SiblingFetcher | None = None,
) -> CheckResult:
    """Check that linked issues (via Closes/Fixes/Resolves #N) have all
    non-exempt acceptance/test checkboxes ticked, or the box's criterion
    delivered by a merged PR on the same ticket (`sibling_evidence`).

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
    delivered: list[str] = []
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
        open_items, total_items = _scan_checklist(issue_body, max_chars=None)
        met = _met_by_siblings(num, open_items, issue_body, fetch_siblings)
        if met:
            prs = sorted(set(met.values()))
            log.info(
                "linked_issue_criteria_met_by_siblings",
                extra={"issue_number": num, "criteria": len(met), "prs": prs},
            )
            delivered.append(f"#{num}: {len(met)} by {', '.join(f'#{p}' for p in prs)}")
        still_open = [item[:100] for item in open_items if item not in met]
        if still_open:
            items_str = "; ".join(still_open)
            failures.append(f"#{num}: {items_str}")
        elif total_items == 0:
            no_criteria.append(num)

    if failures:
        detail = "linked issue(s) have unchecked items: " + "; ".join(failures)
        return CheckResult("linked-issue-completeness", False, detail)

    sibling_note = (
        f"; criteria met by merged PRs ({'; '.join(delivered)})" if delivered else ""
    )

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
        return CheckResult(
            "linked-issue-completeness", True, detail + sibling_note, skipped=True,
        )

    # If any fetches failed, we fail open (pass) but
    # mark the check `skipped`: a partly-fetched issue list was not fully
    # evaluated, and the rollup must not count it toward "all N checks".
    if fetch_failures:
        return CheckResult(
            "linked-issue-completeness", True,
            f"linked issues #{issue_numbers} checked; "
            f"fetch failed for #{fetch_failures} (fail-open){sibling_note}",
            skipped=True,
        )

    return CheckResult(
        "linked-issue-completeness", True,
        f"linked issue(s) #{issue_numbers} all checkboxes ticked{sibling_note}",
    )


@dataclass(frozen=True)
class IssueFacts:
    """What `check_linked_issue_in_epic` needs to know about one issue."""

    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    sub_issue_count: int
    parent_number: int | None
    is_pull_request: bool = False


IssueFactsFetcher = Callable[[int], IssueFacts]

# The tickets a PR names: closing keywords plus the two reference forms that
# point at the work item rather than merely something nearby. `Blocked by` and
# `Relates to` are deliberately left out: they name OTHER work, and requiring an
# epic of a blocker would make one orphan block every PR that mentions it.
_TICKET_REF_PAT = re.compile(
    r"\b(?:close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?|refs|part\s+of)\s+#(\d+)",
    re.IGNORECASE,
)

# The body forms of epic membership that the hunt collector already reads
# (infra production/scripts/backlog-emitter/collect_backlog.py): a line reading
# `Part of #N` or `Parent #N`, optionally as a list item, or a `## Parent`
# heading. A native sub-issue link is the other form and is read separately.
_BODY_PARENT_PAT = re.compile(
    r"^\s*(?:[-*]\s+)?(?:part\s+of|parent:?)\s+(?:[\w.-]+/[\w.-]+)?#\d+"
    r"|^\s*##\s*parent\b",
    re.IGNORECASE | re.MULTILINE,
)


def is_epic(facts: IssueFacts) -> bool:
    """An epic is a root: labelled `epic`, titled `Epic: ...`, or already a
    parent. Repos differ on which of these they use (grug marks epics by title
    and has no `epic` label), so any one is enough."""
    return (
        any(label.lower() == "epic" for label in facts.labels)
        or facts.title.strip().lower().startswith("epic:")
        or facts.sub_issue_count > 0
    )


def belongs_to_epic(facts: IssueFacts) -> bool:
    return facts.parent_number is not None or bool(
        _BODY_PARENT_PAT.search(strip_code_spans(facts.body))
    )


def named_ticket_numbers(body: str) -> list[int]:
    """The ticket numbers a PR body names, sorted and de-duplicated.

    The one extractor behind `which epic` and the webhook trigger that re-runs
    Chief when a named ticket gains or loses an epic (dispatcher
    `_recheck_chief_for_ticket`). Shared so the trigger and the check can
    never disagree on which PRs a ticket change affects. Code spans are
    stripped first: a `Refs #N` quoted in backticks names nothing.
    """
    return sorted({int(n) for n in _TICKET_REF_PAT.findall(strip_code_spans(body or ""))})


def check_linked_issue_in_epic(
    body: str,
    *,
    fetch_issue_facts: IssueFactsFetcher | None = None,
) -> CheckResult:
    """Every ticket the PR names must be an epic or belong to one (grug#1034).

    BLOCKING, by the operator's choice on 2026-09-22: every quadseven repo
    writes the rule down and nothing enforced it, and a triage that day found
    ~230 orphan tickets across 18 repos. The fix is one click (add the ticket
    as a sub-issue of an epic) or one line (`Part of #N` in its body).

    Fails OPEN on a fetch failure and marks itself `skipped`, exactly like
    `ticket done` (#782): a GitHub blip must never freeze merges, and must
    never read as a pass either. A PR naming no ticket passes here with
    nothing to check - `which ticket` already speaks to that.
    """
    numbers = named_ticket_numbers(body)
    if not numbers:
        return CheckResult(
            "linked-issue-epic", True, "no ticket named, so no epic to check",
        )
    if fetch_issue_facts is None:
        return CheckResult(
            "linked-issue-epic", True,
            f"tickets #{numbers} but no fetcher provided (fail-open)",
            skipped=True,
        )

    orphans: list[int] = []
    fetch_failures: list[int] = []
    for num in numbers:
        try:
            facts = fetch_issue_facts(num)
        except Exception as exc:
            log.warning(
                "linked_issue_epic_fetch_failed",
                extra={"issue_number": num, "error": str(exc)},
            )
            fetch_failures.append(num)
            continue
        if facts.is_pull_request or is_epic(facts) or belongs_to_epic(facts):
            continue
        orphans.append(num)

    if orphans:
        named = ", ".join(f"#{n}" for n in orphans)
        return CheckResult(
            "linked-issue-epic", False,
            f"{named} belong to no epic - add each as a sub-issue of an epic, "
            "or write `Part of #<epic>` in its body",
        )
    if fetch_failures:
        return CheckResult(
            "linked-issue-epic", True,
            f"tickets #{numbers} checked; fetch failed for #{fetch_failures} (fail-open)",
            skipped=True,
        )
    return CheckResult(
        "linked-issue-epic", True, f"ticket(s) #{numbers} each belong to an epic",
    )


_PURE_CHECKS = (
    check_why,
    check_acceptance,
    check_estimate,
    check_scope_fence,
    check_issue_link,
    check_loom_footer,
)

ALL_CHECKS = (
    *_PURE_CHECKS,
    check_linked_issue_completeness,
    check_linked_issue_in_epic,
)


def run_all(
    body: str,
    *,
    fetch_issue: IssueFetcher | None = None,
    fetch_issue_facts: IssueFactsFetcher | None = None,
    fetch_changed_files: Callable[[], list[str]] | None = None,
    fetch_siblings: SiblingFetcher | None = None,
) -> list[CheckResult]:
    """Run all DoR checks over the PR body.

    `fetch_issue` is passed through to `check_linked_issue_completeness` and
    `check_in_scope`; `fetch_siblings` to the first; `fetch_issue_facts` to `check_linked_issue_in_epic`;
    `fetch_changed_files` to `check_in_scope`. When either IO fetcher is
    None, its check fails open (pass, marked `skipped`).

    The first 5 checks are pure (body -> CheckResult); the last 3 need IO
    through their injected fetchers.
    """
    # The first 5 checks are pure (body -> CheckResult); the last 3 need IO
    # through their injected fetchers.
    results = [check(body) for check in _PURE_CHECKS]
    results.append(check_linked_issue_completeness(
        body, fetch_issue=fetch_issue, fetch_siblings=fetch_siblings,
    ))
    results.append(check_linked_issue_in_epic(body, fetch_issue_facts=fetch_issue_facts))
    results.append(
        check_in_scope(body, fetch_issue=fetch_issue, fetch_changed_files=fetch_changed_files)
    )
    return results
