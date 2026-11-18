import pytest

from personas.code_reviewer.criteria_coverage import _is_runtime_criterion
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.lane_work import lane_work_findings

_DECISION_CRITERIA = (
    "A written decision names the repository, the OIDC subject anchor and the log exposure.",
    "Proof line: build tickets for the chosen path exist under this epic, "
    "or the decision records the Mac cap for private repos.",
)


@pytest.mark.parametrize(
    "criterion",
    (
        *_DECISION_CRITERIA,
        "The maintainers explain the outcome in a decision record.",
        "The team records the decision with its rationale.",
    ),
)
def test_written_decision_criteria_do_not_require_runtime_behavior(criterion: str):
    assert not _is_runtime_criterion(criterion)


def test_decision_criterion_with_a_separate_runtime_clause_still_requires_code():
    assert _is_runtime_criterion(
        "The written decision names the cap, and the scheduler rejects jobs above it."
    )


def test_docs_only_decision_diff_has_no_intent_mismatch():
    path = "docs/runner-choice.md"
    diff = f"""\
diff --git a/{path} b/{path}
new file mode 100644
--- /dev/null
+++ b/{path}
@@ -0,0 +1,3 @@
+# Runner choice
+Use the example repository, anchor the OIDC subject to its repository ID, and keep logs private.
+Cap private repositories at one Mac runner each.
"""
    findings = lane_work_findings(
        diff_text=diff,
        hunks=parse_diff(diff),
        file_contents={},
        base_line_counts={},
        boxes=tuple(
            (1717, number, "open", criterion)
            for number, criterion in enumerate(_DECISION_CRITERIA, 1)
        ),
        pr_title="Decide how to run private builds",
        pr_body="Refs #1717\n\nLoom ticket example-1717, lane codex.\n",
    )

    assert "intent-mismatch" not in {finding.rule_name for finding in findings}
