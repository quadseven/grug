"""Tests for a future post-merge CI observation as a linked criterion (#1509).

Recorded shape (anonymized): a lane PR fixed a flaky test that failed on
the day before a clock change. Its diff touched only the test file. The
ticket's last criterion read "Live: the next default-branch
`unittest-linux` run after the merge concludes success." Elder raised a
blocking `intent-mismatch`: "every non-test change is docs, imports or
comments, so the runtime behavior these criteria describe cannot happen".
That run cannot exist before the PR merges, so no diff could ever meet it.

The rules:
- a future post-merge CI observation is deferred verification, not a
  missing hunk: it draws no blocking intent-mismatch on its own;
- an unmet runtime criterion beside it still draws the finding;
- a live host-state criterion with no managing change still draws the
  #1364 finding;
- a PR body that ticks the future run as already passed is flagged, not
  accepted, and an LLM finding about it stays visible as outstanding.
"""

from __future__ import annotations

from personas.code_reviewer.criteria_coverage import is_future_post_merge_observation
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.intent_filter import scope_intent_mismatch_to_claimed_boxes
from personas.code_reviewer.lane_work import lane_work_findings
from personas.code_reviewer.persona import Finding

_FUTURE_CI = "Live: the next default-branch `unittest-linux` run after the merge concludes success."
_FOOTER = (
    "\n---\nLoom ticket example.app-50-r1, lane codex, model example-model.\n"
    "Refs example/app#50\n"  # leak-guard-allow: placeholder repo
)
_TITLE = "The simulated report day passes on the day before a clock change"
_BODY = "## Why\nThe report test failed on the day before a clock change.\n" + _FOOTER
_TEST_BOX = (50, 1, "open", "A test pins the report day on the day before a clock change.")
_FUTURE_BOX = (50, 2, "open", _FUTURE_CI)

_TEST_DIFF = (
    "diff --git a/tests/test_report.py b/tests/test_report.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/tests/test_report.py\n+++ b/tests/test_report.py\n"
    "@@ -10,2 +10,3 @@\n"
    " def test_report_day():\n"
    "-    now = datetime(2026, 3, 7, 23, 30)\n"
    "+    now = datetime(2026, 3, 7, 23, 30, tzinfo=ZONE)\n"
    "+    assert report_day(now) == date(2026, 3, 7)\n"
)


def _findings(diff: str = _TEST_DIFF, *, boxes, body: str = _BODY, contents=None):
    return lane_work_findings(
        diff_text=diff, hunks=parse_diff(diff), file_contents=contents or {},
        base_line_counts={}, boxes=boxes, pr_title=_TITLE, pr_body=body,
    )


def _rules(findings) -> list[str]:
    return sorted(f.rule_name for f in findings)


# --- the classifier ------------------------------------------------------------


def test_classifier_names_a_future_ci_run_after_the_merge():
    assert is_future_post_merge_observation(_FUTURE_CI)
    assert is_future_post_merge_observation(
        "The first `build` workflow on main after merging passes.",
    )
    assert is_future_post_merge_observation(
        "Live: the next default-branch `lint` job is green.",
    )


def test_classifier_leaves_host_state_runtime_and_repo_state_alone():
    assert not is_future_post_merge_observation(
        "Live: after the merge, port 443 on each host is reachable and the run passes.",
    )
    assert not is_future_post_merge_observation(
        "Live: after merge, `git ls-files '*.tmp'` on main prints nothing",
    )
    assert not is_future_post_merge_observation(
        "Live: a shipped ticket gets a verify run within an hour",
    )
    assert not is_future_post_merge_observation(
        "The station applies the proof a verify run returns to the ticket",
    )


# --- the recorded pull request ----------------------------------------------------


def test_test_only_fix_with_a_future_ci_criterion_draws_no_intent_mismatch():
    findings = _findings(boxes=(_TEST_BOX, _FUTURE_BOX))
    assert "intent-mismatch" not in _rules(findings), findings


