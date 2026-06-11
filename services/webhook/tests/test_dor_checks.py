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


# external-review P2 on PR #40 — error msg must reference the section the user
# actually used, not always say "Acceptance criteria".
def test_acceptance_error_msg_says_test_plan_when_thats_what_user_used():
    body = "## Test plan\n- only one"
    r = check_acceptance(body)
    assert not r.passed
    assert "Test plan" in r.detail
    assert "Acceptance criteria has" not in r.detail


def test_acceptance_error_msg_says_acceptance_criteria_when_used():
    body = "## Acceptance criteria\n- only one"
    r = check_acceptance(body)
    assert not r.passed
    assert "Acceptance criteria" in r.detail


def test_estimate_pass():
    assert check_estimate("Size: M somewhere in body").passed


def test_estimate_xl_fails():
    r = check_estimate("Size: XL")
    assert not r.passed and "split" in r.detail.lower()


def test_estimate_missing():
    assert not check_estimate("no size here").passed


# Sentry MED on PR #40 — _SIZE_PAT must require Size: prefix, NOT match
# bare letters in random prose.
def test_estimate_rejects_bare_letter_in_prose():
    """`M&Ms` / `the M key` / `XL t-shirts` must NOT satisfy estimate."""
    for body in [
        "use the M key",
        "lots of M&Ms",
        "XL t-shirts on sale",
        "sentence with L in it",
        "Size is fine but no value supplied",  # `Size` alone w/o letter
    ]:
        assert not check_estimate(body).passed, f"falsely accepted: {body!r}"


def test_estimate_accepts_explicit_size_prefix_variants():
    for body in [
        "Size: M",
        "Size:M",
        "Size M",
        "**Size:** S",
        "size: l",  # lowercase
    ]:
        assert check_estimate(body).passed, f"should accept: {body!r}"


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
