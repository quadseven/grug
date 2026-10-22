"""Deterministic check for a ticked box no diff could have checked (#1371).

Lane PR bodies ticked boxes for checks of running systems with nothing
behind them: "maps a route with MEASURED live traffic, evidenced in the PR
with the query and the number" ticked with a query and no number while the
metrics backend was off; "Verify the service account is denied access in
the runner namespaces" ticked for a script no workflow runs; "Record the
live-verification outcome" ticked with nothing recorded. Chief counts ticked
boxes and Elder read them as context, so each claim stood.

One medium `unbacked-claim` finding per PR, naming every such box: a box
the body ticks (`- [x]`) whose criterion is a live, host or measurement
check, when the box cites no number, link or command output and the body
carries no run link or fenced output block. A box about tests is skipped:
the diff itself can back it. Medium, not high: a ticked box can be true,
and the evidence may sit where this check does not look (a comment, the
ticket), so the finding flags the claim without blocking the PR.

The box text is split at the first " -- " or " - " the lane added after
the criterion: the part before is what is claimed, the part after is the
evidence offered. When the box repeats an in-scope linked criterion, the
finding names it by issue and number.

Bounded: one pass over the body's lines, each ticked box compared with at
most `MAX_CRITERIA` criteria. Past `MAX_BODY_CHARS`, `MAX_BOXES` or
`MAX_CRITERIA` the check is not evaluated (no finding).

Pure: PR body, in-scope criteria and parsed hunks in; findings out.
"""

from __future__ import annotations

import re

from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

RULE = "unbacked-claim"
MAX_BODY_CHARS = 200_000
# Ticked boxes and linked criteria compared; past either, not evaluated.
MAX_BOXES = 200
MAX_CRITERIA = 200

_TICKED_RE = re.compile(r"^\s*[-*+]\s+\[[xX]\]\s+(\S.*)$")
# A check only a run against a live system can satisfy.
_LIVE_CLAIM_RE = re.compile(
    r"\blive[- ](?:verif\w*|traffic|runs?|round[- ]trips?|checks?|proof|sync|capture"
    r"|servers?|hosts?|devices?|handsets?|cluster|system|store|deploy\w*|environment)\b"
    r"|\bverified live\b|\bverif(?:y|ied) (?:on|against) (?:a |an |the )?"
    r"(?:live|real|running|production|deployed)\b|^\s*verify\b"
    r"|\bin production\b|\bon (?:the |each |both |every |all )?"
    r"(?:hosts?|servers?|handsets?|phones?|routers?|cluster)\b"
    r"|\bmeasured (?:live|on|in production|over|across)\b|\bmeasured\s+\w+\s+traffic\b"
    r"|\breal (?:traffic|requests?|devices?|handsets?|phones?|servers?|hosts?)\b"
    r"|\bservice account\b|\bquota\b|\bkubectl\b|\bssh\b",
    re.IGNORECASE,
)
_TEST_RE = re.compile(
    r"\btest(?:s|ed|ing)?\b|\btest_\w+|\bassert(?:s|ion|ions)?\b|\bcoverage\b|\bpytest\b",
    re.IGNORECASE,
)
# Where a lane appends its evidence to the criterion it ticks.
_EVIDENCE_SPLIT_RE = re.compile(r"\s(?:--|\u2014|-)\s")
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
# A number standing on its own, not a version, path, address or name part.
_NUMBER_RE = re.compile(r"(?<![\w./:<>-])\d[\d,]*(?:\.\d+)?(?![\w>/.-]|\.\d)")
_LINK_RE = re.compile(r"https?://")
_RUN_LINK_RE = re.compile(r"https?://\S+/(?:actions/)?runs?/\d+")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)", re.MULTILINE)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


_Known = tuple[LinkedCriterion, list[str]]


def _criterion_for(claim: str, known: list[_Known]) -> _Known | None:
    """The in-scope criterion this box repeats: its words start the box."""
    words = _words(claim)
    for c, own in known:
        if own and words[: len(own)] == own:
            return c, own
    return None