def test_an_unmet_runtime_criterion_beside_it_still_draws_the_finding():
    runtime = (50, 3, "open", "The report job sends the daily report at the local report hour.")
    findings = _findings(boxes=(_TEST_BOX, _FUTURE_BOX, runtime))
    mismatch = [f for f in findings if f.rule_name == "intent-mismatch"]
    assert len(mismatch) == 1 and mismatch[0].severity == "high"
    assert runtime[3] in mismatch[0].message
    assert _FUTURE_CI not in mismatch[0].message


def test_a_live_host_state_criterion_with_no_managing_change_still_raises():
    script = ["#!/usr/bin/env python3", "print('check')"]
    diff = _TEST_DIFF + (
        "diff --git a/ops/check-hosts.py b/ops/check-hosts.py\nnew file mode 100644\n"
        "index 0000000..3333333\n--- /dev/null\n+++ b/ops/check-hosts.py\n"
        f"@@ -0,0 +1,{len(script)} @@\n" + "".join(f"+{ln}\n" for ln in script)
        + "diff --git a/README.md b/README.md\nindex 4444444..5555555 100644\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # App\n"
        "+Run `python3 ops/check-hosts.py` to check the hosts.\n"
    )
    host = (50, 3, "open", "Port 8443 on each host is closed to the guest network.")
    findings = _findings(
        diff, boxes=(_FUTURE_BOX, host),
        contents={"ops/check-hosts.py": "\n".join(script) + "\n"},
    )
    mismatch = [f for f in findings if f.rule_name == "intent-mismatch"]
    assert len(mismatch) == 1 and mismatch[0].severity == "high"
    assert "live host, network or deploy state" in mismatch[0].message
    assert host[3] in mismatch[0].message


# --- a claim that the future run already passed -------------------------------------


def test_a_body_ticking_the_future_run_as_passed_is_flagged():
    body = _BODY.replace(
        "## Why", f"- [x] {_FUTURE_CI} -- passed, https://ci.example.test/runs/7\n## Why",
    )
    findings = _findings(boxes=(_TEST_BOX, _FUTURE_BOX), body=body)
    claims = [f for f in findings if f.rule_name == "unbacked-claim"]
    assert len(claims) == 1, findings
    assert "#50 criterion 2" in claims[0].message
    assert "after the merge" in claims[0].message


def test_an_llm_finding_on_only_the_future_box_is_advisory_and_outstanding():
    finding = Finding(
        file="tests/test_report.py", line=12, severity="high", rule_name="intent-mismatch",
        message=(
            f'#50 criterion 2 asks "{_FUTURE_CI}", but the PR only says '
            '"the post-merge unittest-linux run already passed".'
        ),
        suggestion=None,
    )
    body = "Closes #50\n\n- [x] the post-merge unittest-linux run already passed"
    kept, dropped, lowered = scope_intent_mismatch_to_claimed_boxes(
        [finding], boxes=[_TEST_BOX, _FUTURE_BOX], closed_issues=[50], pr_body=body,
    )
    assert (dropped, lowered) == (0, 1)
    assert kept[0].severity == "medium"
    assert "outstanding" in kept[0].message


def test_an_llm_finding_on_a_code_box_beside_the_future_box_still_blocks():
    finding = Finding(
        file="tests/test_report.py", line=12, severity="high", rule_name="intent-mismatch",
        message=(
            f'#50 criterion 1 asks "{_TEST_BOX[3]}", but the PR only says '
            '"the report day uses the local zone".'
        ),
        suggestion=None,
    )
    kept, _dropped, lowered = scope_intent_mismatch_to_claimed_boxes(
        [finding], boxes=[_TEST_BOX, _FUTURE_BOX], closed_issues=[50], pr_body="Closes #50",
    )
    assert lowered == 0 and kept[0].severity == "high"
