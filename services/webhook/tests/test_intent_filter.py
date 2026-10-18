"""Tests for the intent-mismatch post-filter (#1170).

Recorded inputs are the real incident: a negated acceptance criterion
("no guild job writes a leave row for a cohort whose queue did not pop;
the leave counter ignores a refused leave") met by a PR that REMOVED the
row and narrowed the counter, while the Elder read the PR's own "stop
writing" / "no ... row" summary as refusing the work and raised a high
intent-mismatch that blocked a correct merge.

The filter holds the line the prompt now teaches:
- an intent-mismatch finding that does not quote BOTH the criterion and
  the PR sentence is dropped;
- one whose two quotes share the same negation (both say "no"/"never"
  about the same thing) is lowered to medium (advisory, never blocks);
- a finding where the PR ADDS the forbidden thing stays high.
"""

from __future__ import annotations

from personas.code_reviewer.intent_filter import filter_intent_mismatch_findings
from personas.code_reviewer.persona import Finding

# Recorded from the #1170 incident.
_CRITERION = (
    "no guild job writes a leave row for a cohort whose queue did not "
    "pop; the leave counter ignores a refused leave"
)
_PR_SUMMARY_NEGATED = (
    "stop writing leave rows for cohorts whose queue did not pop; the "
    "leave counter now ignores refused leaves"
)
_PR_SUMMARY_ADDING = (
    "writes a leave row for cohorts whose queue did not pop, to keep an "
    "audit trail of refusals"
)


def _finding(rule: str, severity: str, message: str) -> Finding:
    return Finding(
        file="jobs.py",
        line=88,
        severity=severity,  # type: ignore[arg-type]
        rule_name=rule,
        message=message,
        suggestion=None,
    )


def _incident_finding(severity: str = "high") -> Finding:
    """The recorded false positive: both texts quoted, shared negation."""
    return _finding(
        "intent-mismatch",
        severity,
        f'Grug see tablet demand "{_CRITERION}", yet the change boasts '
        f'"{_PR_SUMMARY_NEGATED}" - the change refuses the work the tablet '
        "commands. The hunk at jobs.py:88 deletes the writer. So speaks Grug.",
    )


def test_shared_negation_lowered_to_medium_not_high():
    """The recorded #1170 case: the PR removes the forbidden thing and both
    the criterion and the PR sentence say "no" about leave rows. Not raised
    as a blocking high; advisory medium at most."""
    kept, dropped, lowered = filter_intent_mismatch_findings(
        (_incident_finding("high"),)
    )
    assert dropped == 0
    assert lowered == 1
    assert len(kept) == 1
    assert kept[0].severity == "medium"
    assert kept[0].rule_name == "intent-mismatch"


def test_shared_negation_already_medium_kept():
    kept, dropped, lowered = filter_intent_mismatch_findings(
        (_incident_finding("medium"),)
    )
    assert dropped == 0
    assert lowered == 0
    assert len(kept) == 1
    assert kept[0].severity == "medium"


def test_finding_without_both_quotes_dropped():
    """An intent-mismatch that quotes nothing (or only one text) carries no
    checkable evidence - the prompt requires both quotes, so drop it."""
    no_quotes = _finding(
        "intent-mismatch",
        "high",
        "Grug see change walk away from the tablet's command. The hunk at "
        "jobs.py:88 deletes the writer the criterion demands. So speaks Grug.",
    )
    one_quote = _finding(
        "intent-mismatch",
        "high",
        f'Grug see tablet demand "{_CRITERION}", and the change walks away. '
        "The hunk at jobs.py:88 deletes the writer. So speaks Grug.",
    )
    kept, dropped, lowered = filter_intent_mismatch_findings((no_quotes, one_quote))
    assert dropped == 2
    assert lowered == 0
    assert kept == ()


def test_pr_adds_forbidden_thing_stays_high():
    """The true positive the filter must NOT touch: the criterion forbids
    the row and the PR's own sentence says it writes one anyway."""
    finding = _finding(
        "intent-mismatch",
        "high",
        f'Grug see tablet demand "{_CRITERION}", yet the change boasts '
        f'"{_PR_SUMMARY_ADDING}" - the change writes the very row the tablet '
        "forbids. The hunk at jobs.py:88 adds the writer. So speaks Grug.",
    )
    kept, dropped, lowered = filter_intent_mismatch_findings((finding,))
    assert dropped == 0
    assert lowered == 0
    assert len(kept) == 1
    assert kept[0].severity == "high"


