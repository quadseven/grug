"""Linked-issue guidance for criteria that require test evidence."""

from personas.code_reviewer.linked_issue import build_linked_issue_context


def test_header_distinguishes_existing_coverage_from_required_test_changes():
    block = build_linked_issue_context(
        "Refs #42",
        fetch_issue=lambda _: "## Acceptance criteria\n- The retry test fails on main.",
    )

    assert block is not None
    assert "fails on main/without the change" in block
    assert (
        "is `acceptance-criterion-unmet` when no added or changed test in the diff exercises it."
        in block
    )
    assert (
        "only when the PR body names no existing test for it and "
        "no added or changed test in the diff exercises it"
        in block
    )


def test_header_exempts_tested_criterion_when_pr_names_existing_test():
    block = build_linked_issue_context(
        "Refs #42\n\nExisting coverage: `tests/test_queue.py::test_retries`.",
        fetch_issue=lambda _: "## Acceptance criteria\n- The queue retries (tests).",
    )

    assert block is not None
    header, _, criteria = block.partition("\n#42 criterion 1 [open]: ")
    assert criteria == "The queue retries (tests)."
    assert "only says a behavior is tested ('(test)', '(tests)', or 'tests show')" in header
    assert (
        "when the PR body names an existing test for such a criterion, "
        "do not flag it as unmet."
        in header
    )
