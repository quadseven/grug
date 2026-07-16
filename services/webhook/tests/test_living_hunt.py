"""Living Hunt (#557): last-reviewed head + delta scope."""

from __future__ import annotations

import httpx

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


def test_clean_living_hunt_names_delta_in_title_and_scope():
    ev = CodeReviewEvaluation(findings=(), conclusion="success")
    title, summary = cr_dispatch._summary_markdown(
        ev, living_range="abc12345..def67890",
    )
    assert title.startswith("Living Hunt abc12345..def67890 - ")
    assert "walked the delta diff" in summary
    assert "walked the whole diff" not in summary


def test_degraded_living_hunt_names_delta_in_title():
    ev = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="timeout",
    )
    title, _ = cr_dispatch._summary_markdown(
        ev, living_range="abc12345..def67890",
    )
    assert title.startswith("Living Hunt abc12345..def67890 - ")


def test_fetch_pr_diff_scope_reports_full_fallback(monkeypatch):
    responses = [
        httpx.Response(422, request=httpx.Request("GET", "https://compare")),
        httpx.Response(
            200, text="full diff",
            request=httpx.Request("GET", "https://pull"),
        ),
    ]
    requested_urls: list[str] = []

    def fake_get(url: str, **_kwargs):
        requested_urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(
        cr_dispatch.httpx, "get", fake_get,
    )

    diff, used_compare = cr_dispatch._fetch_pr_diff_with_scope(
        "token", "owner", "repo", 7, base_sha="abc", head_sha="def",
    )

    assert diff == "full diff"
    assert used_compare is False
    assert requested_urls == [
        "https://api.github.com/repos/owner/repo/compare/abc...def",
        "https://api.github.com/repos/owner/repo/pulls/7",
    ]


def test_elder_last_sk_is_stable():
    from adapters.pg_install_store import _elder_last_sk

    assert _elder_last_sk("o/r", 7) == "ELDER#LAST#o/r#7"
