"""Deterministic checks for lane work that cannot do what it claims (#1363).

An audit of the lane pull requests merged from 2026-10-11 to 2026-10-22
found about 73 that did damage, and Elder passed every one. Most had a
shape visible in the diff and the PR body alone. This module is the one
seam the deterministic checks for those shapes run through, so dispatch
and the offline corpus runner call exactly the same code:

- criteria coverage (#1179, #1364): the diff cannot meet the linked
  criteria. A lane PR links its ticket with `Refs owner/name#N`, but the
  station closes that ticket when the PR merges, so its criteria count as
  if the PR closed it.
- destructive edits (#1365): the diff deletes or replaces most of a file
  that neither the title nor the linked criteria name.
- weakened verification (#1367): the diff deletes or flips the tests that
  pinned the old behavior, or drops a verification layer from CI.
- fabricated results (#1368): a rate impossible for its sample size, a
  results file from a simulate path, an audit with no output behind it, a
  percentage set from a count.
- unbacked claims (#1371): the body ticks a live or measurement box and
  cites no output, run link or number.
- dead on arrival (#1366): a new source file nothing else in the diff
  loads, and scratch or temporary files left in the commit.
- wrong target (#1369): the criteria name concrete targets and the diff
  changes none of them, naming them only in comments or tests.
- measurement only (#1370): a health ticket's fix changes only the code
  that measures or reports the finding.
- partial delivery (#1389): an open in-scope criterion with no evidence
  in the diff. Dispatch passes a model `criteria_judge` for the criteria
  the deterministic mapping cannot decide; the corpus runner does not.

Pure: diff text, parsed hunks, file contents and PR text in; findings out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from personas.code_reviewer.criteria_coverage import (
    LinkedCriterion,
    scan_criteria_coverage,
)
from personas.code_reviewer.dead_on_arrival import drop_superseded, scan_dead_on_arrival
from personas.code_reviewer.destructive_edit import scan_destructive_edits
from personas.code_reviewer.diff_parser import DiffHunk, FileChange, file_changes
from personas.code_reviewer.fabricated_result import scan_fabricated_results
from personas.code_reviewer.intent_filter import body_claims_box
from personas.code_reviewer.measurement_only import scan_measurement_only
from personas.code_reviewer.partial_delivery import CriteriaJudge, scan_partial_delivery
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.unbacked_claim import scan_unbacked_claims
from personas.code_reviewer.weakened_verification import scan_weakened_verification
from personas.code_reviewer.wrong_target import scan_wrong_target
from personas.tpm.ticket_compliance import closes_refs

# The footer a Loom lane writes on every pull request it opens.
_LANE_FOOTER_RE = re.compile(r"^Loom ticket \S+, lane \S+", re.MULTILINE)

# (issue, criterion number, "open" | "ticked", text): the shape
# `linked_issue.rendered_criteria_with_state` returns.
LinkedBox = tuple[int, int, str, str]


def is_lane_pr(pr_body: str) -> bool:
    """The body carries a Loom lane footer."""
    return bool(_LANE_FOOTER_RE.search(pr_body or ""))


def criteria_in_scope(
    boxes: Iterable[LinkedBox], pr_body: str,
) -> tuple[LinkedCriterion, ...]:
    """The linked criteria this PR must meet.

    Those of an issue the PR closes; every linked criterion of a lane PR,
    whose ticket the station closes on merge; and, on any other PR, an open
    box whose text the body repeats or names by number (a claim)."""
    body = pr_body or ""
    boxes = tuple(boxes)
    closed = set(closes_refs(body))
    lane = is_lane_pr(body)
    issues = {b[0] for b in boxes}
    single = next(iter(issues)) if len(issues) == 1 else None
    return tuple(
        LinkedCriterion(issue, number, text)
        for issue, number, state, text in boxes
        if issue in closed or lane
        or (state == "open" and body_claims_box((issue, number, state, text), body, single))
    )


def _open_only(
    criteria: tuple[LinkedCriterion, ...], boxes: tuple[LinkedBox, ...],
) -> tuple[LinkedCriterion, ...]:
    """The criteria whose box is still open on the issue."""
    ticked = {(b[0], b[1]) for b in boxes if b[2] != "open"}
    return tuple(c for c in criteria if (c.issue, c.number) not in ticked)


def lane_work_findings(
    *,
    diff_text: str,
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str],
    base_line_counts: dict[str, int],
    boxes: Iterable[LinkedBox],
    pr_title: str,
    pr_body: str,
    changes: tuple[FileChange, ...] | None = None,
    health_issues: Iterable[int] = (),
    criteria_judge: CriteriaJudge | None = None,
) -> tuple[Finding, ...]:
    """Every deterministic lane-work finding for one PR.

    `base_line_counts`: line count of each changed file at the base, by
    path (the old path for a rename); a file missing from it is skipped by
    the destructive-edit check, never guessed at. `changes` overrides the
    per-file statuses read from `diff_text` (the corpus runner passes the
    full counts of a diff it stores truncated). `health_issues`: linked
    issues that are health findings (`linked_issue.rendered_health_issues`);
    without it a PR title starting `Health:` marks one. `criteria_judge`
    decides the criteria the partial-delivery mapping cannot (#1389)."""
    if changes is None:
        changes = file_changes(diff_text)
    boxes = tuple(boxes)
    criteria = criteria_in_scope(boxes, pr_body)
    findings = list(scan_criteria_coverage(
        hunks, file_contents, criteria=criteria,
        pr_title=pr_title, pr_body=pr_body,
        new_files=frozenset(c.path for c in changes if c.status == "added"),
        removed_files=frozenset(c.path for c in changes if c.status == "removed"),
        renamed=tuple(
            (c.previous_path, c.path) for c in changes
            if c.status == "renamed" and c.previous_path
        ),
    ))
    findings.extend(scan_destructive_edits(
        changes, base_line_counts, criteria=criteria,
        pr_title=pr_title, hunks=hunks,
    ))
    findings.extend(scan_weakened_verification(
        changes, hunks, criteria=criteria, pr_title=pr_title,
    ))
    findings.extend(scan_fabricated_results(changes, hunks))
    findings.extend(scan_unbacked_claims(pr_body, criteria=criteria, hunks=hunks))
    # A ticked box was delivered by an earlier slice; only open boxes can
    # name what this diff should have changed.
    findings.extend(scan_wrong_target(
        changes, hunks, criteria=_open_only(criteria, boxes), pr_title=pr_title,
    ))
    findings.extend(scan_measurement_only(
        changes, hunks, criteria=criteria, pr_title=pr_title, health_issues=health_issues,
    ))
    # Criteria coverage already reports every runtime criterion unmet when
    # nothing in the diff runs; partial delivery would say it again.
    if not any(f.rule_name == "intent-mismatch" and f.severity == "high" for f in findings):
        findings.extend(scan_partial_delivery(
            _open_only(criteria, boxes), changes, hunks, judge=criteria_judge,
        ))
    doa = scan_dead_on_arrival(
        changes, hunks, pr_body=pr_body, prior=tuple(findings),
        file_contents=file_contents, lane=is_lane_pr(pr_body),
    )
    return (*drop_superseded(findings, doa), *doa)
