"""Tests for static DoR checks.

Critical regression: closes #20 — empty `- [ ]` placeholders must NOT
count as filled bullets (security: unfilled template should NOT pass).
"""

from __future__ import annotations

from personas.tpm.dor_checks import (
    check_acceptance,
    check_estimate,
    check_issue_link,
    check_scope_fence,
    check_why,
    run_all,
)


def test_why_passes_with_5_words():
    body = "## Why\nWe need this for the launch tomorrow morning"
    assert check_why(body).passed


def test_why_fails_under_5_words():
    body = "## Why\ntoo short"
    r = check_why(body)
    assert not r.passed and "2 words" in r.detail


def test_why_missing_section():
    assert not check_why("nothing").passed


def test_why_falls_back_to_summary():
    body = "## Summary\nthis is a longer summary line"
    assert check_why(body).passed


def test_acceptance_three_filled_bullets_passes():
    body = "## Acceptance criteria\n- [x] one\n- [x] two\n- [x] three"
    assert check_acceptance(body).passed


def test_acceptance_empty_placeholders_reject_closes_20():
    """The bug from #20: `- [ ]` empty checkboxes must not count."""
    body = "## Acceptance criteria\n- [ ]\n- [ ]\n- [ ]"
    assert not check_acceptance(body).passed


def test_acceptance_mixed_empty_and_filled():
    body = "## Acceptance criteria\n- [x] real\n- [ ]\n- [ ]"
    r = check_acceptance(body)
    assert not r.passed and "1 non-empty" in r.detail


def test_acceptance_falls_back_to_test_plan():
    body = "## Test plan\n- a\n- b\n- c"
    assert check_acceptance(body).passed


def test_estimate_pass():
    assert check_estimate("Size: M somewhere in body").passed


def test_estimate_xl_fails():
    r = check_estimate("Size: XL")
    assert not r.passed and "split" in r.detail.lower()


def test_estimate_missing():
    assert not check_estimate("no size here").passed


def test_scope_fence_pass():
    assert check_scope_fence("## Out of scope\nstuff").passed


def test_scope_fence_missing():
    assert not check_scope_fence("nothing").passed


def test_issue_link_variants():
    for kw in ["closes", "Fixes", "Resolves", "Part of"]:
        assert check_issue_link(f"{kw} #42").passed


def test_issue_link_missing():
    assert not check_issue_link("just text").passed


def test_run_all_returns_5():
    results = run_all("")
    assert len(results) == 5
    assert {r.name for r in results} == {
        "why", "acceptance", "estimate", "scope-fence", "issue-link",
    }