def test_other_rules_untouched():
    other = _finding(
        "missing-error-handling",
        "high",
        "Grug see net cast with no ward. `db.write(row)` may fail and none "
        "catch the fall. So speaks Grug.",
    )
    kept, dropped, lowered = filter_intent_mismatch_findings((other,))
    assert (dropped, lowered) == (0, 0)
    assert kept == (other,)


def test_mixed_batch():
    incident = _incident_finding("high")
    adding = _finding(
        "intent-mismatch",
        "high",
        f'Grug see tablet demand "{_CRITERION}", yet the change boasts '
        f'"{_PR_SUMMARY_ADDING}". The hunk at jobs.py:90 adds the writer. '
        "So speaks Grug.",
    )
    kept, dropped, lowered = filter_intent_mismatch_findings((incident, adding))
    assert dropped == 0
    assert lowered == 1
    severities = sorted(f.severity for f in kept)
    assert severities == ["high", "medium"]


# --- #1178: each evidence shape on its own -------------------------------
#
# The batched drop test above asserts a combined count, so a regression on
# one shape can hide behind the other. These pin each branch singly.


def _filter_one(finding: Finding) -> tuple[tuple[Finding, ...], int, int]:
    return filter_intent_mismatch_findings((finding,))


def test_single_quote_of_criterion_only_dropped():
    """Quotes the criterion but never the PR sentence: no checkable
    contradiction, so it is dropped, whatever the lead text says."""
    finding = _finding(
        "intent-mismatch",
        "high",
        f'Grug see tablet demand "{_CRITERION}", yet the change walks away. '
        "The hunk at jobs.py:88 deletes the writer. So speaks Grug.",
    )
    assert _filter_one(finding) == ((), 1, 0)


def test_single_quote_of_pr_sentence_only_dropped():
    """Quotes the PR sentence but never the criterion: also dropped."""
    finding = _finding(
        "intent-mismatch",
        "high",
        f'Grug see the change boast "{_PR_SUMMARY_ADDING}" against the '
        "tablet. The hunk at jobs.py:88 adds the writer. So speaks Grug.",
    )
    assert _filter_one(finding) == ((), 1, 0)


def test_short_emphasis_quote_is_not_evidence():
    """A quoted identifier under the sentence-length floor is emphasis, not
    a second quote: one real quote plus "row" is still one quote."""
    finding = _finding(
        "intent-mismatch",
        "high",
        f'Grug see tablet demand "{_CRITERION}", yet the change keeps the '
        '"row" writer. The hunk at jobs.py:88. So speaks Grug.',
    )
    assert _filter_one(finding) == ((), 1, 0)


def test_both_negated_about_different_things_stays_high():
    """Two negated quotes that share no content word are not the #1170
    misread: the filter must not lower a real contradiction on polarity
    alone."""
    finding = _finding(
        "intent-mismatch",
        "high",
        f'Grug see tablet demand "{_CRITERION}", yet the change boasts '
        '"never retries the billing webhook on timeout". The hunk at '
        "jobs.py:88. So speaks Grug.",
    )
    kept, dropped, lowered = _filter_one(finding)
    assert (dropped, lowered) == (0, 0)
    assert kept == (finding,)


def test_negation_after_the_first_clause_does_not_lower():
    """Only the lead of the FIRST clause sets polarity: a PR sentence that
    WRITES the forbidden rows and negates something after the comma is an
    affirmative action, so the finding stays high. The first clause is
    shorter than the four-word lead window on purpose, so the "never"
    after the comma would leak into the lead without the clause split."""
    finding = _finding(
        "intent-mismatch",
        "high",
        f'Grug see tablet demand "{_CRITERION}", yet the change boasts '
        '"writes leave rows, never skipping a refused leave". The hunk at '
        "jobs.py:88. So speaks Grug.",
    )
    kept, dropped, lowered = _filter_one(finding)
    assert (dropped, lowered) == (0, 0)
    assert kept[0].severity == "high"
