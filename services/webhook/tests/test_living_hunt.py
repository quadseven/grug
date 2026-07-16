"""Living Hunt (#557): last-reviewed head + delta scope."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx

from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import _changed_line_text


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


def test_changed_line_text_empty_body_returns_none():
    """No AttributeError when body is empty/missing (high null-deref path)."""
    header_only = DiffHunk(
        file_path="a.yml",
        new_start=1,
        new_lines=frozenset({1}),
        body="@@ -0,0 +1 @@\n",
    )
    # Valid header-only body: no new-side content at line 1 after header.
    assert _changed_line_text(header_only, 1) is None
    # Synthetic object with body=None must not raise.
    synthetic = MagicMock(spec=["body", "new_start"])
    synthetic.body = None
    synthetic.new_start = 1
    assert _changed_line_text(synthetic, 1) is None


def test_put_elder_last_reviewed_upserts_with_90d_ttl(monkeypatch):
    """put_elder_last_reviewed writes ON CONFLICT upsert and ~90-day TTL."""
    import adapters.pg_install_store as store

    executed: list[tuple] = []

    class _Conn:
        def execute(self, sql, params):
            executed.append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Pool:
        def connection(self):
            return _Conn()

    monkeypatch.setattr(store, "get_pool", lambda: _Pool())
    before = int(datetime.now(timezone.utc).timestamp())
    store.put_elder_last_reviewed(
        install_id=9, repo="o/r", pr_number=7, head_sha="deadbeef",
    )
    after = int(datetime.now(timezone.utc).timestamp())

    assert len(executed) == 1
    sql, params = executed[0]
    assert "ON CONFLICT (pk, sk) DO UPDATE" in sql
    assert params["sk"] == "ELDER#LAST#o/r#7"
    assert params["data"]  # encoded attrs present
    # TTL = now + 90 days (±2s clock slack for the test run).
    expected_lo = before + store._ELDER_LAST_TTL_DAYS * 86400 - 2
    expected_hi = after + store._ELDER_LAST_TTL_DAYS * 86400 + 2
    assert expected_lo <= params["ttl"] <= expected_hi
    assert store._ELDER_LAST_TTL_DAYS == 90


def test_living_hunt_delta_done_log_on_successful_persist(monkeypatch, caplog):
    """Successful delta review with stable head emits elder_living_hunt_delta_done."""
    import logging

    from llm_client import Backend, LlmReviewResponse

    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.CAVE, model_name="coder",
    )
    put_calls: list[dict] = []

    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **k: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **k: {"id": 1},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **k: {"id": 2},
    )
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_pr_diff_with_scope",
        lambda *a, **k: ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n", True),
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )

    def _fake_put(**kwargs):
        put_calls.append(kwargs)

    # Prior head makes living_prior_sha set and living_range non-empty.
    with patch(
        "adapters.install_store.get_elder_last_reviewed",
        return_value="priorhead0001",
    ), patch(
        "adapters.install_store.put_elder_last_reviewed",
        side_effect=_fake_put,
    ):
        with caplog.at_level(logging.INFO):
            out = cr_dispatch.dispatch_code_review(
                {
                    "action": "synchronize",
                    "installation": {"id": 11},
                    "repository": {
                        "id": 22,
                        "name": "myrepo",
                        "owner": {"login": "myorg"},
                    },
                    "pull_request": {
                        "number": 7,
                        "head": {"sha": "abcd1234efgh"},
                        "base": {"sha": "base5678ijkl"},
                        "title": "t",
                        "body": "b",
                        "user": {"login": "evan"},
                    },
                },
                blocking=False,
            )

    assert out["result"] in {"pass", "fail", "skipped"}
    assert put_calls, "expected put_elder_last_reviewed after stable head"
    assert any(
        getattr(r, "message", "") == "elder_living_hunt_delta_done"
        or "elder_living_hunt_delta_done" in str(r)
        for r in caplog.records
    ) or any(
        "elder_living_hunt_delta_done" in r.getMessage() for r in caplog.records
    )
