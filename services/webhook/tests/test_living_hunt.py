"""Living Hunt (#557): last-reviewed head + delta scope."""

from __future__ import annotations

from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding


def test_summary_markdown_includes_living_range():
    ev = CodeReviewEvaluation(
        findings=(
            Finding(
                file="x.py", line=1, severity="high", rule_name="null-deref",
                message="m", suggestion=None,
            ),
        ),
        conclusion="failure",
    )
    title, summary = cr_dispatch._summary_markdown(
        ev, living_range="abc12345..def67890",
    )
    assert "Living Hunt abc12345..def67890" in title
    assert "Living Hunt: reviewing `abc12345..def67890`" in summary


def test_summary_markdown_omits_living_range_when_empty():
    ev = CodeReviewEvaluation(findings=(), conclusion="success")
    title, summary = cr_dispatch._summary_markdown(ev)
    assert "Living Hunt" not in title
    assert "Living Hunt" not in summary


def test_elder_last_sk_is_stable():
    from adapters.pg_install_store import _elder_last_sk

    assert _elder_last_sk("o/r", 7) == "ELDER#LAST#o/r#7"
