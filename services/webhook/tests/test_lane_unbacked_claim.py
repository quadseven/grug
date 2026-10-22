"""Tests for the unbacked-claim check (#1371).

Recorded shapes (anonymized): lane PR bodies ticked "Verify the service
account is denied get, list and create in the runner namespaces" and
"Verify an over-budget pod is rejected with a quota error" for a script no
workflow runs; ticked "maps a route with MEASURED live traffic, evidenced
in the PR with the query and the number" and appended a query with no
number; ticked "Record the live-verification outcome in the issue" with
nothing recorded.
"""

from __future__ import annotations

import time

from personas.code_reviewer import linked_issue
from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.lane_work import lane_work_findings
from personas.code_reviewer.unbacked_claim import (
    MAX_BODY_CHARS,
    RULE,
    scan_unbacked_claims,
)

_FOOTER = "\n---\nLoom ticket example.iac-83-r3, lane codex, model m.\nRefs #83\n"
_C1 = "Verify the runner service account is denied get, list, and create in the runner namespaces."
_C2 = "Verify an over-budget pod is rejected with a quota error."
_C3 = "A test fails without the guard and passes with it."
_CRITERIA = (
    LinkedCriterion(83, 1, _C1),
    LinkedCriterion(83, 2, _C2),
    LinkedCriterion(83, 3, _C3),
)
_DIFF = (
    "diff --git a/tests/test_isolation.py b/tests/test_isolation.py\n"
    "new file mode 100644\nindex 0000000..1111111\n"
    "--- /dev/null\n+++ b/tests/test_isolation.py\n@@ -0,0 +1,2 @@\n"
    "+def test_guard():\n+    assert True\n"
    "diff --git a/scripts/check_isolation.sh b/scripts/check_isolation.sh\n"
    "new file mode 100755\nindex 0000000..2222222\n"
    "--- /dev/null\n+++ b/scripts/check_isolation.sh\n@@ -0,0 +1,2 @@\n"
    "+#!/bin/sh\n+kubectl auth can-i get pods --as \"$SA\" -n runners\n"
)
_HUNKS = parse_diff(_DIFF)


def _body(*boxes: str, extra: str = "") -> str:
    ticks = "".join(f"- [x] {b}\n" for b in boxes)
    return f"## Why\nIsolate the runners.\n\n## Acceptance criteria\n{ticks}{extra}{_FOOTER}"


def _scan(body: str, criteria=_CRITERIA):
    return scan_unbacked_claims(body, criteria=criteria, hunks=_HUNKS)


def test_the_recorded_ticked_live_checks_are_one_medium_finding_naming_each_box():
    findings = _scan(_body(_C1, _C2, _C3))
    assert len(findings) == 1, findings
    f = findings[0]
    assert (f.rule_name, f.severity) == (RULE, "medium")
    # Anchored on the first added line of a non-test file.
    assert (f.file, f.line) == ("scripts/check_isolation.sh", 1)
    assert "ticks 2 boxes" in f.message
    assert f'#83 criterion 1: "{_C1}"' in f.message
    assert f'#83 criterion 2: "{_C2}"' in f.message
    assert "criterion 3" not in f.message


def test_a_box_with_a_number_a_link_or_output_in_the_body_is_backed():
    numbered = _body(f"{_C2} -- `kubectl apply` returned 1 error: exceeded quota")
    assert _scan(numbered) == ()
    linked = _body(f"{_C2} -- see https://example.com/logs/quota-check")
    assert _scan(linked) == ()
    run = _body(_C1, _C2, extra="\nRun: https://github.com/example/iac/actions/runs/123456\n")
    assert _scan(run) == ()
    fenced = _body(_C1, _C2, extra="\n```\n$ kubectl auth can-i get pods\nno\n```\n")
    assert _scan(fenced) == ()


def test_a_number_in_the_criterion_or_a_code_span_is_not_evidence():
    measured = "the write SLI maps a route with MEASURED live traffic, evidenced with the number."
    criteria = (LinkedCriterion(7, 1, measured),)
    body = _body(f"{measured} -- query `sum:app.sli.event{{class:write}}.as_count()` confirms traffic")
    findings = _scan(body, criteria)
    assert len(findings) == 1 and "#7 criterion 1" in findings[0].message
    ported = "Ports 80 and 443 on the hosts are closed."
    assert len(_scan(_body(f"{ported} -- checked `nmap -p 80,443`"),
                     (LinkedCriterion(7, 1, ported),))) == 1


def test_unticked_test_and_code_behavior_boxes_are_fine():
    assert _scan(_body() + f"- [ ] {_C1}\n") == ()
    behavior = (
        "A lane whose runner holds its lock with a live pid is not reported silent.",
        "Compares the deployed reference against the published one.",
        "The click is computed from the measured position.",
    )
    assert _scan(_body(*behavior), ()) == ()


def test_a_box_no_criterion_matches_is_still_read_on_its_own():
    assert len(_scan(_body("Record the live-verification outcome in the issue."), ())) == 1
    backed = _body("Measured live: 77 events over 7 days on the ingest route.")
    assert _scan(backed, ()) == ()


def test_lane_work_findings_reports_the_unbacked_claim_from_in_scope_criteria():
    boxes = [(83, c.number, "open", c.text) for c in _CRITERIA]
    findings = lane_work_findings(
        diff_text=_DIFF, hunks=_HUNKS, file_contents={}, base_line_counts={},
        boxes=boxes, pr_title="Isolate the runner namespaces",
        pr_body=_body(_C1, _C2, _C3),
    )
    unbacked = [f for f in findings if f.rule_name == RULE]
    assert len(unbacked) == 1 and unbacked[0].severity == "medium"
    assert "#83 criterion 1" in unbacked[0].message


def test_the_rule_shows_under_a_named_category_not_general():
    assert linked_issue.RULE_CATEGORIES[RULE] == "requirements"


def test_the_scan_is_linear_and_past_its_bounds_is_not_evaluated():
    criteria = tuple(LinkedCriterion(9, n, f"Verify host {n} answers on the live port.")
                     for n in range(1, 151))
    body = _body(*(f"Verify host {n} answers on the live port." + " x" * 400
                   for n in range(1, 151)))
    start = time.perf_counter()
    scan_unbacked_claims(body, criteria=criteria, hunks=_HUNKS)
    assert time.perf_counter() - start < 1.0
    # A ticked line of whitespace runs does not backtrack.
    spaced = _body("Verify on the hosts" + " " * 50_000 + "done")
    start = time.perf_counter()
    _scan(spaced)
    assert time.perf_counter() - start < 1.0
    assert _scan(_body(_C1) + " " * MAX_BODY_CHARS) == ()
    assert _scan(_body(*([_C1] * 201))) == ()
