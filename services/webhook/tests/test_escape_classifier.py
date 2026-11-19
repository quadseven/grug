"""The pure escape classifier uses only trace, finding and review evidence."""

from escape_classifier import (
    EscapeClass,
    ReviewRecord,
    ReviewSource,
    classify_review_outcome,
)
from escape_trace import EscapeTrace, FindingRef


PATH = "services/example.py"


def _trace(*, escaped_pr: int | None = 10, shared_files=(PATH,)) -> EscapeTrace:
    return EscapeTrace(
        correcting_pr=20,
        escaped_pr=escaped_pr,
        link="revert-body" if escaped_pr is not None else None,
        shared_files=shared_files,
    )


def _review(
    *,
    pr: int = 10,
    source: ReviewSource = ReviewSource.ELDER,
    seen_paths=(PATH,),
) -> ReviewRecord:
    return ReviewRecord(pr=pr, source=source, seen_paths=seen_paths)


def _finding(*, pr: int = 10, path: str = PATH, verdict: str = "") -> FindingRef:
    return FindingRef(
        pr=pr,
        path=path,
        finding_class="example-rule",
        verdict=verdict,
        reviewer="elder",
    )


def test_untraced_or_without_shared_paths_has_no_class():
    assert (
        classify_review_outcome(
            _trace(escaped_pr=None), findings=(), review_history=()
        )
        is None
    )
    assert (
        classify_review_outcome(
            _trace(shared_files=()), findings=(), review_history=()
        )
        is None
    )


def test_no_review_of_escaped_pr_is_not_reviewed():
    result = classify_review_outcome(
        _trace(), findings=(), review_history=(_review(pr=11),)
    )

    assert result is EscapeClass.NOT_REVIEWED


def test_review_that_did_not_see_a_shared_file_is_not_seen():
    result = classify_review_outcome(
        _trace(),
        findings=(),
        review_history=(_review(seen_paths=("README.md",)),),
    )

    assert result is EscapeClass.NOT_SEEN


def test_elder_saw_shared_file_without_a_relevant_finding():
    result = classify_review_outcome(_trace(), findings=(), review_history=(_review(),))

    assert result is EscapeClass.SEEN_NOT_FLAGGED


def test_dismissed_finding_is_flagged_dismissed():
    result = classify_review_outcome(
        _trace(),
        findings=(_finding(verdict="dismissed"),),
        review_history=(_review(),),
    )

    assert result is EscapeClass.FLAGGED_DISMISSED


def test_rule_suppressed_finding_is_flagged_suppressed():
    result = classify_review_outcome(
        _trace(),
        findings=(_finding(verdict="suppressed-by-rule"),),
        review_history=(_review(),),
    )

    assert result is EscapeClass.FLAGGED_SUPPRESSED_BY_RULE


def test_unclassified_finding_disposition_has_no_class():
    result = classify_review_outcome(
        _trace(),
        findings=(_finding(verdict="fixed"),),
        review_history=(_review(),),
    )

    assert result is None


def test_deterministic_lane_that_saw_the_file_without_finding_is_a_miss():
    result = classify_review_outcome(
        _trace(),
        findings=(),
        review_history=(_review(source=ReviewSource.DETERMINISTIC_LANE),),
    )

    assert result is EscapeClass.DETERMINISTIC_LANE_MISS


def test_unrelated_findings_and_reviews_do_not_change_the_class():
    result = classify_review_outcome(
        _trace(),
        findings=(_finding(pr=11), _finding(path="README.md")),
        review_history=(_review(pr=11),),
    )

    assert result is EscapeClass.NOT_REVIEWED