def _split(text: str, match: _Known | None) -> tuple[str, str]:
    """(what is claimed, the evidence the lane appended)."""
    if match is not None:
        criterion, own = match
        # Walk the box until its words cover the criterion's.
        for seen, m in enumerate(_WORD_RE.finditer(text.lower()), start=1):
            if seen == len(own):
                return criterion.text, text[m.end():]
    m = _EVIDENCE_SPLIT_RE.search(text)
    if m:
        return text[: m.start()], text[m.end():]
    return text, ""


def _backed(evidence: str) -> bool:
    if _LINK_RE.search(evidence):
        return True
    return bool(_NUMBER_RE.search(_CODE_SPAN_RE.sub(" ", evidence)))


def _anchor(hunks: tuple[DiffHunk, ...]) -> tuple[str, int] | None:
    """First added line, preferring a non-test file: the claim is about the
    PR as a whole, and a line inside a hunk is the only place a finding can
    sit."""
    fallback = None
    for hunk in hunks:
        if not hunk.new_lines:
            continue
        spot = (hunk.file_path, min(hunk.new_lines))
        if not is_test_file(hunk.file_path):
            return spot
        fallback = fallback or spot
    return fallback


def _ticked_boxes(body: str, criteria: tuple[LinkedCriterion, ...]) -> list[str] | None:
    """The ticked boxes in `body`; None when the check is not evaluated
    (past a bound, or the body cites a run or pastes output)."""
    if len(body) > MAX_BODY_CHARS or len(criteria) > MAX_CRITERIA:
        return None
    if _RUN_LINK_RE.search(body) or _FENCE_RE.search(body):
        return None
    ticked = [m.group(1).strip() for m in map(_TICKED_RE.match, body.splitlines()) if m]
    if len(ticked) > MAX_BOXES:
        return None
    return ticked


def _unbacked_name(text: str, known: list[_Known]) -> str | None:
    """How the finding names this box when it claims an unbacked live
    check, else None."""
    match = _criterion_for(text, known)
    criterion = match[0] if match else None
    claim, evidence = _split(text, match)
    if not _LIVE_CLAIM_RE.search(claim) or _TEST_RE.search(claim):
        return None
    if _backed(evidence) or (criterion is None and _backed(claim)):
        return None
    label = f"#{criterion.issue} criterion {criterion.number}: " if criterion else ""
    return f'{label}"{claim.strip()[:160]}"'


def _unbacked_finding(anchor: tuple[str, int], unbacked: list[str]) -> Finding:
    noun = "box" if len(unbacked) == 1 else "boxes"
    return Finding(
        file=anchor[0],
        line=anchor[1],
        severity="medium",
        rule_name=RULE,
        message=(
            f"The PR body ticks {len(unbacked)} {noun} for a check of a live "
            f"system, host or measurement: {'; '.join(unbacked)}. A diff cannot "
            "show that such a check passed, and the body cites no output, run "
            "link or number for it. Paste the command and its output, link "
            "the run, or leave the box unticked for the operator."
        ),
        suggestion=None,
        effort="quick-win",
    )


def scan_unbacked_claims(
    pr_body: str,
    *,
    criteria: tuple[LinkedCriterion, ...],
    hunks: tuple[DiffHunk, ...],
) -> tuple[Finding, ...]:
    """One medium finding naming every unbacked live box the body ticks."""
    ticked = _ticked_boxes(pr_body or "", criteria)
    if ticked is None:
        return ()
    known = [(c, _words(c.text)) for c in criteria]
    names = (_unbacked_name(text, known) for text in ticked)
    # dict.fromkeys keeps the first occurrence of each name, in order.
    unbacked = list(dict.fromkeys(n for n in names if n is not None))
    if not unbacked:
        return ()
    anchor = _anchor(hunks)
    if anchor is None:
        return ()
    return (_unbacked_finding(anchor, unbacked),)
