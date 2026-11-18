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
carries no run link or fenced output block. An operational behavior box
can also be backed by a matching test in the diff. Medium, not high: a
ticked box can be true, and the evidence may sit where this check does not
look (a comment, the ticket), so the finding flags it without blocking the PR.

The box text is split at the first " -- " or " - " the lane added after
the criterion: the part before is what is claimed, the part after is the
evidence offered. When the box repeats an in-scope linked criterion, the
finding names it by issue and number.

Bounded: one pass over the body's lines and the added test lines, each
ticked box compared with at most `MAX_CRITERIA` criteria. Past
`MAX_BODY_CHARS`, `MAX_BOXES` or `MAX_CRITERIA` the check is not evaluated.

Pure: PR body, in-scope criteria and parsed hunks in; findings out.
"""

from __future__ import annotations

import re

from personas.code_reviewer.criteria_coverage import (
    LinkedCriterion,
    is_future_post_merge_observation,
)
from personas.code_reviewer.diff_parser import DiffHunk, added_lines
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

RULE = "unbacked-claim"
MAX_BODY_CHARS = 200_000
# Ticked boxes and linked criteria compared; past either, not evaluated.
MAX_BOXES = 200
MAX_CRITERIA = 200

_TICKED_RE = re.compile(r"^\s*[-*+]\s+\[[xX]\]\s+(\S.*)$")
_OBSERVE = r"\b(?:check(?:ed)?|confirm(?:ed)?|verif(?:y|ied)|observ(?:e|ed)|measur(?:e|ed|ements?)|record(?:ed)?)\b"
_DEPLOYED = (
    r"(?<![\w-])(?:post[- ]deploy(?:ment)?|after (?:the )?deploy(?:ment)?|"
    r"(?:deployed|running|live) (?:service|system|environment)|"
    r"(?:on|from) (?:the )?dashboard|(?:production|live) dashboard)\b(?!-)"
)
# Observations of the running system, including a recorded live check outcome.
_LIVE_CLAIM_RE = re.compile(
    r"^\s*live\s*:"
    r"|(?<![\w-])live[- ](?:verification|checks?|runs?) (?:outcomes?|results?|output)\b"
    r"|\bverified live\b|\b(?:on|against) (?:a |an |the )?"
    r"(?:live|real|running|production|deployed) (?:\w+[- ])?"
    r"(?:services?|systems?|servers?|hosts?|devices?|clusters?|environments?)\b"
    r"|\bin production\b|\bon (?:the |each |both |every |all )?"
    r"(?:hosts?|servers?|handsets?|phones?|routers?|cluster)\b"
    r"|\bmeasured (?:live|on|in production|over|across)\b|\bmeasured\s+\w+\s+traffic\b"
    rf"|{_OBSERVE}[^.;]{{0,100}}{_DEPLOYED}|{_DEPLOYED}[^.;]{{0,100}}{_OBSERVE}",
    re.IGNORECASE,
)
# Admission and access checks can be run or exercised by tests of the policy.
_OPERATIONAL_CHECK_RE = re.compile(
    r"^\s*(?:verify|check|confirm)\b[^.;]{0,80}"
    r"(?:\bservice account is (?:allowed|denied)\b[^.;]{0,100}\bin\b[^.;]{0,60}\bnamespaces?\b"
    r"|\bpod is rejected\b[^.;]{0,80}\bquota\b)",
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
_TEST_FILLER = frozenset({"a", "an", "the", "is", "are", "with", "that", "verify", "check", "confirm"})


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
    (past a bound). When the body cites a run or pastes output, only the
    boxes for a CI run after the merge, which no pre-merge run can be."""
    if len(body) > MAX_BODY_CHARS or len(criteria) > MAX_CRITERIA:
        return None
    ticked = [m.group(1).strip() for m in map(_TICKED_RE.match, body.splitlines()) if m]
    if len(ticked) > MAX_BOXES:
        return None
    if _RUN_LINK_RE.search(body) or _FENCE_RE.search(body):
        return [t for t in ticked if is_future_post_merge_observation(t)]
    return ticked


def _unbacked_name(
    text: str, known: list[_Known], test_words: list[set[str]],
) -> str | None:
    """How the finding names this box when it claims an unbacked live
    check, else None."""
    match = _criterion_for(text, known)
    criterion = match[0] if match else None
    claim, evidence = _split(text, match)
    # A CI run after the merge cannot have happened yet: no evidence backs
    # a tick on it before the merge (#1509).
    if not is_future_post_merge_observation(claim):
        prose = _CODE_SPAN_RE.sub(" ", claim)
        if not _LIVE_CLAIM_RE.search(prose):
            if not _OPERATIONAL_CHECK_RE.search(prose) or _TEST_RE.search(prose):
                return None
            words = set(_words(claim)) - _TEST_FILLER
            if any(words <= added for added in test_words):
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
            "link or number for it, or the check is a CI run after the merge, "
            "which cannot have happened yet. Paste the command and its output, "
            "link the run, or leave the box unticked for the operator."
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
    if not ticked:
        return ()
    known = [(c, _words(c.text)) for c in criteria]
    test_words = [
        {word for _, line in added_lines(hunk) if _TEST_RE.search(line) for word in _words(line)}
        for hunk in hunks
        if is_test_file(hunk.file_path)
    ]
    names = (_unbacked_name(text, known, test_words) for text in ticked)
    # dict.fromkeys keeps the first occurrence of each name, in order.
    unbacked = list(dict.fromkeys(n for n in names if n is not None))
    if not unbacked:
        return ()
    anchor = _anchor(hunks)
    if anchor is None:
        return ()
    return (_unbacked_finding(anchor, unbacked),)
