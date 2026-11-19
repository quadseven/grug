"""Classify a traced escape from recorded review evidence only."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from escape_trace import EscapeTrace, FindingRef


class EscapeClass(StrEnum):
    """The six review outcomes in the escaped-defect loop."""

    NOT_REVIEWED = "not-reviewed"
    NOT_SEEN = "not-seen"
    SEEN_NOT_FLAGGED = "seen-not-flagged"
    FLAGGED_DISMISSED = "flagged-dismissed"
    FLAGGED_SUPPRESSED_BY_RULE = "flagged-suppressed-by-rule"
    DETERMINISTIC_LANE_MISS = "deterministic-lane-miss"


class ReviewSource(StrEnum):
    """A review path that can account for the escape."""

    ELDER = "elder"
    DETERMINISTIC_LANE = "deterministic-lane"


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """Files actually included in one review of a pull request."""

    pr: int
    source: ReviewSource
    seen_paths: tuple[str, ...]


def _verdict(value: str) -> str:
    return value.strip().casefold().replace("_", "-")


def _finding_disposition(
    findings: Sequence[FindingRef],
) -> tuple[bool, EscapeClass | None]:
    verdicts = frozenset(_verdict(finding.verdict) for finding in findings)
    classes = {
        "dismissed": EscapeClass.FLAGGED_DISMISSED,
        "suppressed-by-rule": EscapeClass.FLAGGED_SUPPRESSED_BY_RULE,
    }
    if not verdicts:
        return True, None
    if len(verdicts) != 1:
        return False, None
    verdict = next(iter(verdicts))
    if verdict not in classes:
        return False, None
    return True, classes[verdict]


def _seen_by_source(
    reviews: Sequence[ReviewRecord], shared_files: frozenset[str],
) -> dict[ReviewSource, frozenset[str]]:
    seen = {source: set() for source in ReviewSource}
    for record in reviews:
        seen[record.source].update(set(record.seen_paths) & shared_files)
    return {source: frozenset(paths) for source, paths in seen.items()}


def _classify_reviewed_paths(
    shared_files: frozenset[str],
    findings: Sequence[FindingRef],
    reviews: Sequence[ReviewRecord],
) -> EscapeClass | None:
    valid_disposition, disposition = _finding_disposition(findings)
    if not valid_disposition:
        return None
    if disposition is not None:
        return disposition

    seen_by_source = _seen_by_source(reviews, shared_files)
    seen_paths = frozenset().union(*seen_by_source.values())
    seen_paths |= frozenset(finding.path for finding in findings)
    if not seen_paths:
        return EscapeClass.NOT_SEEN if reviews else EscapeClass.NOT_REVIEWED

    deterministic_findings = frozenset(
        finding.path
        for finding in findings
        if finding.reviewer.strip().casefold() == ReviewSource.DETERMINISTIC_LANE.value
    )
    if seen_by_source[ReviewSource.DETERMINISTIC_LANE] - deterministic_findings:
        return EscapeClass.DETERMINISTIC_LANE_MISS
    return EscapeClass.SEEN_NOT_FLAGGED


def classify_review_outcome(
    trace: EscapeTrace,
    *,
    findings: Sequence[FindingRef],
    review_history: Sequence[ReviewRecord],
) -> EscapeClass | None:
    """Classify a traced escape, or return ``None`` without shared-file evidence.

    Pass findings already associated with the escaped defect. They are still
    restricted to the escaped PR and paths shared with the correction. Review
    records describe files the named review path actually saw, not merely
    files present in the pull request.
    """
    escaped_pr = trace.escaped_pr
    shared_files = frozenset(trace.shared_files)
    if escaped_pr is None or not shared_files:
        return None

    reviews = tuple(record for record in review_history if record.pr == escaped_pr)
    relevant_findings = tuple(
        finding
        for finding in findings
        if finding.pr == escaped_pr and finding.path in shared_files
    )
    if not reviews and not relevant_findings:
        return EscapeClass.NOT_REVIEWED
    # A finding itself proves that a review saw its path, even if an older
    # review snapshot did not retain its file list.
    return _classify_reviewed_paths(shared_files, relevant_findings, reviews)
