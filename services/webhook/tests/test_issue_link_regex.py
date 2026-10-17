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


@pytest.mark.parametrize(
    "body",
    [
        "Refs owner/name#42",
        "refs Owner/Name#42",
        "REFS my-org/my.repo_2#42",
        "Closes owner/name#42",
        "Part of owner/name#42",
    ],
)
def test_accepts_qualified_same_or_cross_repo_ref(body):
    """A `Refs owner/name#N` link names its ticket as well as `Refs #N` does
    (#1214). Lane PRs link this way, and some operators never use a closing
    keyword because a ticket closes only after its boxes are ticked."""
    assert check_issue_link(body).passed, f"should accept: {body!r}"


@pytest.mark.parametrize("body", ["Refs owner/name", "Refs owner/#42", "Refs /name#42"])
def test_rejects_malformed_qualified_ref(body):
    assert not check_issue_link(body).passed, f"should reject: {body!r}"


def test_failure_message_names_the_forms_that_count():
    detail = check_issue_link("no link here").detail
    for form in ("Refs #N", "Refs owner/name#N", "closes #N"):
        assert form in detail, f"{form!r} missing from: {detail!r}"
