"""Regression tests for #49 — issue-link regex must accept the legacy
gate's full vocabulary, not just closing keywords + Part of."""

from __future__ import annotations

import pytest

from personas.tpm.dor_checks import check_issue_link


@pytest.mark.parametrize(
    "body",
    [
        "closes #42",
        "Closes #42",
        "fixes #42",
        "Resolves #42",
        "Part of #42",
        "Refs #42",
        "refs #42",
        "Relates to #42",
        "Blocked by #42",
        "blocked by #42",
        "#42",  # bare line
        "  #42  ",  # bare with leading whitespace
    ],
)
def test_accepted_link_forms(body):
    assert check_issue_link(body).passed, f"should accept: {body!r}"


@pytest.mark.parametrize(
    "body",
    [
        "no link here",
        "see #foo",
        "issue 42",
        "see PR42",
    ],
)
def test_rejected_bodies(body):
    assert not check_issue_link(body).passed, f"should reject: {body!r}"
