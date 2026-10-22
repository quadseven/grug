"""Tests for the measurement-only lane-work check (#1370).

Recorded shape (anonymized): a health finding reported a guild bank under
a gold while its members held under a gold each. The lane PR changed only
the health endpoint, adding gold a second account carried to the measured
balance, so the finding went quiet while the members stayed broke.
"""

from __future__ import annotations

from personas.code_reviewer import measurement_only
from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.lane_work import lane_work_findings
from personas.code_reviewer.linked_issue import (
    build_linked_issue_context,
    rendered_health_issues,
)
from personas.code_reviewer.measurement_only import (
    is_measurement_path,
    scan_measurement_only,
)

_FOOTER = "\n---\nLoom ticket example.game-27-r1, lane codex, model gpt-6-luna.\nRefs example/game#27\n"  # leak-guard-allow: placeholder repo
_TITLE = "Health: The guild bank holds under a gold"
_INTAKE_BOXES = (
    (27, 1, "open", "The pull request names the cause with the numbers that show it."),
    (27, 2, "open", "A test fails without the fix and passes with it."),
    (27, 3, "open", "Live: the finding is absent from the health endpoint for 24 hours."),
)
_HEALTH_OLD = ["def bank_findings(banks):", "    for b in banks:"]
_HEALTH_NEW = ["        bank = int(b['bank']) + int(b.get('carried') or 0)"]
_TEST = ["def test_carried_gold_counts():", "    assert bank_findings([]) == []"]


def _changed(path: str, old: list[str], new: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\nindex 2222222..3333333 100644\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1,{len(old)} +1,{len(old) + len(new)} @@\n"
        + "".join(f" {ln}\n" for ln in old)
        + "".join(f"+{ln}\n" for ln in new)
    )


def _recorded_diff() -> str:
    return _changed("api/health.py", _HEALTH_OLD, _HEALTH_NEW) + _changed(
        "tests/test_health.py", ["import health"], _TEST,
    )


def _acting_diff() -> str:
    return _changed("guildfund.py", ["def refunds(bank):"], ["    pay_waiting_members(bank)"])


def _findings(diff: str, *, boxes=_INTAKE_BOXES, title=_TITLE, health_issues=()):
    return lane_work_findings(
        diff_text=diff, hunks=parse_diff(diff), file_contents={}, base_line_counts={},
        boxes=boxes, pr_title=title, pr_body="## Why\nHealth intake.\n" + _FOOTER,
        health_issues=health_issues,
    )


def _scan(diff: str, texts: list[str], *, title=_TITLE, health_issues=()):
    criteria = tuple(LinkedCriterion(27, n, t) for n, t in enumerate(texts, start=1))
    return scan_measurement_only(
        file_changes(diff), parse_diff(diff), criteria=criteria,
        pr_title=title, health_issues=health_issues,
    )


def test_recorded_health_fix_that_changes_only_the_measurement_is_high():
    found = _findings(_recorded_diff())
    assert [(f.rule_name, f.severity) for f in found] == [("symptom-only", "high")]
    assert found[0].file == "api/health.py"
    assert found[0].line in parse_diff(_recorded_diff())[0].new_lines
    assert "#27" in found[0].message and "`api/health.py`" in found[0].message


def test_a_health_fix_in_the_acting_code_gets_no_finding():
    assert _findings(_acting_diff()) == ()
    both = _recorded_diff() + _acting_diff()
    assert _findings(both) == ()


def test_a_ticket_that_is_not_a_health_finding_gets_no_finding():
    title = "The health endpoint reports a family repeating a stalled dungeon"
    assert _scan(_recorded_diff(), ["The finding names the map."], title=title) == ()


def test_a_health_ticket_that_asks_for_the_measurement_gets_no_finding():
    asks = ["The health endpoint counts a follower as stuck only when its leader is."]
    assert _scan(_recorded_diff(), asks) == ()
    names_file = ["health.py reads the bank from the ledger, not the vault."]
    assert _scan(_recorded_diff(), names_file) == ()


def test_health_issues_mark_a_health_ticket_whatever_the_title():
    texts = [b[3] for b in _INTAKE_BOXES]
    title = "The guild bank holds under a gold"
    assert _scan(_recorded_diff(), texts, title=title) == ()
    assert len(_scan(_recorded_diff(), texts, title=title, health_issues={27})) == 1
    # A health issue the PR does not answer does not count.
    assert _scan(_recorded_diff(), texts, title=title, health_issues={99}) == ()


def test_docs_and_tests_alone_are_not_a_finding():
    diff = _changed("docs/HEALTH.md", ["# Health"], ["More words."]) + _changed(
        "tests/test_health.py", ["import health"], _TEST,
    )
    assert _scan(diff, [b[3] for b in _INTAKE_BOXES]) == ()


def test_measurement_paths():
    for path in ("api/health.py", "apiv2/health.py", "ops/monitors/bank.yaml",
                 "metrics.go", "k8s/probe-config.yaml", "src/alerting/rules.py"):
        assert is_measurement_path(path), path
    for path in ("guildfund.py", "bridge.py", "apiv2/members.py", "healthy_food.py"):
        assert not is_measurement_path(path), path


def test_past_the_file_bound_is_not_evaluated(monkeypatch):
    monkeypatch.setattr(measurement_only, "MAX_FILES", 1)
    assert _scan(_recorded_diff(), [b[3] for b in _INTAKE_BOXES]) == ()


def test_the_rendered_health_block_names_its_issues():
    issues = {
        27: {"title": "Health: The guild bank holds under a gold",
             "body": "> bank 0c, 65 members under a gold\n\n- [ ] A test fails without the fix."},
        31: {"title": "Add a ledger", "body": "- [ ] The ledger lists each refund."},
    }
    block = build_linked_issue_context("Refs #27\nRefs #31", fetch_issue=issues.__getitem__)
    assert rendered_health_issues(block) == {27}
    assert rendered_health_issues("") == frozenset()
    assert rendered_health_issues("#27 quoted evidence: no heading") == frozenset()
