"""Every criterion a PR must meet needs evidence in its diff (#1389).

The commonest damage the lane checks still missed was partial delivery: a
lane PR met one or two of its ticket's criteria, the station closed the
ticket on merge, and the rest were never done. Elder's model review is told
to name the hunk that meets each criterion, but nothing held it to that.

For each criterion in scope (`lane_work.criteria_in_scope`, open on the
issue), this module looks for evidence in the diff: a changed line or path
of code, configuration or test that meets it. A criterion is

- met: the diff changes a target it names (a backticked identifier, a
  path, a snake_case, CamelCase or UPPER_CASE name), or most of its
  content words appear in the changed text;
- missing: it names targets and the diff changes none of them, with
  little of its wording in the diff either, and it asks for a change (not
  to keep, remove or rule something out); or it asks for a test and the
  diff changes no test file;
- unsure: anything else. Once the mapping has found a missing criterion,
  a `CriteriaJudge` (a model call, see `criteria_judge.py`) decides the
  unsure ones when dispatch passes it, and those it finds unmet join the
  finding. An unsure criterion is never reported on
  its own: on the lane corpus the model's "not met" alone flagged 21 of
  303 good PRs and 9 of 103 damaging ones, no better than chance.

Excluded, because the diff cannot show them: live criteria (a running
host, a deploy, a measurement), verification steps ("Confirm ..."),
operator actions, criteria about the pull request process, things kept as
they were ("existing coverage is preserved"), the outcome of a run ("the
suite passes"), and one of several alternatives ("If built: ...").
Loom's verify-before-ship checks the live ones.

One medium `partial-delivery` finding names every criterion without
evidence: the lane must deliver them or narrow the ticket. Medium, not
high, even when most criteria lack evidence: on held-out good lane PRs the
check never saw (sample 3 of the lane corpus), a high for "most criteria
lack evidence" blocked 2 of 100 good PRs while adding 1 of 103 damaging
ones, so it asks rather than blocks.

Pure: criteria, hunks and file changes in; findings out. Fail-open: past
`MAX_SCAN_CHARS` of changed text the check is not evaluated.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from personas.code_reviewer.criteria_coverage import (
    _LIVE_STATE_RE,
    _PROCESS_CRITERION_RE,
    _TEST_CRITERION_RE,
    LinkedCriterion,
)
from personas.code_reviewer.diff_parser import DiffHunk, FileChange
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file
from personas.code_reviewer.unbacked_claim import _LIVE_CLAIM_RE
from personas.code_reviewer.wrong_target import (
    _BACKTICK_RE,
    _NOT_A_CHANGE_RE,
    _PATH_RE,
    _identifier,
    _spellings,
)

RULE = "partial-delivery"
SEVERITY = "medium"
MAX_SCAN_CHARS = 4_000_000
MAX_CRITERIA = 45

Status = Literal["met", "missing", "unsure"]
CriterionKey = tuple[int, int]
# Decides the unsure criteria: True when it found grounded evidence, False
# when it found none; a criterion left out stays unsure.
CriteriaJudge = Callable[
    [tuple[LinkedCriterion, ...], tuple[DiffHunk, ...]],
    Mapping[CriterionKey, bool],
]

# An action only the operator, or a person by hand, can take.
_OPERATOR_RE = re.compile(
    r"\boperator\b|\bmanual(?:ly)?\b|\bby hand\b|\bsign[- ]?off\b|\bapproves?\b|\bapproval\b",
    re.IGNORECASE,
)
# A verification step or a measurement: a run shows it, not the diff.
_VERIFY_RE = re.compile(
    r"^\W*(?:confirm|verify|check)\b|\bmeasurabl[ey]\b", re.IGNORECASE
)
# Something kept as it was: nothing in a diff shows a thing left alone.
_KEEP_RE = re.compile(
    r"\b(?:preserv\w*|retain\w*|remains?|still|unchanged|untouched|keeps?|kept"
    r"|(?:does ?n[o']t|do not|never|without) (?:break\w*|regress\w*|reduc\w*|chang\w*)"
    r"|no (?:new|regressions?)\b)",
    re.IGNORECASE,
)
# The outcome of a run ("the suite passes", "the build is green"): CI shows
# it. A test that "fails without the change and passes with it" is a test
# to write, so it is not one of these.
_RUN_OUTCOME_RE = re.compile(
    r"\b(?:tests?|suites?|checks?|ci|builds?|lint\w*|ruff|pytest|runs?)\b[^.;]*\bpass(?:es|ing)?\b"
    r"|\bgreen\b",
    re.IGNORECASE,
)
# One of several alternatives ("If built: ...; If deleted: ..."): only one
# can hold.
_CONDITIONAL_RE = re.compile(r"^\W*if\b", re.IGNORECASE)
# A criterion that asks for a test: "a test", "tests cover", "(test)".
_ASKS_FOR_TEST_RE = re.compile(
    r"\b(?:unit |regression |integration |a |new |added |focused )?tests?\b(?! suite)"
    r"|\btest_\w+|\bcovered by\b",
    re.IGNORECASE,
)
# Snake_case, camelCase, CamelCase and UPPER_CASE names in plain text.
_NAME_RE = re.compile(
    r"(?<![\w./<-])(?:[a-z][a-z0-9]*_[a-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]+"
    r"|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]+|[A-Z][A-Z0-9]*_[A-Z0-9_]+)(?![\w<>-])"
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_STOP_WORDS = """
a an the and or of to in on for with by from at as is are be been being this that these
those it its which who what when where why how not no nor but if then than so such into
onto out over under about after before between through during without within each every
all any both either neither some more most other same new old only own can could should
would will shall may might must do does did done has have had having there their them
they we our you your pr ticket issue criterion criteria change changes changed diff file
files line lines code add adds added adding make makes made use uses used using via also
still just one two three instead whether here
"""
_STOP = frozenset(_STOP_WORDS.split())
_TEST_NAME_RE = re.compile(r"(\.test\.|\.spec\.|_test\.\w+$|^Test[A-Z_])")
# Met when at least this share of a criterion's content words, and at
# least two, appear in the changed text.
_MET_SHARE = 0.5
# Missing (with named targets none of which changed) below this share.
_MISSING_SHARE = 0.34


@dataclass(frozen=True, slots=True)
class CriterionEvidence:
    criterion: LinkedCriterion
    status: Status
    # Decided by the model judge rather than the diff mapping.
    judged: bool = False


def _stem(word: str) -> str:
    w = word.lower()
    for suffix in ("ings", "ing", "ies", "ied", "es", "ed", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def _content_words(text: str) -> list[str]:
    plain = _BACKTICK_RE.sub(" ", text)
    return list(
        dict.fromkeys(
            _stem(w)
            for w in _WORD_RE.findall(plain)
            if len(w) >= 4 and w.lower() not in _STOP
        )
    )


def _targets(text: str) -> list[str]:
    found: dict[str, None] = {}
    for match in _BACKTICK_RE.finditer(text):
        ident = _identifier(match.group(1))
        if ident:
            found[ident] = None
    plain = _BACKTICK_RE.sub(" ", text)
    for match in _PATH_RE.finditer(plain):
        found[match.group(1)] = None
    for match in _NAME_RE.finditer(plain):
        found[match.group(0)] = None
    return list(found)


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return is_test_file(path) or bool(_TEST_NAME_RE.search(name)) or "/t/" in f"/{path}"


def excluded(text: str) -> bool:
    """Criteria a diff cannot show: live state, verification steps,
    operator actions, the pull request process, things kept as they were,
    the outcome of a run, and one of several alternatives."""
    return bool(
        _LIVE_STATE_RE.search(text)
        or _LIVE_CLAIM_RE.search(text)
        or _OPERATOR_RE.search(text)
        or _PROCESS_CRITERION_RE.search(text)
        or _VERIFY_RE.search(text)
        or _KEEP_RE.search(text)
        or _CONDITIONAL_RE.search(text)
        or (_RUN_OUTCOME_RE.search(text) and not re.search(r"\bfails?\b", text, re.IGNORECASE))
    )


class _Changed:
    """The diff's changed text (paths and changed lines), lowercased, and
    its stemmed words, from one pass."""

    def __init__(
        self, changes: Iterable[FileChange], hunks: Iterable[DiffHunk]
    ) -> None:
        parts: list[str] = []
        self.has_test = False
        for change in changes:
            parts.append(change.path)
            if change.previous_path:
                parts.append(change.previous_path)
            self.has_test = self.has_test or is_test_path(change.path)
        size = sum(len(p) for p in parts)
        self.bounded = True
        for hunk in hunks:
            for raw in hunk.body.splitlines()[1:]:
                if raw[:1] in ("+", "-"):
                    parts.append(raw[1:])
                    size += len(raw)
            if size > MAX_SCAN_CHARS:
                self.bounded = False
                break
        raw_text = "\n".join(parts)
        self.text = raw_text.lower()
        split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw_text)
        self.words = {_stem(w) for w in _WORD_RE.findall(split)} | {
            _stem(w) for w in re.findall(r"[A-Za-z0-9]+", raw_text)
        }

    def names(self, target: str) -> bool:
        return any(s in self.text for s in _spellings(target))


def _status(text: str, diff: _Changed) -> Status:
    if (
        _ASKS_FOR_TEST_RE.search(text)
        and _TEST_CRITERION_RE.search(text)
        and not diff.has_test
    ):
        return "missing"
    targets = _targets(text)
    if any(diff.names(t) for t in targets):
        return "met"
    words = _content_words(text)
    hits = sum(1 for w in words if w in diff.words)
    share = hits / len(words) if words else 1.0
    # A criterion that keeps, removes or rules something out can be met
    # without the diff naming it; only a reader can tell.
    if targets and share < _MISSING_SHARE and not _NOT_A_CHANGE_RE.search(text):
        return "missing"
    if hits >= 2 and share >= _MET_SHARE:
        return "met"
    return "unsure"


def criteria_evidence(
    criteria: tuple[LinkedCriterion, ...],
    changes: Iterable[FileChange],
    hunks: tuple[DiffHunk, ...],
    *,
    judge: CriteriaJudge | None = None,
) -> tuple[CriterionEvidence, ...]:
    """The evidence status of each in-scope criterion the diff can show,
    or () when the check is not evaluated."""
    evaluated = tuple(c for c in criteria if not excluded(c.text))
    if not evaluated or len(evaluated) > MAX_CRITERIA:
        return ()
    diff = _Changed(changes, hunks)
    if not diff.bounded:
        return ()
    statuses = {(c.issue, c.number): _status(c.text, diff) for c in evaluated}
    judged: set[CriterionKey] = set()
    unsure = tuple(c for c in evaluated if statuses[(c.issue, c.number)] == "unsure")
    # The judge only weighs in once the mapping has found a criterion with
    # no evidence: alone, its "not met" was as common on good lane PRs as
    # on damaging ones (lane corpus, #1389).
    if unsure and judge is not None and "missing" in statuses.values():
        verdicts = judge(unsure, hunks)
        for c in unsure:
            verdict = verdicts.get((c.issue, c.number))
            if verdict is not None:
                statuses[(c.issue, c.number)] = "met" if verdict else "missing"
                judged.add((c.issue, c.number))
    return tuple(
        CriterionEvidence(c, statuses[(c.issue, c.number)], (c.issue, c.number) in judged)
        for c in evaluated
    )


def _anchor(hunks: Iterable[DiffHunk]) -> tuple[str, int] | None:
    first = None
    for hunk in hunks:
        if not hunk.new_lines:
            continue
        if not is_test_path(hunk.file_path):
            return hunk.file_path, min(hunk.new_lines)
        first = first or (hunk.file_path, min(hunk.new_lines))
    return first


def scan_partial_delivery(
    criteria: tuple[LinkedCriterion, ...],
    changes: Iterable[FileChange],
    hunks: tuple[DiffHunk, ...],
    *,
    judge: CriteriaJudge | None = None,
) -> tuple[Finding, ...]:
    """One finding naming every in-scope criterion with no evidence."""
    found = criteria_evidence(criteria, changes, hunks, judge=judge)
    missing = [e.criterion for e in found if e.status == "missing"]
    anchor = _anchor(hunks)
    if not missing or anchor is None:
        return ()
    most = len(missing) >= 2 and 2 * len(missing) > len(found)
    listed = "; ".join(
        f"#{c.issue} criterion {c.number}: {c.text}" for c in missing[:6]
    )
    more = f"; and {len(missing) - 6} more" if len(missing) > 6 else ""
    return (
        Finding(
            file=anchor[0],
            line=anchor[1],
            severity=SEVERITY,
            rule_name=RULE,
            message=(
                f"{len(missing)} of the {len(found)} linked criteria this PR must meet have no "
                f"evidence in the diff (no changed code, configuration or test meets them)"
                f"{', most of the ticket' if most else ''}: {listed}{more}. Deliver them, or "
                "narrow the ticket to what this PR does."
            ),
            suggestion=None,
            effort="heavy-lift",
        ),
    )
