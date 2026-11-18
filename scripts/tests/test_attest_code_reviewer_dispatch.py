"""Verify the dispatch attester follows the public entry point."""

import ast
import runpy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ATTESTER = REPO / "infra/scripts/attest_code_reviewer_dispatch.py"
DISPATCH = REPO / "services/_shared/personas/code_reviewer/dispatch.py"
CHECK_DISPATCH = runpy.run_path(str(ATTESTER))["_check_dispatch"]


def test_dispatch_attestation_accepts_wrapped_review_pass():
    assert CHECK_DISPATCH(ast.parse(DISPATCH.read_text()), DISPATCH) == []


@pytest.mark.parametrize(
    ("original", "replacement", "expected"),
    [
        ("return _review_pass(", "return _other_pass(", "return _review_pass"),
        ("payload, blocking=blocking,", "payload, blocking=False,", "forward blocking"),
        (
            'mode: ReviewMode = "blocking" if blocking else "advisory"',
            'mode: ReviewMode = "advisory"',
            "mode` not derived",
        ),
    ],
)
def test_dispatch_attestation_rejects_broken_delegation_or_mode(original, replacement, expected):
    source = DISPATCH.read_text()
    assert source.count(original) == 1
    failures = CHECK_DISPATCH(ast.parse(source.replace(original, replacement)), DISPATCH)
    assert any(expected in failure for failure in failures), failures
