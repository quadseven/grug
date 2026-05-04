"""Regression test for #45 — H3 inside ## section must not truncate.

Mirrored from services/webhook/tests/test_section_h3_no_truncate.py.
The api Lambda also imports `personas.tpm.dor_checks` for self-check
endpoints, so the same regex fix needs the same coverage. Greptile P2
on PR #58.
"""

from __future__ import annotations

from personas.tpm.dor_checks import check_acceptance, check_why


def test_acceptance_with_h3_subsections_passes():
    body = """## Acceptance criteria

### Per-feature group A
- [x] one
- [x] two

### Per-feature group B
- [x] three
- [x] four

## Test plan
"""
    r = check_acceptance(body)
    assert r.passed, f"failed: {r.detail}"


def test_acceptance_with_h4_subsections_passes():
    body = """## Acceptance criteria

#### deeper
- [x] one
- [x] two
- [x] three
"""
    assert check_acceptance(body).passed


def test_why_with_h3_inside_passes():
    body = """## Why
We need this for the launch tomorrow morning.

### Background context
some prose
"""
    assert check_why(body).passed


def test_h3_only_section_does_not_satisfy_h2_requirement():
    """Sanity: H3-only `### Why` should NOT count as `## Why`."""
    body = "### Why\nplenty of words here for the why"
    assert not check_why(body).passed
