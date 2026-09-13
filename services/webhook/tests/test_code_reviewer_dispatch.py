"""Tests for personas/code_reviewer/dispatch.dispatch_code_review.

The dispatch function orchestrates: fetch PR diff via GH API → parse →
LLM → evaluate → publish check-run + inline review. Mocks every
downstream call; no real network or DDB."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_client import (
    Backend,
    Finding as LlmFinding,
    FindingJudgement,
    FindingOrigin,
    LlmReviewResponse,
)
from personas.code_reviewer import dispatch as cr_dispatch
from review_pipeline import ReviewCoverage, ReviewabilityConcern


_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def _payload(action: str = "opened") -> dict:
    return {
        "action": action,
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
            "title": "Preserve PR intent",
            "body": "Reviewer should understand the requested behavior.",
            "user": {"login": "evan"},
        },
    }


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    """Skip the real with_install_token_retry by stubbing it to call
    the wrapped function directly with a fake token."""
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )


def _diff_response(diff: str = _DIFF):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = diff
    return r


def test_dispatch_advisory_mode_posts_neutral_check_and_comment_review(monkeypatch):
    """Default mode (`code_reviewer_blocking=False`): check-run
    conclusion=neutral, review event=COMMENT. Both clients called with
    the right payload."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="catches Exception silently",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        model_name="laguna",
    )
    posted_check = []
    posted_review = []

    def _fake_review_diff(hunks, installation_id, pr_context=None, file_contents=None, cross_file_contents=None, runtime_context=None, voice="caveman", cancel_event=None):
        return llm

    def _fake_post_check_run(install_token, owner, repo, result, external_id=None):
        posted_check.append({"owner": owner, "repo": repo, "result": result})
        return {"id": 1}

    def _fake_post_review(install_token, owner, repo, *, pull_number, result):
        posted_review.append({"pull_number": pull_number, "result": result})
        return {"id": 2}

    monkeypatch.setattr(cr_dispatch, "review_diff", _fake_review_diff)
    monkeypatch.setattr(cr_dispatch, "post_check_run", _fake_post_check_run)
    monkeypatch.setattr(cr_dispatch, "post_review", _fake_post_review)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(
            _payload(), blocking=False,
        )

    assert out == {"persona": "code_reviewer", "result": "pass"}
    assert len(posted_check) == 1
    assert posted_check[0]["result"].conclusion == "neutral"  # advisory
    assert len(posted_review) == 1
    assert posted_review[0]["result"].event == "COMMENT"  # advisory
    # Inline comments include the finding.
    inline = posted_review[0]["result"].comments
    assert len(inline) == 1
    assert inline[0].path == "src/x.py"
    assert inline[0].line == 2
    # #189: inline body carries the hidden grug-rule marker so a later
    # synchronize can dedup it.
    assert "<!-- grug-rule:silent-failure -->" in inline[0].body


def test_dispatch_synchronize_dedups_already_commented_finding(monkeypatch):
    """On synchronize, a finding already commented on an unchanged line
    is NOT re-posted — prior Grug comments are fetched + matched (#189)."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    posted_review = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    # The diff GET and the prior-comments GET both go through httpx.get;
    # route by URL.
    prior_comment = {
        "path": "src/x.py", "line": 2,
        "body": "old\n\n<!-- grug-rule:silent-failure -->",
    }

    def staged_get(url, **kw):
        if "/comments" in url:
            r = MagicMock(spec=httpx.Response)
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value=[prior_comment])
            return r
        return _diff_response()

    with patch("httpx.get", side_effect=staged_get):
        out = cr_dispatch.dispatch_code_review(_payload(action="synchronize"), blocking=False)

    # The single finding was already commented → no inline review posted.
    assert posted_review == []
    # Check-run still ran (the bug is still there) — result reflects it.
    assert out["result"] == "pass"


def test_dispatch_synchronize_posts_new_finding_on_changed_line(monkeypatch):
    """A finding whose line differs from the prior comment IS posted
    (moved/changed line → new). The prior comment was on line 2; the new
    finding is on line 9."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=9, rule="silent-failure",
            severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    posted_review = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    def staged_get(url, **kw):
        if "/comments" in url:
            r = MagicMock(spec=httpx.Response)
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value=[
                {"path": "src/x.py", "line": 2,
                 "body": "<!-- grug-rule:silent-failure -->"},
            ])
            return r
        # The diff must contain line 9 so the hallucination filter keeps
        # the finding.
        return _diff_response(
            "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n"
            "@@ -1,2 +1,10 @@\n a\n+l2\n+l3\n+l4\n+l5\n+l6\n+l7\n+l8\n+new9\n b\n"
        )

    with patch("httpx.get", side_effect=staged_get):
        cr_dispatch.dispatch_code_review(_payload(action="synchronize"), blocking=False)

    assert len(posted_review) == 1
    assert posted_review[0].comments[0].line == 9


def test_dispatch_synchronize_dedup_fetch_failure_posts_all_and_flags(monkeypatch, caplog):
    """Prior-comments fetch failing on synchronize → dedup degrades to
    post-everything, and the dispatch log carries dedup_degraded=True so
    the duplicate comments are attributable to the fetch, not new
    findings."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    posted_review = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    def staged_get(url, **kw):
        if "/comments" in url:
            raise httpx.ReadTimeout("prior fetch down")
        return _diff_response()

    with caplog.at_level("INFO"):
        with patch("httpx.get", side_effect=staged_get):
            cr_dispatch.dispatch_code_review(_payload(action="synchronize"), blocking=False)

    # Fetch failed → posted anyway (post-everything fallback).
    assert len(posted_review) == 1
    rec = next(r for r in caplog.records if r.message == "code_reviewer_dispatched")
    assert rec.__dict__["dedup_degraded"] is True


def test_fetch_pr_review_comments_non_list_body_breaks(monkeypatch, caplog):
    """A non-list 200 (proxy/error envelope) is logged + treated as
    end-of-pages, not silently as empty without trace."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"message": "Moved"})
    with caplog.at_level("WARNING"):
        with patch("httpx.get", return_value=r):
            out = cr_dispatch._fetch_pr_review_comments("tok", "o", "r", 7)
    assert out == []
    assert any("comments_non_list_body" in rec.message for rec in caplog.records)


def test_fetch_pr_review_comments_uses_short_timeout(monkeypatch):
    """The dedup fetch is on the synchronous webhook path (60s budget, #252)
    and is best-effort — it must use a tight timeout (not the 10s diff
    timeout) so it can't exhaust the Lambda budget before degrading."""
    captured = {}

    def cap(url, *, params, headers, timeout):
        captured["timeout"] = timeout
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value=[])
        return r

    with patch("httpx.get", side_effect=cap):
        cr_dispatch._fetch_pr_review_comments("tok", "o", "r", 7)
    assert captured["timeout"] == cr_dispatch._COMMENT_FETCH_TIMEOUT
    assert cr_dispatch._COMMENT_FETCH_TIMEOUT < cr_dispatch._DIFF_FETCH_TIMEOUT


def test_fetch_pr_review_comments_accumulates_across_pages(monkeypatch):
    """A full page (100) followed by a short page must accumulate BOTH —
    guards against a per-page `out` reset or an off-by-one short-page
    break that would silently drop prior keys on a >100-comment PR
    (→ duplicate floods, the exact bug #189 fixes)."""
    pages = [
        [{"path": "x.py", "line": i, "body": "b"} for i in range(100)],  # full
        [{"path": "x.py", "line": 999, "body": "b"}],                    # short
    ]
    idx = {"n": 0}

    def staged(url, **kw):
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value=pages[idx["n"]])
        idx["n"] += 1
        return r

    with patch("httpx.get", side_effect=staged):
        out = cr_dispatch._fetch_pr_review_comments("tok", "o", "r", 7)
    assert len(out) == 101  # both pages accumulated, not reset
    assert idx["n"] == 2


def test_inline_comment_body_includes_suggestion_block(monkeypatch):
    """A finding WITH a suggestion renders the Suggested-fix block (the
    suggestion!=None arm) — all other tests use suggestion=None."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="m", suggestion="add a None guard",
    )
    body = cr_dispatch._inline_comment_body(f)
    assert "**Fix**" in body
    assert "add a None guard" in body
    assert "What Elder sees" in body
    assert "<!-- grug-rule:null-deref -->" in body  # marker still appended


def test_inline_comment_body_appends_precedent_note():
    """#555: a precedent note renders as a blockquote under the message,
    before the hidden dedup marker."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="sync-io-in-async",
        message="blocking call", suggestion=None,
    )
    body = cr_dispatch._inline_comment_body(
        f, precedent_note="Grug see this before -- 2 time(s) fixed here (#400, #366).",
    )
    assert "**Lore**" in body
    assert "> Grug see this before" in body
    assert "#400" in body
    assert body.index("Grug see this before") < body.index("grug-rule:sync-io-in-async")


def test_inline_comment_body_exposes_bounded_model_provenance():
    from personas.code_reviewer.persona import Finding

    f = Finding(
        file="src/x.py",
        line=2,
        severity="high",
        rule_name="null-deref",
        message="missing guard",
        suggestion=None,
        origins=(FindingOrigin(
            backend=Backend.CAVE,
            model="code-specialist",
            review_span_context=None,
            cohort_index=2,
            cohort_count=3,
            evidence_paths=("src/x.py", "src/types.py"),
            head_sha="abc123",
        ),),
    )

    body = cr_dispatch._inline_comment_body(f)

    assert "Evidence and provenance" in body
    assert "cohort 2/3" in body
    assert "src/types.py" in body
    assert "abc123" in body


def test_precedent_notes_for_is_best_effort_on_store_failure(monkeypatch):
    """A ledger fetch failure yields {} - a review is never blocked by
    missing precedent (the finding just posts without its citation)."""
    def _boom(repo, *a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr("adapters.install_store.list_ledger_rows", _boom, raising=False)
    from personas.code_reviewer.persona import Finding
    findings = (Finding(file="x.py", line=1, severity="high", rule_name="r", message="m", suggestion=None),)
    assert cr_dispatch._precedent_notes_for("o/r", findings) == {}


def test_precedent_notes_for_cites_matching_ledger_row(monkeypatch):
    """A finding whose class+file match a prior ACCEPTED ledger row gets a
    keyed precedent note."""
    rows = [
        {"repo": "o/r", "pr": 400, "reviewer": "claude/x", "severity": "HIGH",
         "class": "sync-io-in-async", "finding": "blocking call in async",
         "verdict": "fixed", "evidence": "re-targeted services/webhook/consumer.py",
         "ts": "2026-06-10T00:00:00Z"},
    ]
    monkeypatch.setattr("adapters.install_store.list_ledger_rows",
                        lambda repo, *a, **k: rows, raising=False)
    from personas.code_reviewer.dedup import finding_key
    from personas.code_reviewer.persona import Finding
    f = Finding(file="services/webhook/consumer.py", line=42, severity="high",
                rule_name="sync-io-in-async", message="m", suggestion=None)
    notes = cr_dispatch._precedent_notes_for("o/r", (f,))
    key = finding_key(f.file, f.line, f.rule_name)
    assert key in notes
    assert "#400" in notes[key]


def test_summary_markdown_renders_findings_table():
    """The findings table (severity icons + blocking count) is otherwise
    only reached on a real review; assert the row format + high/critical
    count directly."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    ev = CodeReviewEvaluation(
        findings=(
            Finding(file="x.py", line=1, severity="critical", rule_name="secret-in-log-or-trace", message="key", suggestion=None),
            Finding(file="y.py", line=2, severity="low", rule_name="dead-code", message="unused", suggestion=None),
        ),
        conclusion="failure",
    )
    title, summary = cr_dispatch._summary_markdown(ev)
    assert "1 blocking" in title  # one critical, one low
    assert "Markings Board" in summary
    assert "| Severity | Effort | File | Line | Rule | Marking |" in summary
    assert "secret-in-log-or-trace" in summary and "dead-code" in summary
    assert "`x.py`" in summary
    # Effort column present for every finding row (dash when unknown).
    assert summary.count("| - |") + summary.count("| quick win |") + summary.count(
        "| heavy lift |"
    ) >= 2


def test_summary_markdown_reports_exact_coverage_and_reviewability_debt():
    from personas.code_reviewer.persona import CodeReviewEvaluation

    coverage = ReviewCoverage(
        total_cohorts=3,
        completed_cohorts=2,
        failed_cohorts=(3,),
        cohort_labels=("schemas", "src", "tests"),
        concerns=(ReviewabilityConcern(
            kind="cross-cohort-module",
            message="Module spans independent proof units.",
            paths=("src/tangled.py",),
        ),),
    )
    ev = CodeReviewEvaluation(
        findings=(),
        conclusion="neutral",
        degraded_reason="partial_review",
        coverage=coverage,
    )

    _, summary = cr_dispatch._summary_markdown(ev)

    assert "Coverage: 2/3 cohorts" in summary
    assert "failed: 3" in summary
    assert "Reviewability" in summary
    assert "src/tangled.py" in summary


def test_summary_names_oversized_hunks_separately_from_data_files():
    """A hunk too big for one cohort is dropped BEFORE planning, and the
    summary must say so in its own words - calling a real code change
    "data/generated ... no meat for review there" would be a lie."""
    from personas.code_reviewer.persona import CodeReviewEvaluation

    ev = CodeReviewEvaluation(findings=(), conclusion="success")
    _, summary = cr_dispatch._summary_markdown(
        ev,
        excluded_paths=("services/api/uv.lock",),
        oversized_paths=("data/generated/lookup_table.json",),
    )

    assert "data/generated" in summary
    assert "services/api/uv.lock" in summary
    assert "one hunk too big" in summary
    assert "lookup_table.json" in summary
    # The two omissions are counted separately, not merged into one tally.
    assert "Grug not read 1 data/generated" in summary
    assert "Grug not read 1 file(s) whose change" in summary


def test_fetch_pr_review_comments_caps_pages(monkeypatch, caplog):
    """Pagination can't spin forever — a backend always returning a full
    page hits the cap + logs rather than looping inside the timeout."""
    def full_page(url, **kw):
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value=[{"path": "x", "line": 1, "body": "b"}] * 100)
        return r
    with caplog.at_level("WARNING"):
        with patch("httpx.get", side_effect=full_page):
            out = cr_dispatch._fetch_pr_review_comments("tok", "o", "r", 7)
    # Exactly _MAX_COMMENT_PAGES pages fetched, then cap.
    assert len(out) == cr_dispatch._MAX_COMMENT_PAGES * 100
    assert any("page_cap_hit" in rec.message for rec in caplog.records)


def test_dispatch_opened_skips_prior_comment_fetch(monkeypatch):
    """First pass (opened) — no prior Grug comments exist, so skip the
    comments fetch entirely (only the diff GET happens)."""
    llm = LlmReviewResponse(kind="no_diff")
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    urls = []

    def staged_get(url, **kw):
        urls.append(url)
        return _diff_response()

    with patch("httpx.get", side_effect=staged_get):
        cr_dispatch.dispatch_code_review(_payload(action="opened"), blocking=False)

    assert not any("/comments" in u for u in urls)


def test_dispatch_blocking_mode_uses_failure_conclusion_and_request_changes(monkeypatch):
    """`code_reviewer_blocking=True` + a high/critical finding flips
    check-run conclusion=failure and review event=REQUEST_CHANGES."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="secret-leak",
            severity="critical", message="API key in log",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    posted_check, posted_review = [], []

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    assert posted_check[0].conclusion == "failure"
    assert posted_review[0].event == "REQUEST_CHANGES"


def test_dispatch_no_findings_posts_clean_pass(monkeypatch):
    """LLM returned reviewed + empty findings → check conclusion=success
    (or neutral in advisory mode), review event=COMMENT, zero inline
    comments. Don't post a noisy "looks good!" inline comment when
    there's nothing to flag."""
    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
    )
    posted_check, posted_review = [], []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # Clean pass — check posts but with no findings table.
    assert posted_check[0].conclusion == "neutral"  # advisory always neutral
    # No inline review when there's nothing to comment on.
    assert posted_review == []


def test_dispatch_llm_outage_does_not_block_pr(monkeypatch):
    """LLM all_failed → advisory check-run posted with conclusion=neutral
    and degraded_reason message. No inline review (nothing to comment
    on). Elder must not 500 the dispatcher on LLM outages."""
    llm = LlmReviewResponse(kind="all_failed", error="poolside: timeout")
    posted_check, posted_review = [], []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    # Even in blocking mode, an LLM outage degrades to neutral —
    # advisory-first contract.
    assert posted_check[0].conclusion == "neutral"
    assert posted_review == []
    assert out == {
        "persona": "code_reviewer",
        "result": "skipped",
        "degraded_reason": "all_failed",
    }


def test_dispatch_single_backend_review_publishes_as_a_complete_review(monkeypatch):
    """Since #586 a single deep-backend reply is a complete `reviewed` result
    (not provisional): its findings publish as a normal review, not a neutral
    'incomplete' one."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py",
            line=2,
            rule="silent-failure",
            severity="high",
            message="error is discarded",
        ),),
        backend_used=Backend.POOLSIDE,
        backends_used=(Backend.POOLSIDE,),
        models_used=("poolside/laguna-m.1",),
    )
    posted_check, posted_review = [], []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch,
        "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch,
        "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    assert out == {"persona": "code_reviewer", "result": "fail"}  # blocking + high
    assert "incomplete" not in posted_check[0].title
    assert posted_review[0].event == "REQUEST_CHANGES"
    assert len(posted_review[0].comments) == 1


def test_dispatch_unparseable_diff_yields_neutral(monkeypatch):
    """parse_diff raises DiffParseError on a malformed @@ header. Caller
    must catch and degrade to advisory neutral — Elder cannot fail a
    PR on parser issues."""
    bad_diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ garbled @@\n+foo\n"
    posted_check = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )

    with patch("httpx.get", return_value=_diff_response(bad_diff)):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert posted_check[0].conclusion == "neutral"
    assert out["result"] == "skipped"


def test_dispatch_review_publish_failure_returns_publish_failed(monkeypatch):
    """Inline-review publish 5xx must surface as `publish_failed`, not
    silently `pass`. Without this, DD dashboards would overstate
    success — inline comments never reached GitHub yet the log fires
    with result=pass and findings_count=N."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="x", severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})

    def _raise(*a, **kw):
        raise httpx.ConnectError("reviews API down")

    monkeypatch.setattr(cr_dispatch, "post_review", _raise)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "publish_failed"


def test_dispatch_check_run_publish_failure_returns_publish_failed(monkeypatch):
    """Check-run is the load-bearing GH surface (flips mergeability in
    blocking mode). If it fails to publish, the persona result must be
    `publish_failed` not `pass`/`fail` — operator needs to see the
    distinct state to triage."""
    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    def _raise(*a, **kw):
        raise httpx.ConnectError("checks API down")

    monkeypatch.setattr(cr_dispatch, "post_check_run", _raise)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "publish_failed"


def test_dispatch_llm_degraded_logs_warning(monkeypatch, caplog):
    """A 100% LLM-outage rate must produce a distinct log signal so
    DD/dashboards can monitor backend health per-install. Without
    this, all_failed looks identical to "no findings" in logs."""
    llm = LlmReviewResponse(
        kind="all_failed", error="poolside: timeout",
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("WARNING"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert any(
        "code_review_llm_degraded" in r.message for r in caplog.records
    )


def test_dispatch_degraded_publish_failure_returns_publish_failed(monkeypatch):
    """Fetch fails AND the degraded check-run publish also fails →
    result must be `publish_failed`, not `skipped`. Without this, a
    regression that silently swallows the degraded publish would mask
    a "no check-run at all" production state as a benign "skipped"."""
    # Make the diff fetch raise so we enter the _publish_degraded path.
    def _fetch_raises(*a, **kw):
        raise httpx.ConnectError("github down")

    def _post_raises(*a, **kw):
        raise httpx.ConnectError("checks API also down")

    monkeypatch.setattr(cr_dispatch, "post_check_run", _post_raises)

    with patch("httpx.get", side_effect=_fetch_raises):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "publish_failed"


def test_blocking_mode_failure_produces_request_changes(monkeypatch):
    """The ONLY path that actually blocks a merge:
    mode=blocking + evaluation.conclusion=failure →
    (check.conclusion=failure, review.event=REQUEST_CHANGES).
    Advisory + degraded tests cover the other branches; this is the
    one that makes blocking-mode mean something."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="critical-rule",
            severity="critical", message="secret leak",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )
    posted_check, posted_review = [], []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: posted_check.append(kw.get("result") or a[3]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw["result"]) or {},
    )

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    assert posted_check[0].conclusion == "failure"
    assert posted_review[0].event == "REQUEST_CHANGES"
    assert out["result"] == "fail"


def test_resolve_result_publish_failed_wins_over_skipped():
    """When check-run publish fails AND the evaluation is degraded
    (e.g. all_failed LLM), publish_failed must win. Operator needs to
    see the publish failure (production-visible) over the skip reason
    (already implied by neutral conclusion)."""
    from personas.code_reviewer.persona import CodeReviewEvaluation
    degraded = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="all_failed",
    )
    assert cr_dispatch._resolve_result(
        degraded, check_publish_failed=True,
    ) == "publish_failed"


def test_dispatch_emits_structured_log_on_success(monkeypatch, caplog):
    """Acceptance criterion (#186): "Grug webhook logs show
    `code_reviewer_dispatched` structured log entry." Operator uses
    this to confirm Elder ran on a real PR end-to-end. Must include
    pr, installation_id, backend, model, finding count, and result."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="catches Exception silently",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        model_name="poolside/laguna-m.1",
        backends_used=(Backend.POOLSIDE, Backend.OPENROUTER),
        models_used=("poolside/laguna-m.1", "anthropic/claude-opus-4.7"),
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("INFO"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    dispatched_records = [
        r for r in caplog.records if r.message == "code_reviewer_dispatched"
    ]
    assert len(dispatched_records) == 1
    extra = dispatched_records[0].__dict__
    # Operator must be able to filter by install + PR coords.
    assert extra.get("installation_id") == 11
    assert extra.get("pr") == "myorg/myrepo#7"
    # Backend + model attribution for DD LLM Obs / per-backend dashboards.
    assert extra.get("backend") == "poolside"
    assert extra.get("model") == "poolside/laguna-m.1"
    assert extra.get("backends") == ["poolside", "openrouter"]
    assert extra.get("models") == [
        "poolside/laguna-m.1", "anthropic/claude-opus-4.7",
    ]
    # Finding count + result for at-a-glance triage.
    assert extra.get("findings_count") == 1
    assert extra.get("result") == "pass"


def test_structured_log_handles_none_backend_without_attributeerror(monkeypatch, caplog):
    """The conditional `backend_used.value if not None else None`
    guards against an AttributeError on degraded responses where
    `backend_used is None` (e.g. no_diff). This is the exact path
    operators care about monitoring — a NoneType crash here would
    silently break the degraded-backend log."""
    llm = LlmReviewResponse(kind="no_diff")  # backend_used defaults None
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("INFO"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    rec = next(
        r for r in caplog.records if r.message == "code_reviewer_dispatched"
    )
    # backend is None when no LLM call ran.
    assert rec.__dict__.get("backend") is None


def test_structured_log_carries_dropped_hallucinations_count(monkeypatch, caplog):
    """The `dropped_hallucinations` field on the log lets DD slice the
    LLM hallucination rate per backend. A regression renaming the
    attribute or substituting `len(llm_response.findings)` would
    silently break that observability slice."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            LlmFinding(path="src/x.py", line=2, rule="real", severity="medium", message="m"),  # type: ignore[arg-type]
            # Two hallucinations the filter will drop:
            LlmFinding(path="src/x.py", line=9999, rule="ghost1", severity="low", message="m"),  # type: ignore[arg-type]
            LlmFinding(path="absent.py", line=2, rule="ghost2", severity="low", message="m"),  # type: ignore[arg-type]
        ),
        backend_used=Backend.POOLSIDE,
        model_name="laguna",
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("INFO"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    rec = next(
        r for r in caplog.records if r.message == "code_reviewer_dispatched"
    )
    assert rec.__dict__.get("dropped_hallucinations") == 2
    # findings_count is the POST-drop kept count, not the raw LLM count.
    assert rec.__dict__.get("findings_count") == 1


def test_resolve_result_both_publishes_failed_returns_publish_failed():
    """Both publish failures coalesce to a single `publish_failed` (not
    double-counted, no separate `both_failed` state). Also covers the
    `review_publish_failed=True` + `evaluation.passed=False` path —
    `fail` must NOT mask the publish-failure signal."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    # Build an evaluation where evaluation.passed=False (a critical).
    failing_eval = CodeReviewEvaluation(
        findings=(Finding(
            file="x.py", line=1, severity="critical", rule_name="c",
            message="m", suggestion=None,
        ),),
        conclusion="failure",
    )
    out = cr_dispatch._resolve_result(
        failing_eval,
        check_publish_failed=True,
        review_publish_failed=True,
    )
    assert out == "publish_failed"
    # Single-publish-failure ALSO returns publish_failed (not "fail")
    # even when verdict is failure.
    out_review_only = cr_dispatch._resolve_result(
        failing_eval,
        check_publish_failed=False,
        review_publish_failed=True,
    )
    assert out_review_only == "publish_failed"


def test_dispatch_structured_log_carries_degraded_reason(monkeypatch, caplog):
    """When LLM all_failed → degraded_reason on the log so DD can
    correlate dispatch volume with backend health."""
    llm = LlmReviewResponse(kind="all_failed", error="poolside: timeout")
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with caplog.at_level("INFO"):
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    rec = next(
        r for r in caplog.records if r.message == "code_reviewer_dispatched"
    )
    assert rec.__dict__.get("degraded_reason") == "all_failed"
    assert rec.__dict__.get("result") == "skipped"


def test_dispatch_submits_judge_evals_after_publish_with_review_span(monkeypatch):
    """The review's findings are graded pre-publish (grade_findings) and
    the verdicts submitted to DD AFTER publishing (submit_evals) — with the
    full graded set + the review's span context for eval attribution (#467)."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="r", severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "rs1"},
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})
    # A "real" verdict so nothing is suppressed - the finding still publishes.
    monkeypatch.setattr(
        cr_dispatch, "grade_findings",
        lambda *a, **kw: (FindingJudgement(0, True, "real", 0.9),),
    )

    submit_calls: list[dict] = []
    monkeypatch.setattr(
        cr_dispatch, "submit_evals",
        lambda findings, verdicts, **kw: submit_calls.append(
            {"findings": findings, "verdicts": verdicts, **kw}
        ),
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert len(submit_calls) == 1
    assert submit_calls[0]["review_span_context"] == {"span_id": "rs1"}
    assert len(submit_calls[0]["findings"]) == 1  # the full graded set


def test_dispatch_suppresses_confident_medium_false_positive(monkeypatch):
    """A medium finding the judge confidently calls not-real is NOT
    published as an inline comment, but IS still recorded to DD evals
    (#467)."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="nit", severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "rs1"},
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(
        cr_dispatch, "grade_findings",
        lambda *a, **kw: (FindingJudgement(0, False, "false positive", 0.95),),
    )
    posted_reviews: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_reviews.append(kw) or {},
    )
    submitted: list = []
    monkeypatch.setattr(
        cr_dispatch, "submit_evals",
        lambda findings, verdicts, **kw: submitted.append(findings),
    )

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # The suppressed finding produced no inline review (nothing to post).
    assert posted_reviews == []
    # But it was still submitted to DD evals (denominator preserved).
    assert len(submitted[0]) == 1
    # Suppressed-all -> clean pass verdict.
    assert out["result"] == "pass"


def test_dispatch_judge_failure_does_not_change_result(monkeypatch):
    """The judge is pure observability past the gate — even if submit_evals
    raises (past its own internal guard), the dispatch result must stand."""
    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "rs1"},
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    def _boom(*a, **kw):
        raise RuntimeError("judge exploded past its guard")

    monkeypatch.setattr(cr_dispatch, "submit_evals", _boom)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # No findings → clean pass; judge explosion must not change it.
    assert out["result"] == "pass"


def test_dispatch_passes_identity_and_intent_to_review_diff(monkeypatch):
    """The review receives both trace coordinates and author intent."""
    captured = []

    def _fake_review_diff(hunks, installation_id, pr_context=None, file_contents=None, cross_file_contents=None, runtime_context=None, voice="caveman", cancel_event=None):
        captured.append(pr_context)
        return LlmReviewResponse(kind="no_diff")

    monkeypatch.setattr(cr_dispatch, "review_diff", _fake_review_diff)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx == {
        "installation_id": 11,
        "repo": "myorg/myrepo",
        "pr_number": 7,
        "head_sha": "abcd1234efgh",
        "base_sha": "base5678ijkl",
        "title": "Preserve PR intent",
        "body": "Reviewer should understand the requested behavior.",
    }


def test_dispatch_fetches_immutable_base_head_diff(monkeypatch):
    """Diff, full files, and publication all target the event snapshot."""
    captured = []
    monkeypatch.setattr(
        cr_dispatch, "review_diff",
        lambda *a, **kw: LlmReviewResponse(kind="no_diff"),
    )
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    def capture_get(url, *, headers, timeout, params=None):
        captured.append({"url": url, "headers": headers, "timeout": timeout})
        return _diff_response()

    with patch("httpx.get", side_effect=capture_get):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # First GET is the unified diff (diff Accept header).
    assert captured[0]["headers"]["Accept"] == "application/vnd.github.diff"
    assert "myorg/myrepo" in captured[0]["url"]
    assert "/compare/base5678ijkl...abcd1234efgh" in captured[0]["url"]
    assert captured[0]["timeout"] == cr_dispatch._DIFF_FETCH_TIMEOUT
    # #336: subsequent GET(s) fetch full file content with the `.raw` Accept
    # header so the Elder sees mitigations outside the diff hunk.
    raw_gets = [c for c in captured if c["headers"].get("Accept") == "application/vnd.github.raw"]
    assert raw_gets, "expected a full-file-content fetch with the raw Accept header"
    assert "/contents/" in raw_gets[0]["url"]


@pytest.mark.parametrize("compare_status", [404, 422])
def test_fetch_pr_diff_falls_back_when_immutable_compare_is_unavailable(
    compare_status,
):
    unavailable = _diff_response()
    unavailable.status_code = compare_status
    fallback = _diff_response("fallback diff")

    with patch("httpx.get", side_effect=[unavailable, fallback]) as get:
        diff = cr_dispatch._fetch_pr_diff(
            "token", "myorg", "myrepo", 7,
            base_sha="base", head_sha="head",
        )

    assert diff == "fallback diff"
    assert "/compare/base...head" in get.call_args_list[0].args[0]
    assert get.call_args_list[1].args[0].endswith("/pulls/7")
    assert get.call_args_list[0].kwargs["headers"] == (
        get.call_args_list[1].kwargs["headers"]
    )


def test_durable_dispatch_publishes_when_only_intent_moved_during_inference(monkeypatch):
    """#773: an intent edit mid-flight must not throw the review away.

    This used to assert the opposite - same `head_sha`, body changed, verdict
    discarded as `stale_snapshot`. That IS the bug: the discard re-enqueues
    against the same sha, `already_in_progress` then declines to post a
    check-run, and the check sits `pending` forever.

    The findings were computed against a byte-identical diff, so they still
    anchor to real lines. Publishing a review whose intent blurb is one edit
    behind is bounded staleness; never publishing is unbounded - the same
    trade-off `review_freshness_id` already makes explicitly for `base_sha`.
    """
    payload = _payload(action="review")
    initial_id = cr_dispatch.review_freshness_id_from_pr(payload["pull_request"])
    changed_pr = {**payload["pull_request"], "body": "Changed intent"}
    changed_id = cr_dispatch.review_freshness_id_from_pr(changed_pr)
    assert changed_id != initial_id, "fixture must actually move the freshness id"
    monkeypatch.setattr(
        cr_dispatch,
        "review_diff",
        lambda *a, **kw: LlmReviewResponse(kind="reviewed"),
    )
    monkeypatch.setattr(
        cr_dispatch,
        "_fetch_current_review_snapshot",
        MagicMock(side_effect=[
            # SAME head sha both times - only the body moved.
            (initial_id, "abcd1234efgh", "open", False),
            (changed_id, "abcd1234efgh", "open", False),
        ]),
    )
    post_check = MagicMock()
    post_review = MagicMock()
    monkeypatch.setattr(cr_dispatch, "post_check_run", post_check)
    monkeypatch.setattr(cr_dispatch, "post_review", post_review)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(payload, blocking=False)

    assert out["result"] != "skipped"
    assert out.get("degraded_reason") != "stale_snapshot"
    post_check.assert_called()


def test_durable_dispatch_rejects_stale_input_before_inference(monkeypatch):
    # Was asserted against a base-sha-only change, which ENCODED the bug fixed
    # in the base-churn PR: an unrelated merge to the base branch cancelled a
    # review of unchanged code. Staleness now means the author pushed new code,
    # so this drives it with a real head change.
    #
    # The fixture used to compute `current_id` from head `newhead0000` while
    # returning `abcd1234efgh` as the CURRENT head - i.e. it asserted the #773
    # bug (same sha, different id => discarded). Now internally consistent:
    # a genuine push moves both.
    payload = _payload(action="review")
    current_pr = {
        **payload["pull_request"],
        "head": {**payload["pull_request"]["head"], "sha": "newhead0000"},
    }
    current_id = cr_dispatch.review_freshness_id_from_pr(current_pr)
    monkeypatch.setattr(
        cr_dispatch,
        "_fetch_current_review_snapshot",
        lambda *a: (current_id, "newhead0000", "open", False),
    )
    review = MagicMock()
    monkeypatch.setattr(cr_dispatch, "review_diff", review)
    post_check = MagicMock()
    post_review = MagicMock()
    monkeypatch.setattr(cr_dispatch, "post_check_run", post_check)
    monkeypatch.setattr(cr_dispatch, "post_review", post_review)

    out = cr_dispatch.dispatch_code_review(payload, blocking=False)

    assert out["degraded_reason"] == "stale_snapshot"
    review.assert_not_called()
    post_check.assert_not_called()
    post_review.assert_not_called()


def test_intent_only_edit_does_not_discard_a_review_of_the_same_commit(monkeypatch):
    """#773: editing the PR body must not throw away a finished review.

    The diff is what Elder reviews. When `head_sha` is unchanged the findings
    were computed against byte-identical code, so discarding them is pure loss
    - and the discard re-enqueues against the same sha, where
    `already_in_progress` then declines to post a check-run, leaving the check
    `pending` forever.

    Live on quadseven/infra#2133 (2026-08-01): Elder passed at 03:00:27, the
    author edited the body to satisfy Chief's DoR gate at 03:08, and the check
    went back to pending for 25 minutes on a docs-only diff.
    """
    payload = _payload(action="review")
    edited_pr = {**payload["pull_request"], "body": "Rewritten intent, same code."}
    edited_id = cr_dispatch.review_freshness_id_from_pr(edited_pr)
    assert edited_id != cr_dispatch.review_freshness_id_from_pr(
        payload["pull_request"]
    ), "fixture must actually change the freshness id"

    monkeypatch.setattr(
        cr_dispatch,
        "_fetch_current_review_snapshot",
        # SAME head sha as the payload - only the intent moved.
        lambda *a: (edited_id, "abcd1234efgh", "open", False),
    )

    out = cr_dispatch._review_snapshot_freshness_failure(
        installation_id=11,
        owner="myorg",
        repo_name="myrepo",
        pull_number=7,
        expected_snapshot_id=cr_dispatch.review_freshness_id_from_pr(
            payload["pull_request"]
        ),
        expected_head_sha="abcd1234efgh",
    )

    assert out is None, "a review of the current commit must still publish"


@pytest.mark.parametrize(
    ("state", "draft"),
    (("closed", False), ("open", True)),
)
def test_durable_dispatch_rejects_ineligible_pr_before_inference(
    monkeypatch, state, draft,
):
    payload = _payload(action="review")
    snapshot_id = cr_dispatch.review_freshness_id_from_pr(payload["pull_request"])
    monkeypatch.setattr(
        cr_dispatch,
        "_fetch_current_review_snapshot",
        lambda *a: (snapshot_id, "abcd1234efgh", state, draft),
    )
    review = MagicMock()
    monkeypatch.setattr(cr_dispatch, "review_diff", review)
    post_check = MagicMock()
    post_review = MagicMock()
    monkeypatch.setattr(cr_dispatch, "post_check_run", post_check)
    monkeypatch.setattr(cr_dispatch, "post_review", post_review)

    out = cr_dispatch.dispatch_code_review(payload, blocking=False)

    assert out["degraded_reason"] == "pr_ineligible"
    review.assert_not_called()
    post_check.assert_not_called()
    post_review.assert_not_called()


def test_no_single_webhook_timeout_reaches_lambda_budget():
    """#252 AC: the code-review per-request httpx timeout CONSTANTS are
    reconciled with the webhook Lambda budget — none reaches it, so one hung
    upstream on these paths can't, by itself, consume the whole budget and
    kill the handler mid-flight (a 16s review was killed by the old 15s
    budget). This is the bound this PR makes. (The post_check_run/post_review
    clients also carry 10s literals — well under 60s — not tracked as named
    constants here.)

    It deliberately does NOT assert the PATH SUM (diff + review + publish +
    dedup + capture + judge, ×`_RETRY_ATTEMPTS` ×2 backends) fits 60s — it
    does NOT under a slow/hung backend (~180s), and no sane synchronous budget
    bounds that. The only real fix is moving the LLM call off the ACK path
    (async offload, #272). Asserting a path-sum bound here would be false."""
    import llm_client
    # 420s: the post-#272 async-offload webhook Lambda timeout (the function
    # that runs the Elder dispatch). The old 60 here predated that bump.
    _WEBHOOK_LAMBDA_BUDGET = 420  # keep in sync w/ infra/pulumi/__main__ webhook
    assert cr_dispatch._DIFF_FETCH_TIMEOUT < _WEBHOOK_LAMBDA_BUDGET
    assert cr_dispatch._COMMENT_FETCH_TIMEOUT < _WEBHOOK_LAMBDA_BUDGET
    assert llm_client._TIMEOUT_SECONDS < _WEBHOOK_LAMBDA_BUDGET


# --- #247a: capture inline-comment IDs on publish (best-effort) ---

def _llm_with_span():
    return LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "s1", "trace_id": "t1"},
    )


def test_dispatch_captures_comment_records_on_publish(monkeypatch):
    """After the review posts, fetch its inline-comment IDs and persist a
    CommentRecord per comment — keyed to the review span + the finding's
    eval_tags — so the reaction poller (#247) can later attach human_verdict."""
    captured = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [{
            "id": 555, "path": "src/x.py", "line": 2,
            "body": "m\n<!-- grug-rule:silent-failure -->",
        }],
    )
    monkeypatch.setattr(
        cr_dispatch, "put_comment_record",
        lambda **kw: captured.append(kw),
    )
    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "pass"
    assert len(captured) == 1
    rec = captured[0]
    assert rec["comment_id"] == 555
    assert rec["repo"] == "myorg/myrepo"
    assert rec["pr_number"] == 7
    assert rec["review_span_context"] == {"span_id": "s1", "trace_id": "t1"}
    # finding_tags are the judge's eval_tags shape (shared finding identity).
    assert rec["finding_tags"]["rule_name"] == "silent-failure"
    assert rec["finding_tags"]["file"] == "src/x.py"
    assert rec["finding_tags"]["line"] == "2"
    assert rec["finding_text"] == "m"
    assert rec["head_sha"] == "abcd1234efgh"
    assert rec["author_login"] == "evan"
    assert rec["trust_reactors"] is True


def test_dispatch_captures_all_origin_spans_without_global_span(monkeypatch):
    """Ensemble findings remain reaction-trainable even when the legacy
    response-level span is absent: persist every finding origin on the
    posted comment record."""
    origins = (
        FindingOrigin(
            backend=Backend.POOLSIDE,
            model="poolside/laguna-m.1",
            review_span_context={"span_id": "poolside-span"},
        ),
        FindingOrigin(
            backend=Backend.OPENROUTER,
            model="anthropic/claude-opus-4.7",
            review_span_context={"span_id": "openrouter-span"},
        ),
    )
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py",
            line=2,
            rule="silent-failure",
            severity="medium",
            message="m",
            origins=origins,
        ),),
        review_span_context=None,
    )
    captured: list[dict] = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch,
        "get_review_comments",
        lambda *a, **kw: [{
            "id": 555,
            "path": "src/x.py",
            "line": 2,
            "body": "m\n<!-- grug-rule:silent-failure -->",
        }],
    )
    monkeypatch.setattr(
        cr_dispatch,
        "put_comment_record",
        lambda **kw: captured.append(kw),
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert captured[0]["review_span_context"] == {"span_id": "poolside-span"}
    assert captured[0]["finding_origins"] == [
        {
            "backend": "poolside",
            "model": "poolside/laguna-m.1",
            "review_span_context": {"span_id": "poolside-span"},
        },
        {
            "backend": "openrouter",
            "model": "anthropic/claude-opus-4.7",
            "review_span_context": {"span_id": "openrouter-span"},
        },
    ]


def test_dispatch_capture_failure_does_not_affect_review(monkeypatch):
    """A capture failure (GH 5xx fetching review comments) is swallowed —
    the review already posted, so dispatch still returns its real result and
    never raises (the best-effort, post-publish contract)."""
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})

    def _boom(*a, **kw):
        raise httpx.RequestError("comment fetch down")
    monkeypatch.setattr(cr_dispatch, "get_review_comments", _boom)
    persisted = []
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: persisted.append(kw))

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "pass"   # review outcome unaffected
    assert persisted == []            # nothing captured, no crash


def test_dispatch_capture_ddb_error_does_not_500_dispatch(monkeypatch):
    """A NON-httpx error in capture (e.g. a DDB put_comment_record failure)
    must also be swallowed — capture touches both GH and DDB, so the guard is
    broad. Otherwise a DDB blip would 500 the webhook (spec 0017 never-raise)."""
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [{
            "id": 9, "path": "src/x.py", "line": 2,
            "body": "m\n<!-- grug-rule:silent-failure -->",
        }],
    )

    def _ddb_boom(**kw):
        raise RuntimeError("DDB ProvisionedThroughputExceeded")
    monkeypatch.setattr(cr_dispatch, "put_comment_record", _ddb_boom)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "pass"   # dispatch did not raise


def test_dispatch_capture_partial_batch_persists_the_rest(monkeypatch):
    """A single comment's DDB put failing must NOT drop the rest of the batch
    — capture is per-comment best-effort (one throttle ≠ lose every record)."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            LlmFinding(path="src/x.py", line=2, rule="r1", severity="medium", message="m"),  # type: ignore[arg-type]
            LlmFinding(path="src/x.py", line=3, rule="r2", severity="low", message="m"),  # type: ignore[arg-type]
        ),
        backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "s", "trace_id": "t"},
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [
            {"id": 1, "path": "src/x.py", "line": 2,
             "body": "m\n<!-- grug-rule:r1 -->"},
            {"id": 2, "path": "src/x.py", "line": 3,
             "body": "m\n<!-- grug-rule:r2 -->"},
        ],
    )
    persisted = []

    def _put(**kw):
        if kw["comment_id"] == 1:
            raise RuntimeError("DDB throttle on first")
        persisted.append(kw["comment_id"])
    monkeypatch.setattr(cr_dispatch, "put_comment_record", _put)

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "pass"
    assert persisted == [2]   # comment 1 threw, comment 2 still persisted


def test_dispatch_capture_two_rules_same_line_keep_distinct_tags(monkeypatch):
    """Two distinct rules can comment the SAME (file, line) — capture must
    key by rule (via the comment's grug-rule marker), NOT collapse to one
    finding, so each record carries its own rule's tags (matches dedup)."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            LlmFinding(path="src/x.py", line=2, rule="null-deref", severity="high", message="m"),  # type: ignore[arg-type]
            LlmFinding(path="src/x.py", line=2, rule="race-condition", severity="high", message="m"),  # type: ignore[arg-type]
        ),
        backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "s", "trace_id": "t"},
    )
    captured = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [
            {"id": 1, "path": "src/x.py", "line": 2, "body": "m\n<!-- grug-rule:null-deref -->"},
            {"id": 2, "path": "src/x.py", "line": 2, "body": "m\n<!-- grug-rule:race-condition -->"},
        ],
    )
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: captured.append(kw))
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    by_id = {c["comment_id"]: c["finding_tags"]["rule_name"] for c in captured}
    assert by_id == {1: "null-deref", 2: "race-condition"}  # NOT collapsed


def test_dispatch_captures_learning_record_when_review_span_absent(monkeypatch):
    """A tracing outage must not discard later trusted human feedback."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        review_span_context=None,  # span never exported
    )
    captured = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [{
            "id": 9,
            "path": "src/x.py",
            "line": 2,
            "body": "m\n<!-- grug-rule:silent-failure -->",
        }],
    )
    monkeypatch.setattr(
        cr_dispatch, "put_comment_record", lambda **kw: captured.append(kw),
    )
    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert out["result"] == "pass"
    assert captured[0]["review_span_context"] is None
    assert captured[0]["finding_text"] == "m"
    assert captured[0]["author_login"] == "evan"


def test_dispatch_capture_skips_unmarked_human_comment(monkeypatch):
    """LOAD-BEARING: a human reply with no grug-rule marker that happens to
    land on a finding's (file,line) must NOT be captured — only OUR marked
    comments become CommentRecords. A marked sibling in the same batch IS."""
    captured = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [
            {"id": 100, "path": "src/x.py", "line": 2, "body": "looks fine to me"},  # human, no marker
            {"id": 200, "path": "src/x.py", "line": 2,
             "body": "m\n<!-- grug-rule:silent-failure -->"},                          # ours
        ],
    )
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: captured.append(kw["comment_id"]))
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert captured == [200]  # human comment 100 NOT captured


def test_dispatch_capture_skips_marked_comment_with_no_matching_finding(monkeypatch):
    """A marked comment whose (file,line,rule) matches no current finding
    (stale prior-review comment, renamed rule) is skipped — not persisted."""
    captured = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())  # finding: silent-failure@2
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [{"id": 9, "path": "src/x.py", "line": 2,
                           "body": "m\n<!-- grug-rule:some-other-rule -->"}],
    )
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: captured.append(kw))
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert captured == []


def test_dispatch_capture_ignores_null_line_and_original_line(monkeypatch):
    """grug#967: GitHub's REST API is eventually consistent for a JUST-
    created review comment's `line`/`original_line` fields - fetching them
    in the same synchronous round-trip this capture step runs in (moments
    after `post_review` returns) reliably returns both null, confirmed
    live, even though the identical comment queried again seconds later has
    the correct values. This was a 100%-reproducing capture failure: every
    real comment this function ever saw was silently dropped. The fix
    (grug#967) stopped reading `line`/`original_line` from the fetched
    comment at all - `path` and the rule marker are matched against the
    Finding directly, so a comment lacking BOTH fields still captures on
    the very first fetch, no retry needed."""
    captured = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [{
            "id": 555, "path": "src/x.py", "line": None, "original_line": None,
            "body": "m\n<!-- grug-rule:silent-failure -->",
        }],
    )
    monkeypatch.setattr(
        cr_dispatch, "put_comment_record",
        lambda **kw: captured.append(kw),
    )
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert len(captured) == 1
    assert captured[0]["comment_id"] == 555


def test_dispatch_capture_succeeds_on_position_only_comment_no_retry(monkeypatch):
    """grug#967's complete shape, confirmed live via a throwaway PR: the
    FIRST (and, post-fix, ONLY) fetch, run in the same synchronous
    round-trip as `post_review`, can return a comment carrying only
    `position`/`original_position` (a diff-patch offset, not a file line
    number) - GitHub had not yet computed ANY human-readable field, and a
    live measurement showed that gap can outlast even a 30-second bounded
    retry. The fix never needs those fields: exactly one fetch happens, no
    sleep, and the comment still captures via its path + rule marker."""
    calls = {"n": 0}

    def _fetch(*a, **kw):
        calls["n"] += 1
        return [{
            "id": 556, "path": "src/x.py",
            "position": 2, "original_position": 2,
            "body": "m\n<!-- grug-rule:silent-failure -->",
        }]

    captured = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(cr_dispatch, "get_review_comments", _fetch)
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: captured.append(kw))
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert calls["n"] == 1
    assert len(captured) == 1
    assert captured[0]["comment_id"] == 556


def test_dispatch_capture_same_rule_two_lines_matches_in_order(monkeypatch):
    """(file, rule) alone is ambiguous when the SAME rule fires twice in one
    file at different lines - capture must not cross-wire them. Each
    matching comment claims the next unclaimed finding for that (file,
    rule) in the order `_build_review_result` posted them."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            LlmFinding(path="src/x.py", line=2, rule="null-deref", severity="medium", message="first"),  # type: ignore[arg-type]
            LlmFinding(path="src/x.py", line=3, rule="null-deref", severity="medium", message="second"),  # type: ignore[arg-type]
        ),
        backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "s", "trace_id": "t"},
    )
    captured = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [
            {"id": 1, "path": "src/x.py", "body": "first\n<!-- grug-rule:null-deref -->"},
            {"id": 2, "path": "src/x.py", "body": "second\n<!-- grug-rule:null-deref -->"},
        ],
    )
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: captured.append(kw))
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    by_id = {c["comment_id"]: c["finding_text"] for c in captured}
    assert by_id == {1: "first", 2: "second"}


def test_dispatch_capture_zero_alarm_logged(monkeypatch, caplog):
    """0-of-N capture (non-empty fetch, nothing matched) fires the
    code_review_comment_capture_zero alarm — the only signal that a
    comment↔finding shape regression silently emptied the poller batch."""
    import logging as _logging
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [{"id": 1, "path": "src/x.py", "line": 2, "body": "no marker"}],
    )
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: None)
    with patch("httpx.get", return_value=_diff_response()), caplog.at_level(_logging.WARNING):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    zero = [r for r in caplog.records if r.msg == "code_review_comment_capture_zero"]
    assert zero and getattr(zero[0], "fetched", None) == 1


def test_dispatch_no_capture_when_review_publish_failed(monkeypatch):
    """If the review post FAILED, capture must not run (nothing to attach to,
    and get_review_comments on a non-existent review_id would 404)."""
    fetched = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})

    def _raise(*a, **kw):
        raise httpx.RequestError("review post down")
    monkeypatch.setattr(cr_dispatch, "post_review", _raise)
    monkeypatch.setattr(cr_dispatch, "get_review_comments", lambda *a, **kw: fetched.append(1) or [])
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: None)
    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert out["result"] == "publish_failed"
    assert fetched == []  # capture skipped — review never posted


def test_dispatch_no_capture_when_review_resp_has_no_id(monkeypatch):
    """A post_review response shape without `id` short-circuits capture
    (can't build the reviews/{id}/comments URL)."""
    fetched = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _llm_with_span())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})  # no id
    monkeypatch.setattr(cr_dispatch, "get_review_comments", lambda *a, **kw: fetched.append(1) or [])
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: None)
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert fetched == []


def test_activity_verdict_is_errored_when_check_run_publish_fails(monkeypatch):
    """No-lies: if the check-run never reaches GitHub, the Activity row must NOT
    claim a pass/block — it records a `check_publish_failed` degraded_reason so
    the verdict resolves to `errored` (re-runnable, honest)."""
    llm = LlmReviewResponse(kind="reviewed", findings=(), backend_used=Backend.POOLSIDE)
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    # check-run POST fails; dispatch logs publish_failed + continues.
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.RequestError("publish boom")),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})
    recorded: dict = {}
    monkeypatch.setattr(
        cr_dispatch, "record_check_verdict",
        lambda **kw: recorded.update(kw),
    )
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert recorded["degraded_reason"] == "check_publish_failed"


def test_activity_verdict_errored_when_diff_fetch_fails(monkeypatch):
    """No-lies: a diff fetch/parse failure ("Grug couldn't even look") records
    an `errored` Activity row (degraded_reason set), never a fabricated pass."""
    recorded: dict = {}
    monkeypatch.setattr(cr_dispatch, "record_check_verdict", lambda **kw: recorded.update(kw))
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    with patch("httpx.get", side_effect=httpx.ConnectError("gh down")):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert recorded["degraded_reason"] == "fetch_or_parse_failed"
    assert recorded["conclusion"] == "neutral"
    assert recorded["persona_key"] == "code_reviewer"


def test_dispatch_all_failed_enqueues_cave_fallback(monkeypatch):
    """Both cloud LLM backends down (kind=all_failed) → the owned cave fallback
    is enqueued (ADR-0005, #310) with the PR coords, AND the degraded path still
    publishes (the errored verdict the connector later heals)."""
    import cave_fallback

    llm = LlmReviewResponse(kind="all_failed", error="both backends failed")
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    calls = []
    monkeypatch.setattr(
        cave_fallback, "enqueue_fallback",
        lambda hunks, **kw: (calls.append((hunks, kw)) or True),
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert len(calls) == 1, "all_failed must enqueue exactly one fallback job"
    hunks, kw = calls[0]
    assert kw["repo"] == "myorg/myrepo"
    assert kw["pr_number"] == 7
    assert kw["head_sha"] == "abcd1234efgh"
    assert len(hunks) >= 1  # the parsed diff hunks were forwarded inline


def test_dispatch_reviewed_does_not_enqueue_fallback(monkeypatch):
    """A healthy review (kind=reviewed) must NOT touch the fallback path."""
    import cave_fallback

    llm = LlmReviewResponse(kind="reviewed", findings=(), backend_used=Backend.POOLSIDE, model_name="m")
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})

    calls = []
    monkeypatch.setattr(cave_fallback, "enqueue_fallback", lambda *a, **kw: calls.append(1))

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert calls == []  # no fallback on a healthy review


def test_fetch_file_contents_url_encodes_path(monkeypatch):
    """#336 follow-up: a changed file with a space/special char must be
    URL-encoded into the contents API path, not interpolated raw (which
    truncates the URL → silent 404 → wrong diff-only degrade)."""
    captured = []

    def cap(url, *, headers, timeout, params=None):
        captured.append(url)
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.text = "x"
        return r

    with patch("httpx.get", side_effect=cap):
        out = cr_dispatch._fetch_file_contents(
            "tok", "o", "r", ("dir/a b#c.py",), "deadbeef"
        )
    assert out == {"dir/a b#c.py": "x"}
    got = captured[0]
    assert "/contents/dir/a%20b%23c.py" in got  # space→%20, #→%23, / kept
    assert " " not in got and "#" not in got.split("?")[0]


def test_elder_no_longer_carries_security_findings(monkeypatch):
    """#466 acceptance: a diff carrying a committed secret produces NO
    security finding on ELDER's surfaces (the security suite moved to
    Guard's own check-run). With the LLM returning zero findings, Elder
    posts a clean check-run and no inline review - even though the diff
    contains a real AWS key shape."""
    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
        review_span_context=None,
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    posted_check, posted_review = [], []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda t, o, r, result, external_id=None: posted_check.append(result) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_review.append(kw) or {},
    )

    secret_diff = (
        "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n"
        "@@ -0,0 +1,1 @@\n"
        "+AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\n"
    )
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = secret_diff
    with patch("httpx.get", return_value=r):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    assert out["result"] == "pass"
    assert posted_check[0].conclusion == "success"  # blocking mode, no findings
    assert posted_review == []  # no inline security comments from Elder


def test_inline_comment_body_renders_committable_suggestion_block():
    """#553: a SINGLE-LINE fence-safe suggestion renders as a GitHub-native
    committable block - the comment anchors one line, so only a one-line
    replacement may carry the Apply button."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="m", suggestion="use(x) if x is not None else None",
    )
    body = cr_dispatch._inline_comment_body(f)
    assert "```suggestion\nuse(x) if x is not None else None\n```" in body
    # dedup marker must remain the LAST marker in the body
    assert body.rstrip().endswith("<!-- grug-rule:null-deref -->")


def test_inline_comment_body_multiline_suggestion_never_committable():
    """#553 audit H2: a multi-line suggestion applied to a single anchored
    line duplicates the following original lines - one-click corruption.
    Multi-line degrades to fenced prose with an explicit scope label."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="m", suggestion="if x is not None:\n    use(x)",
    )
    body = cr_dispatch._inline_comment_body(f)
    assert "```suggestion" not in body
    assert "verify scope before applying" in body


def test_inline_comment_body_fence_bearing_suggestion_is_contained():
    """#553 audit H3: a suggestion containing a ```suggestion payload must
    render INSIDE a longer fence, never as a live committable block - the
    degrade path must not route the payload around the sanitizer."""
    from personas.code_reviewer.persona import Finding
    payload = "```suggestion\nrm -rf /\n```"
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="m", suggestion=payload,
    )
    body = cr_dispatch._inline_comment_body(f)
    assert "````" in body  # containing fence is longer than the payload's
    assert not body.lstrip().startswith("```suggestion")


def test_inline_comment_body_fence_unsafe_suggestion_degrades_to_prose():
    """A suggestion containing a backtick fence must never produce broken
    markdown - degrade to the prose form."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="m", suggestion="use\n```\nfenced\n```\nthing",
    )
    body = cr_dispatch._inline_comment_body(f)
    assert "```suggestion" not in body
    assert "**Fix**" in body


def test_inline_comment_body_effort_chip_and_agent_prompt():
    """#553 / Markings v2: effort chip, category, why-it-matters, CR-style
    agent meta contract on every finding."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=42, severity="medium", rule_name="dead-code",
        message="Grug see unused path", suggestion=None, effort="quick-win",
    )
    body = cr_dispatch._inline_comment_body(f)
    assert "quick win" in body
    assert "Prompt for AI agents" in body
    assert "x.py:42" in body and "dead-code" in body
    # deterministic assembly: the finding message rides the prompt block
    assert "Grug see unused path" in body
    assert "Why it matters" in body
    assert "Verify each finding against the current code" in body
    # dead-code rule maps to maintainability in RULES
    assert "maintainability" in body
    assert cr_dispatch._category_for_rule("dead-code") == "maintainability"


def test_why_it_matters_covers_every_rules_bug_class():
    """Every RULES bug_class must have a specific impact one-liner."""
    from code_review_prompt import RULES
    classes = {r.bug_class for r in RULES}
    missing = classes - set(cr_dispatch._WHY_IT_MATTERS)
    assert not missing, f"missing _WHY_IT_MATTERS keys: {sorted(missing)}"


def test_review_stack_body_has_marker_and_actionable_count():
    """Markings v2 PR-timeline stack comment."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    ev = CodeReviewEvaluation(
        findings=(
            Finding(
                file="a.py", line=1, severity="high", rule_name="null-deref",
                message="npe", suggestion=None,
            ),
            Finding(
                file="b.py", line=2, severity="low", rule_name="dead-code",
                message="unused", suggestion=None,
            ),
        ),
        conclusion="failure",
    )
    body = cr_dispatch._review_stack_body(
        ev, conclusion="neutral", living_range="abc..def", review_phase="tier1",
    )
    assert cr_dispatch._STACK_MARKER in body
    assert "2 actionable marking" in body
    assert "null-deref" in body
    assert "Prompt for AI agents" in body
    assert "Address each finding below" in body
    assert "Looked at: `abc..def`" in body
    # A clear tier-1 pass is PROVISIONAL; saying so is the one piece of phase
    # information a reader needs. The old "Tier-1 coder arm (deep may append
    # later)" said it in vocabulary only this repo understands.
    assert "deep look may add more" in body


def test_review_stack_and_summary_omit_empty_agent_prompt():
    """Clear pass must not ship a hollow 'Address each finding' shell."""
    from personas.code_reviewer.persona import CodeReviewEvaluation
    ev = CodeReviewEvaluation(findings=(), conclusion="success")
    assert cr_dispatch._consolidated_agent_prompt(ev) == ""
    stack = cr_dispatch._review_stack_body(ev, conclusion="success")
    assert "Prompt for AI agents" not in stack
    assert "Address each finding" not in stack
    assert "review degraded" not in stack.lower()
    _, summary = cr_dispatch._summary_markdown(ev)
    assert "Prompt for AI agents" not in summary
    assert "Address each finding" not in summary


def test_clean_body_says_nothing_found_exactly_once():
    """The measured defect: one body stated 'no findings' FIVE times (header,
    fold summary, Status bullet, table placeholder, 'nothing to remediate').
    An email that repeats itself five times reads as broken."""
    from personas.code_reviewer.persona import CodeReviewEvaluation
    body = cr_dispatch._review_stack_body(
        CodeReviewEvaluation(findings=(), conclusion="success"),
        conclusion="success",
    )
    assert "nothing to remediate" not in body.lower()
    assert "No inline markings" not in body
    assert "- Status:" not in body            # restated the fold summary
    assert "Check-run:" not in body           # already a row in the PR checks
    assert body.lower().count("clear") <= 2   # header verdict + fold summary


def test_review_stack_degraded_empty_findings_not_nothing_to_remediate():
    """FLINT #665: degraded + empty findings is not a clean clear pass."""
    from personas.code_reviewer.persona import CodeReviewEvaluation
    ev = CodeReviewEvaluation(
        findings=(),
        conclusion="neutral",
        degraded_reason="all_failed",
    )
    stack = cr_dispatch._review_stack_body(ev, conclusion="neutral")
    assert "nothing to remediate" not in stack.lower()
    assert "review degraded" in stack.lower()
    assert "no usable findings" in stack.lower()


def test_partial_review_stays_advisory_but_publishes_validated_findings():
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

    evaluation = CodeReviewEvaluation(
        findings=(Finding(
            file="src/x.py",
            line=2,
            severity="high",
            rule_name="null-deref",
            message="validated cohort finding",
            suggestion=None,
        ),),
        conclusion="failure",
        degraded_reason="partial_review",
    )

    assert cr_dispatch._publish_shape(
        evaluation, mode="blocking",
    ) == ("neutral", "COMMENT")
    result, posted_findings = cr_dispatch._build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
    )
    assert result is not None
    assert len(result.comments) == 1
    assert posted_findings == evaluation.findings
    title, summary = cr_dispatch._summary_markdown(evaluation)
    assert "coverage partial" in title
    # The findings Elder DID validate are published, not withheld because
    # coverage was incomplete.
    assert "published below" in summary
    # Partial coverage must not borrow the blackout vocabulary: Elder saw
    # most of this diff, so "could not see" would discard real work.
    assert "could not see" not in summary


def test_board_speaks_partial_coverage_not_blackout():
    """One event, one vocabulary. The board used to render partial coverage
    with the total-blackout sentence ("Grug eyes cloudy - read for self"),
    which tells the author to ignore a pass that produced real findings."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

    ev = CodeReviewEvaluation(
        findings=(Finding(file="src/x.py", line=2, severity="medium",
                          rule_name="null-deref", message="m", suggestion=None),),
        conclusion="success",
        degraded_reason="partial_review",
    )

    stack = cr_dispatch._review_stack_body(ev, conclusion="neutral")

    assert "cloudy" not in stack
    assert "could not see" not in stack
    assert "Partial coverage" in stack
    assert "not walked" in stack


def test_board_still_speaks_blackout_for_a_real_outage():
    """Splitting partial coverage out must not soften a genuine blackout."""
    from personas.code_reviewer.persona import CodeReviewEvaluation

    ev = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="all_failed",
    )

    stack = cr_dispatch._review_stack_body(ev, conclusion="neutral")

    assert "cloudy" in stack
    assert "could not see" in stack


def test_summary_markdown_appends_bounded_consolidated_agent_prompt():
    """#553: the check-run summary carries ONE consolidated agent prompt
    covering the findings, bounded so the 65536 body cap stays safe."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    ev = CodeReviewEvaluation(
        findings=tuple(
            Finding(file=f"f{i}.py", line=i + 1, severity="high",
                    rule_name="null-deref", message="m" * 500, suggestion=None)
            for i in range(50)
        ),
        conclusion="failure",
    )
    _, summary = cr_dispatch._summary_markdown(ev)
    assert len(summary) < 60000
    # The truncation must WORK, not merely leave the summary small: whole
    # findings included up to the budget, a visible cut-line for the rest,
    # and never the omission fallback (that is the ceiling path).
    assert "omitted" not in summary
    assert "f0.py:1" in summary
    import re as _re
    m = _re.search(r"\(\+(\d+) more finding", summary)
    assert m and int(m.group(1)) > 0


def test_agent_prompt_blocks_are_fence_breakout_safe():
    """#553 audit: model-supplied message/suggestion containing ``` must not
    break out of the agent-prompt fences and render live markdown - the
    fence grows longer than the longest backtick run inside (CommonMark)."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    hostile = "evil\n```\n[click me](https://x) @maintainer\n```\nmore"
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message=hostile, suggestion=None,
    )
    body = cr_dispatch._inline_comment_body(f)
    # the hostile fence must be CONTAINED: a longer fence opens before it
    assert "````" in body
    ev = CodeReviewEvaluation(findings=(f,), conclusion="failure")
    _, summary = cr_dispatch._summary_markdown(ev)
    assert "````" in summary


def test_chips_cover_the_whole_shared_vocabulary():
    """A level added to review_types must not silently lose its chip.

    This guard used to assert against `_EFFORT_LABELS`, a derived dict that
    NOTHING used - so it could never drift and the test could never fail. The
    dicts actually rendered, `_SEVERITY_CHIP` and `_EFFORT_CHIP`, are
    hand-written and had no guard at all: a new severity would have fallen
    through `.get()` to the bare identifier with nothing to say so. The guard
    was watching a decoy.
    """
    assert cr_dispatch._chip_vocabulary_drift() == {}

    # And it can actually fail - a guard nobody has seen go red is a guess.
    from review_types import SEVERITIES
    original = dict(cr_dispatch._SEVERITY_CHIP)
    try:
        cr_dispatch._SEVERITY_CHIP.pop(next(iter(SEVERITIES)))
        assert cr_dispatch._chip_vocabulary_drift() != {}
    finally:
        cr_dispatch._SEVERITY_CHIP.clear()
        cr_dispatch._SEVERITY_CHIP.update(original)
    assert cr_dispatch._chip_vocabulary_drift() == {}



def test_consolidated_prompt_hard_ceiling_degrades_loudly():
    """#553 audit: a fence-inflating suggestion (huge backtick run) pushes
    the block past the deterministic ceiling - it must degrade to the
    omission notice, never 422 the check-run publish away."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="m", suggestion="`" * 1900,
    )
    ev = CodeReviewEvaluation(
        findings=tuple(
            Finding(file=f"y{i}.py", line=1, severity="high",
                    rule_name="null-deref", message="n" * 1400,
                    suggestion="`" * 1900)
            for i in range(5)
        ) + (f,),
        conclusion="failure",
    )
    out = cr_dispatch._consolidated_agent_prompt(ev)
    assert out == (
        "(Prompt for AI agents omitted - findings too large; "
        "see the table above)"
    ) or len(out) <= 2 * cr_dispatch._CONSOLIDATED_PROMPT_BUDGET


def test_dispatch_suggestion_survives_pipe_to_inline_comment(monkeypatch):
    """#553 E2E: suggestion + effort survive review_diff -> evaluate_diff ->
    dedup -> _build_review_result into the POSTED InlineComment body - a
    dedup/build refactor that rebuilds findings could drop them with every
    unit test still green."""
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="medium", message="Grug see quiet drop",  # type: ignore[arg-type]
            suggestion="log.warning('dropped %s', e)", effort="quick-win",
        ),),
        backend_used=Backend.POOLSIDE,
        model_name="laguna",
    )
    posted_review = []
    monkeypatch.setattr(cr_dispatch, "review_diff",
                        lambda *a, **k: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run",
                        lambda *a, **k: {"id": 1})
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda t, o, r, *, pull_number, result: posted_review.append(result) or {"id": 2},
    )
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    assert len(posted_review) == 1
    body = posted_review[0].comments[0].body
    assert "```suggestion\nlog.warning('dropped %s', e)\n```" in body
    assert "quick win" in body
    assert "Prompt for AI agents" in body


def test_committable_suggestion_preserves_leading_indentation():
    """#553 audit stage 8: GitHub commits the block verbatim as the full
    replacement line - stripping leading whitespace one-clicks an
    IndentationError into the file."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="m", suggestion="    return use(x)",
    )
    body = cr_dispatch._inline_comment_body(f)
    assert "```suggestion\n    return use(x)\n```" in body


def test_unterminated_fence_in_message_cannot_swallow_the_body():
    """FLINT on #558: an UNBALANCED ``` in the message head (prose, unfenced
    by design) must not open a fence that eats the suggestion block and the
    dedup marker."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="evil ``` unterminated", suggestion="use(x)",
    )
    body = cr_dispatch._inline_comment_body(f)
    assert "```suggestion\nuse(x)\n```" in body
    assert body.rstrip().endswith("<!-- grug-rule:null-deref -->")
    # the head's backtick run was defused below fence-capability
    head = body.split("**Fix", 1)[0]
    assert "```" not in head


def test_all_newline_suggestion_never_produces_empty_committable_block():
    """FLINT on #558: a suggestion of only newlines passes the single-line
    check (no literal \\n after an initial .strip()) but strips to empty -
    committing it would replace the line with a BLANK line. Must degrade,
    never emit an empty ```suggestion``` block."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="m", suggestion="\n\n\n",
    )
    body = cr_dispatch._inline_comment_body(f)
    assert "```suggestion\n\n```" not in body
    assert "```suggestion" not in body


def test_summary_markdown_escapes_hostile_finding_fields():
    """LLM-controlled file/rule/message must not break Markings table or code spans."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    ev = CodeReviewEvaluation(
        findings=(
            Finding(
                file="evil`path|x.py",
                line=1,
                severity="high",
                rule_name="rule|with`ticks",
                message="msg with | pipe and\nnewline",
                suggestion=None,
            ),
        ),
        conclusion="failure",
    )
    _, summary = cr_dispatch._summary_markdown(ev)
    # Table cells: backticks stripped from code spans; pipes escaped in message.
    assert "`evilpath|x.py`" in summary
    assert "`rule|withticks`" in summary
    assert "msg with \\| pipe and newline" in summary
    assert summary.count("| Severity | Effort | File | Line | Rule | Marking |") == 1


def test_inline_comment_body_defuses_tilde_fences_in_message():
    """A model message with ~~~ must not open a GFM fence in unfenced prose."""
    from personas.code_reviewer.persona import Finding
    f = Finding(
        file="x.py", line=1, severity="high", rule_name="null-deref",
        message="~~~\nsecret\n~~~", suggestion=None,
    )
    body = cr_dispatch._inline_comment_body(f)
    # Prose head (before agent-prompt details) must not carry a live fence.
    head = body.split("<details>", 1)[0]
    assert "~~~" not in head
    assert "**Where:**" in head
    assert "<!-- grug-rule:null-deref -->" in body


def test_dispatch_verification_kills_contradicted_finding(monkeypatch, caplog):
    """#708 wiring: a judge-approved finding whose claim the repo evidence
    contradicts (async-family rule anchored in a provably-sync def) is
    killed before publication, with one structured log row carrying the
    machine-readable reason."""
    import logging

    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="sync-io-in-async",
            severity="high", message="blocking sleep stalls the event loop",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "rs1"},
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    # Judge says REAL - verification must still kill on repo evidence.
    monkeypatch.setattr(
        cr_dispatch, "grade_findings",
        lambda *a, **kw: (FindingJudgement(0, True, "real", 0.9),),
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_file_contents",
        lambda token, owner, repo, paths, sha: {
            "src/x.py": "def handler(fn):\n    fn()\n    return 1\n",
        },
    )
    posted_reviews: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_reviews.append(kw) or {},
    )

    with patch("httpx.get", return_value=_diff_response()), \
         caplog.at_level(logging.INFO):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert posted_reviews == []  # killed finding produced no inline review
    assert out["result"] == "pass"
    row = next(
        r for r in caplog.records
        if r.getMessage() == "code_review_verification_killed"
    )
    assert row.reason == "sync_context"
    assert row.rule == "sync-io-in-async"
    assert row.path == "src/x.py"


def test_dispatch_refute_gate_kills_confidently_refuted_high_finding(monkeypatch, caplog):
    """#714 wiring: a HIGH finding that survives the plausibility judge and
    deterministic verification but is confidently refuted by the evidence
    gate is killed before publication (reason 'refuted'); medium findings
    never reach the gate."""
    import logging

    from llm_client import FindingJudgement as FJ

    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(
            LlmFinding(
                path="src/x.py", line=2, rule="inverted-logic",
                severity="high", message="condition reads backwards",  # type: ignore[arg-type]
            ),
            LlmFinding(
                path="src/x.py", line=3, rule="nit",
                severity="medium", message="minor style point",  # type: ignore[arg-type]
            ),
        ),
        backend_used=Backend.POOLSIDE,
        review_span_context={"span_id": "rs1"},
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(
        cr_dispatch, "grade_findings",
        lambda *a, **kw: (FJ(0, True, "plausible", 0.9), FJ(1, True, "real nit", 0.9)),
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_file_contents",
        lambda token, owner, repo, paths, sha: {"src/x.py": "x = 1\ny = 2\nz = 3\n"},
    )
    refute_calls = []

    def _fake_refute(findings, hunks, installation_id, **kw):
        refute_calls.append(findings)
        return (FJ(0, False, "quoted code shows the opposite of the claim", 0.95),)

    monkeypatch.setattr(cr_dispatch, "refute_findings", _fake_refute)
    posted_reviews: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_reviews.append(kw) or {},
    )

    with patch("httpx.get", return_value=_diff_response()), \
         caplog.at_level(logging.INFO):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # Only the HIGH finding reached the refute gate.
    assert len(refute_calls) == 1
    assert [f.severity for f in refute_calls[0]] == ["high"]
    # The refuted high finding died; the medium survived and published.
    assert len(posted_reviews) == 1
    row = next(
        r for r in caplog.records
        if r.getMessage() == "code_review_verification_killed" and r.reason == "refuted"
    )
    assert row.rule == "inverted-logic"
    assert out["result"] == "pass"


# --- #730: deep review capture + stack refresh --------------------------------


def _deep_llm_with_finding(rule="deep-bug", severity="high"):
    return LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule=rule,
            severity=severity, message="deep finding",  # type: ignore[arg-type]
            suggestion="fix it", effort="quick-win",
        ),),
        backend_used=Backend.POOLSIDE,
        model_name="laguna",
        review_span_context={"span_id": "deep-span", "trace_id": "deep-trace"},
    )


def test_deep_review_publishes_and_captures_comment_records(monkeypatch):
    """#730: a successful async deep finding is stored with its GitHub
    comment ID and stable finding identity, so the reaction/reply learning
    loops can attach human verdicts to the hardest-won findings."""
    deep_llm = _deep_llm_with_finding()
    # Tier-1 returns no findings; deep returns the finding.
    tier1_llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
        model_name="laguna",
    )
    monkeypatch.setattr(
        cr_dispatch, "review_diff",
        lambda *a, **kw: tier1_llm,
    )
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    posted_reviews = []
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **kw: posted_reviews.append(kw["result"]) or {"id": 77},
    )
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [{"id": 555, "path": "src/x.py", "line": 2,
                           "body": "m\n<!-- grug-rule:deep-bug -->"}],
    )
    captured = []
    monkeypatch.setattr(
        cr_dispatch, "put_comment_record",
        lambda **kw: captured.append(kw),
    )
    # Force deep escalation.
    monkeypatch.setattr(cr_dispatch, "review_is_staged", lambda hunks: False)
    monkeypatch.setattr(
        cr_dispatch, "decide_deep_escalation",
        lambda hunks, pr_context: MagicMock(escalate=True, reasons=["test"], added_lines=5),
    )
    monkeypatch.setattr(
        cr_dispatch, "review_reasoner_diff",
        lambda *a, **kw: deep_llm,
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_file_contents",
        lambda token, owner, repo, paths, sha: {"src/x.py": "def f():\n    pass\n"},
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a: ("snap", "abcd1234efgh", "open", False),
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # The deep review posted.
    assert len(posted_reviews) == 1
    # The deep finding's comment was captured with its ID + span context.
    assert len(captured) == 1
    rec = captured[0]
    assert rec["comment_id"] == 555
    assert rec["review_span_context"] == {"span_id": "deep-span", "trace_id": "deep-trace"}
    assert rec["finding_tags"]["rule_name"] == "deep-bug"
    assert rec["author_login"] == "evan"


def test_deep_review_capture_failure_does_not_change_result(monkeypatch):
    """#730: a capture failure (GH 5xx or DDB error) must be swallowed -
    the deep review already posted, so dispatch's outer result is unaffected.
    Exercises the outer try/except in _capture_review_comments around the
    get_review_comments fetch (not the inner per-comment put)."""
    deep_llm = _deep_llm_with_finding()
    tier1_llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
        model_name="laguna",
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: tier1_llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())

    def _gh_boom(*a, **kw):
        raise RuntimeError("GH 5xx")
    monkeypatch.setattr(cr_dispatch, "get_review_comments", _gh_boom)
    monkeypatch.setattr(cr_dispatch, "review_is_staged", lambda hunks: False)
    monkeypatch.setattr(
        cr_dispatch, "decide_deep_escalation",
        lambda hunks, pr_context: MagicMock(escalate=True, reasons=["t"], added_lines=5),
    )
    monkeypatch.setattr(cr_dispatch, "review_reasoner_diff", lambda *a, **kw: deep_llm)
    monkeypatch.setattr(
        cr_dispatch, "_fetch_file_contents",
        lambda token, owner, repo, paths, sha: {"src/x.py": "def f():\n    pass\n"},
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a: ("snap", "abcd1234efgh", "open", False),
    )

    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # Deep review posted and capture failed (GH 5xx), but dispatch did not raise.
    assert out["result"] == "pass"


def test_deep_review_refreshes_stack_comment_with_tier1_findings(monkeypatch):
    """#730: the PR-timeline stack comment is refreshed after deep findings
    publish, so the canonical projection includes deep findings.

    Regression for FLINT finding: the deep-phase stack refresh must
    use the combined (Tier-1 + deep) evaluation, not just novel_deep, so
    Tier-1 findings are preserved in the refreshed stack body."""
    deep_llm = _deep_llm_with_finding()
    # Tier-1 returns a finding that should be preserved in the deep stack.
    tier1_llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="tier1-bug",
            severity="low", message="tier1 finding",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
        model_name="laguna",
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: tier1_llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 77})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "get_review_comments",
        lambda *a, **kw: [{"id": 9, "path": "src/x.py", "line": 2,
                           "body": "m\n<!-- grug-rule:deep-bug -->"}],
    )
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: None)
    stack_bodies = []
    monkeypatch.setattr(
        cr_dispatch, "_upsert_review_stack_comment",
        # **kw, not a fixed arg list: the real signature grew create_if_absent,
        # and a double that rejects it raises TypeError straight into the
        # caller's `except Exception` - where it looks like an upsert failure
        # rather than a broken test.
        lambda token, owner, repo, pr, body, **kw: stack_bodies.append(body),
    )
    monkeypatch.setattr(cr_dispatch, "review_is_staged", lambda hunks: False)
    monkeypatch.setattr(
        cr_dispatch, "decide_deep_escalation",
        lambda hunks, pr_context: MagicMock(escalate=True, reasons=["t"], added_lines=5),
    )
    monkeypatch.setattr(cr_dispatch, "review_reasoner_diff", lambda *a, **kw: deep_llm)
    monkeypatch.setattr(
        cr_dispatch, "_fetch_file_contents",
        lambda token, owner, repo, paths, sha: {"src/x.py": "def f():\n    pass\n"},
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a: ("snap", "abcd1234efgh", "open", False),
    )

    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    # The stack was refreshed: Tier-1 + deep append (2 total).
    assert len(stack_bodies) == 2
    # The last (deep) body must include BOTH Tier-1 and deep findings.
    deep_body = stack_bodies[-1]
    assert "deep" in deep_body.lower()
    assert "deep-bug" in deep_body
    assert "tier1-bug" in deep_body  # Tier-1 finding preserved via combined_eval


# --- the stack comment IS the board (#791) ----------------------------------

def test_stack_body_is_a_board_with_a_verdict_first():
    """GitHub mails comment CREATION only, so this body IS the email. It must
    lead with a conclusion, not a table."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    ev = CodeReviewEvaluation(findings=(
        Finding(file="a.py", line=1, severity="high", rule_name="null-deref",
                message="npe", suggestion=None),
    ), conclusion="failure")
    body = cr_dispatch._review_stack_body(ev, conclusion="failure", pr_title="#1 thing")
    lines = [l for l in body.splitlines() if l.strip()]
    assert lines[0] == cr_dispatch.board.BOARD_MARKER   # findable, else re-POST
    assert "WAIT" in lines[1]                           # verdict before evidence
    assert "**1 block**" in body
    assert "_#1 thing_" in body


def test_worth_an_email_is_findings_or_degraded_only():
    """The gate on whether grug may CREATE a comment - i.e. whether it may
    mail the author. A clean review is not news; the green check-run already
    says so. A degraded review IS news in the other direction, because silence
    there would be misread as approval.

    The degraded exemplar is a TERMINAL one. It used to be `all_failed`, which
    the durable lane retries - so that assertion was pinning the behaviour
    that mailed a doomed verdict six minutes before the retry produced the
    real one (infra#2157). The guarantee this test exists for is unchanged:
    a last-word failure still mails.
    """
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    f = Finding(file="a.py", line=1, severity="low", rule_name="r",
                message="m", suggestion=None)
    assert not cr_dispatch.worth_an_email(
        CodeReviewEvaluation(findings=(), conclusion="success"))
    assert cr_dispatch.worth_an_email(
        CodeReviewEvaluation(findings=(f,), conclusion="failure"))
    assert cr_dispatch.worth_an_email(CodeReviewEvaluation(
        findings=(), conclusion="neutral",
        degraded_reason="degraded_no_backend"))


def test_no_diff_is_not_an_alarm():
    """Nothing to review is not a failure to review.

    Observed live on grug#801: an empty re-trigger commit produced "Grug eyes
    cloudy this pass - read for self", which reads as "the reviewer broke" for
    a PR where simply nothing had changed - and would have created a board,
    and mailed. dispatch already treats `no_diff` as equivalent to clean at
    both publish gates; this makes the email path agree.
    """
    from personas.code_reviewer.persona import CodeReviewEvaluation
    nod = CodeReviewEvaluation(findings=(), conclusion="neutral",
                               degraded_reason="no_diff")
    assert not cr_dispatch.worth_an_email(nod)
    body = cr_dispatch._review_stack_body(nod, conclusion="neutral", pr_title="t")
    assert "cloudy" not in body and "Trail clear" in body
    assert "Review degraded" not in body

    # A REAL degradation must still alarm and still mail - the fix must not
    # silence the case where silence gets read as approval. Uses a TERMINAL
    # reason: a retried one is not the last word, so it no longer mails
    # (see worth_an_email / infra#2157). The BODY still says "cloudy" for any
    # blackout, retried or not - that surface is an edit, not an email.
    bad = CodeReviewEvaluation(findings=(), conclusion="neutral",
                               degraded_reason="degraded_no_backend")
    assert cr_dispatch.worth_an_email(bad)
    body = cr_dispatch._review_stack_body(bad, conclusion="neutral", pr_title="t")
    assert "cloudy" in body and "Review degraded" in body

    retried = CodeReviewEvaluation(findings=(), conclusion="neutral",
                                   degraded_reason="all_failed")
    assert not cr_dispatch.worth_an_email(retried), (
        "a retry is queued; let it speak instead of mailing a doomed verdict"
    )


def test_clean_review_posts_nothing_when_no_board_exists(monkeypatch):
    """No news + no existing board -> no POST, therefore no email. This is
    the whole fix: five consecutive PRs each mailed a 'Trail clear' that
    carried no information."""
    monkeypatch.setattr(cr_dispatch, "_find_stack_comment_id",
                        lambda *a, **k: None)
    posted = []
    monkeypatch.setattr(cr_dispatch.httpx, "post",
                        lambda *a, **k: posted.append(a) or None)
    cr_dispatch._upsert_review_stack_comment(
        "tok", "o", "r", 5, "no board marker here", create_if_absent=False,
    )
    assert posted == []


def test_clean_review_still_updates_an_existing_board(monkeypatch):
    """Editing is free (GitHub never mails an edit), so a board left saying
    '3 findings' after they were fixed must still be corrected. Silence is
    about not RINGING the phone, not about leaving stale claims up."""
    monkeypatch.setattr(cr_dispatch, "_find_stack_comment_id",
                        lambda *a, **k: 42)
    patched = []
    monkeypatch.setattr(
        cr_dispatch.httpx, "patch",
        lambda url, **k: patched.append(url) or _ok_response())
    cr_dispatch._upsert_review_stack_comment(
        "tok", "o", "r", 5, "no board marker here", create_if_absent=False,
    )
    assert patched and patched[0].endswith("/issues/comments/42")


def _ok_response():
    import httpx as _h
    return _h.Response(200, json={}, request=_h.Request("PATCH", "https://x"))


def test_clean_review_body_is_three_lines_and_has_no_empty_fold():
    """A clean pass has no evidence, so it gets no fold. An empty <details> is
    a disclosure triangle hiding nothing, which reads as a broken tool."""
    from personas.code_reviewer.persona import CodeReviewEvaluation
    body = cr_dispatch._review_stack_body(
        CodeReviewEvaluation(findings=(), conclusion="success"),
        conclusion="success", pr_title="#2 bump",
    )
    assert "Trail clear" in body
    assert "block" not in body                          # no counts on a clean pass
    assert "<details" not in body
    # Count only what a reader SEES: the region delimiters are HTML comments
    # and render as nothing.
    visible = [l for l in body.splitlines()
               if l.strip() and not l.strip().startswith("<!--")]
    assert len(visible) == 2, visible                    # verdict + PR title


def test_table_and_detail_bullets_are_blank_line_separated():
    """A list starting on the line after a table row is swallowed INTO the
    table by GitHub's markdown - the bullets silently vanish."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    body = cr_dispatch._review_stack_body(
        CodeReviewEvaluation(findings=(
            Finding(file="a.py", line=1, severity="low", rule_name="r",
                    message="m", suggestion=None),
        ), conclusion="failure"),
        conclusion="failure", suppressed_count=1,
    )
    lines = body.splitlines()
    bullet = next(i for i, l in enumerate(lines) if l.startswith("- Grug swallow"))
    assert lines[bullet - 1].strip() == ""


def test_findings_table_is_capped_so_the_mail_stays_short():
    """<details> does NOT fold in Gmail (verified against the raw notification
    HTML for grug#799), so nothing but brevity keeps the mail readable."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    many = tuple(
        Finding(file=f"f{i}.py", line=i, severity="low", rule_name="r",
                message="m", suggestion=None)
        for i in range(1, 31)          # line >= 1; Finding rejects 0
    )
    body = cr_dispatch._review_stack_body(
        CodeReviewEvaluation(findings=many, conclusion="failure"),
        conclusion="failure",
    )
    assert body.count("| low |") == cr_dispatch._STACK_TABLE_ROWS
    assert f"+{30 - cr_dispatch._STACK_TABLE_ROWS} more" in body


def test_findings_open_the_section_clean_leaves_it_folded():
    """Something to fix -> open. Nothing to fix -> no clicks, no scrolling."""
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    blocking = CodeReviewEvaluation(findings=(
        Finding(file="a.py", line=1, severity="high", rule_name="r",
                message="m", suggestion=None),
    ), conclusion="failure")
    assert "<details open>" in cr_dispatch._review_stack_body(blocking, conclusion="failure")
    clean = CodeReviewEvaluation(findings=(), conclusion="success")
    assert "<details open>" not in cr_dispatch._review_stack_body(clean, conclusion="success")


def test_elder_body_is_mergeable_by_the_board_client():
    """What Elder PRODUCES must be something board_client can MERGE.

    It was not. The body was a flat `marker + header + content` string with no
    region delimiters, so `extract_section(body, "elder")` returned None,
    `_upsert_review_stack_comment` skipped the merge path, and Elder PATCHed
    the COMPLETE body - deleting Chief's section on every pass. Confirmed
    against the live boards on #797/#798/#799: `grug-board` present, not one
    `grug-sec:` region among them.

    The board_client unit tests all passed throughout, because they call the
    client directly. Nothing asserted the two halves fit together.
    """
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
    for ev in (
        CodeReviewEvaluation(findings=(
            Finding(file="a.py", line=1, severity="high", rule_name="r",
                    message="m", suggestion=None),
        ), conclusion="failure"),
        CodeReviewEvaluation(findings=(), conclusion="success"),
        CodeReviewEvaluation(findings=(), conclusion="neutral",
                             degraded_reason="all_failed"),
    ):
        body = cr_dispatch._review_stack_body(ev, conclusion="neutral",
                                              pr_title="t")
        assert cr_dispatch.board.extract_section(body, "elder") is not None
        assert cr_dispatch.board.extract_header(body) is not None


def test_elder_pass_does_not_delete_chiefs_section():
    """End-to-end on the two modules together: Chief writes, Elder writes,
    Chief's words survive. This is the property #797 claimed and did not have."""
    from personas import board, board_client
    from personas.code_reviewer.persona import CodeReviewEvaluation
    existing = board.upsert_section(
        board.set_header(board.new_board(), "### old"), "chief", "CHIEF-SAYS-THIS")
    elder_body = cr_dispatch._review_stack_body(
        CodeReviewEvaluation(findings=(), conclusion="success"),
        conclusion="success", pr_title="t",
    )
    merged = board.upsert_section(
        existing, "elder", board.extract_section(elder_body, "elder") or "")
    assert "CHIEF-SAYS-THIS" in merged
    assert board_client.LEGACY_MARKERS      # legacy path still reachable


def test_legacy_elder_stack_comment_is_still_located():
    """PRs reviewed before this change carry an elder-stack comment. Failing
    to find it would post a board NEXT to it - the duplicate this exists to
    prevent."""
    assert cr_dispatch._LEGACY_STACK_MARKER == "<!-- grug-elder-stack -->"
    assert cr_dispatch._STACK_MARKER == cr_dispatch.board.BOARD_MARKER


def test_degraded_review_does_not_claim_clear():
    from personas.code_reviewer.persona import CodeReviewEvaluation
    ev = CodeReviewEvaluation(findings=(), conclusion="neutral",
                              degraded_reason="all_failed")
    body = cr_dispatch._review_stack_body(ev, conclusion="neutral")
    assert "Trail clear" not in body and "cloudy" in body


# --- infra#2157: do not mail a verdict a retry is about to overwrite --------


def test_no_board_for_a_degradation_that_will_be_retried():
    """A retried degradation is not news YET.

    On quadseven/infra#2157 the LLM half was cancelled mid-flight
    (`all_failed`), the deterministic complexity scanner still produced one
    finding, and Elder created the board on the strength of it - mailing
    "Grug eyes cloudy this pass - read for self" plus a cyclomatic nitpick on
    a PR that fixes a packet-mode bootstrap deadlock. The retry six minutes
    later came back "no bad omens from the Cave", but GitHub does not mail an
    EDIT, so that inbox keeps the degraded verdict forever.

    `all_failed` is in the durable lane's retry set, so a better answer is
    always coming. Wait for it.
    """
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

    complexity_only = CodeReviewEvaluation(
        findings=(Finding(file="a.py", line=677, severity="medium",
                          rule_name="null-deref", message="tangled",
                          suggestion=None),),
        conclusion="neutral",
        degraded_reason="all_failed",
    )
    assert cr_dispatch.worth_an_email(complexity_only) is False, (
        "deterministic findings must not carry a board when the LLM half "
        "failed and a retry is queued - the retry republishes them anyway"
    )


def test_terminal_blackout_still_earns_a_board():
    """A degradation NOT in the retry set is the last word, so silence would
    be misread as approval."""
    from personas.code_reviewer.persona import CodeReviewEvaluation

    terminal = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="degraded_no_backend",
    )
    assert cr_dispatch.worth_an_email(terminal) is True


def test_real_findings_on_a_healthy_pass_still_earn_a_board():
    from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

    ev = CodeReviewEvaluation(
        findings=(Finding(file="a.py", line=1, severity="high",
                          rule_name="sync-io-in-async", message="m",
                          suggestion=None),),
        conclusion="failure",
    )
    assert cr_dispatch.worth_an_email(ev) is True


def test_stack_discloses_scope_on_a_full_diff_run_too():
    """#673 item 2: 'what did you look at' must have an answer on EVERY run.

    `- Looked at: <range>` was emitted only when a Living Hunt delta existed.
    A full base..head review - the common path, and every first review of a
    PR - disclosed nothing at all about its scope.
    """
    delta = cr_dispatch._stack_detail_lines("deep", "aaa..bbb", 0, "c0ffee1234")
    assert "aaa..bbb" in delta

    full = cr_dispatch._stack_detail_lines(
        "deep", "", 0, "c0ffee1234", "base987654",
    )
    assert "Looked at" in full, "a full-diff run must still say what it read"
    assert "base987..c0ffee1" in full, "disclose the RANGE, not one commit"


def test_stack_detail_without_a_head_sha_still_renders():
    """Callers that have no sha to hand must not crash or emit a dangling
    'Looked at:' with nothing after it."""
    out = cr_dispatch._stack_detail_lines("deep", "", 0)
    assert "Looked at" not in out
    assert "None" not in out


# --- grug#891: name WHY an HTTP call failed, not just its class -------------


def test_http_error_detail_names_status_url_and_body():
    """grug#891: the real incident logged only `kind=HTTPStatusError`. This is
    the exact GitHub secondary-rate-limit shape that cost two days to find -
    a 403 on /compare while /pulls returned 200 on the same token."""
    import httpx
    from personas.code_reviewer.dispatch import _http_error_detail

    request = httpx.Request(
        "GET",
        "https://api.github.com/repos/quadseven/infra/compare/c7bc534a...d853fe16",
    )
    response = httpx.Response(
        403, request=request,
        headers={"retry-after": "60", "x-ratelimit-remaining": "0"},
        text='{"message":"You have exceeded a secondary rate limit"}',
    )
    detail = _http_error_detail(
        httpx.HTTPStatusError("403", request=request, response=response)
    )
    assert detail["status"] == 403
    assert "secondary rate limit" in str(detail["body"])
    assert "/compare/" in str(detail["url"])
    assert detail["retry_after"] == "60"
    assert detail["x_ratelimit_remaining"] == "0"


def test_http_error_detail_bounds_a_large_body():
    """An upstream HTML interstitial must not flood the log."""
    import httpx
    from personas.code_reviewer.dispatch import _http_error_detail

    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(502, request=request, text="x" * 50_000)
    detail = _http_error_detail(
        httpx.HTTPStatusError("502", request=request, response=response)
    )
    assert len(str(detail["body"])) <= 300


def test_http_error_detail_handles_an_error_with_no_response():
    """A RequestError (DNS, connect reset) carries no response - the helper
    must still return the message rather than raise inside a log call."""
    import httpx
    from personas.code_reviewer.dispatch import _http_error_detail

    detail = _http_error_detail(
        httpx.ConnectError("nodename nor servname provided")
    )
    assert "nodename" in str(detail["err"])
    assert "status" not in detail


# --- grug#770: a permanent 4xx on review-publish must not redrive -----------


def _one_finding_llm() -> LlmReviewResponse:
    return LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="x", severity="medium", message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )


def _review_status_error(
    status: int, body: str = "", headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST", "https://api.github.com/repos/myorg/myrepo/pulls/7/reviews",
    )
    response = httpx.Response(
        status, request=request, text=body, headers=headers or {},
    )
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def _dispatch_with_review_error(monkeypatch, error: Exception):
    """Run one advisory dispatch whose inline-review POST raises `error`.
    Returns (result dict, number of post_review attempts)."""
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _one_finding_llm())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    attempts: list[object] = []

    def _raise(*a, **kw):
        attempts.append(kw.get("result"))
        raise error

    monkeypatch.setattr(cr_dispatch, "post_review", _raise)
    with patch("httpx.get", return_value=_diff_response()):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)
    return out, len(attempts)


def test_dispatch_review_publish_422_is_rejected_not_publish_failed(monkeypatch, caplog):
    """grug#770: GitHub 422s the WHOLE review when one inline comment sits
    outside the diff. The payload is identical on every attempt, so the
    answer is too - four Elder jobs redrove 5x each into the DLQ on it. A
    deterministic 4xx must complete as `review_rejected` (the check-run
    already landed; only the advisory surface was lost), never as the
    `publish_failed` value rerun raises on. The GitHub error body must be
    on the log line so the next incident is diagnosable from one record."""
    body = (
        '{"message":"Unprocessable Entity","errors":['
        '"Pull request review thread line must be part of the diff"]}'
    )
    with caplog.at_level("ERROR"):
        out, attempts = _dispatch_with_review_error(
            monkeypatch, _review_status_error(422, body),
        )

    assert out["result"] == "review_rejected"
    assert attempts == 1
    records = [
        r for r in caplog.records
        if r.message == "code_review_review_publish_failed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.status == 422
    assert record.permanent is True
    assert "must be part of the diff" in record.body
    assert "/pulls/7/reviews" in record.url


@pytest.mark.parametrize("status", [500, 502, 503])
def test_dispatch_review_publish_5xx_still_publish_failed(monkeypatch, status):
    """A 5xx is GitHub's problem, not the payload's: the durable lane must
    still raise for redrive, exactly as before grug#770."""
    out, attempts = _dispatch_with_review_error(
        monkeypatch, _review_status_error(status, "upstream brownout"),
    )
    assert out["result"] == "publish_failed"
    assert attempts == 1


def test_dispatch_review_publish_429_still_publish_failed(monkeypatch):
    """429 is a 4xx that IS transient (rate limit): the identical payload
    succeeds once the window resets, so it keeps redriving."""
    out, _ = _dispatch_with_review_error(
        monkeypatch,
        _review_status_error(
            429, '{"message":"API rate limit exceeded"}',
            headers={"retry-after": "60"},
        ),
    )
    assert out["result"] == "publish_failed"


def test_dispatch_review_publish_transport_error_still_publish_failed(monkeypatch):
    """A transport error carries no verdict on the payload at all."""
    out, _ = _dispatch_with_review_error(
        monkeypatch, httpx.ConnectError("reviews API down"),
    )
    assert out["result"] == "publish_failed"


def test_dispatch_review_publish_failed_log_names_status_and_body(monkeypatch, caplog):
    """Acceptance (grug#770): the GitHub error response body is logged on
    publish failure - on the retryable branch too, not only the permanent
    one. `kind=HTTPStatusError` alone is the grug#891 blind spot again."""
    with caplog.at_level("ERROR"):
        _dispatch_with_review_error(
            monkeypatch, _review_status_error(502, "<html>bad gateway</html>"),
        )
    record = next(
        r for r in caplog.records
        if r.message == "code_review_review_publish_failed"
    )
    assert record.status == 502
    assert record.permanent is False
    assert "bad gateway" in record.body


@pytest.mark.parametrize(
    ("status", "headers", "permanent"),
    [
        (400, {}, True),
        (403, {}, True),
        (404, {}, True),
        (422, {}, True),
        # A 403 that names a rate limit is GitHub's secondary throttle
        # (grug#891 shape), not a verdict on the payload.
        (403, {"retry-after": "60"}, False),
        (403, {"x-ratelimit-remaining": "0"}, False),
        (429, {}, False),
        (500, {}, False),
        (502, {}, False),
    ],
)
def test_is_permanent_rejection_classifies_status(status, headers, permanent):
    assert cr_dispatch._is_permanent_rejection(
        _review_status_error(status, headers=headers),
    ) is permanent


def test_is_permanent_rejection_is_false_without_a_response():
    """Transport errors have no status to judge; they stay retryable. And a
    bare ConnectError's `.request` property RAISES (grug#891) - the classifier
    must not blow up inside an except block."""
    assert cr_dispatch._is_permanent_rejection(
        httpx.ConnectError("reset"),
    ) is False


def test_resolve_result_rejected_review_precedence():
    """`review_rejected` is a completed pass minus its advisory surface. It
    must never mask a real publish failure (redrive) or a degraded eval
    (whose degradation owns the retry decision)."""
    from personas.code_reviewer.persona import CodeReviewEvaluation

    clean = CodeReviewEvaluation(findings=(), conclusion="success")
    assert cr_dispatch._resolve_result(
        clean, check_publish_failed=False, review_publish_rejected=True,
    ) == "review_rejected"
    assert cr_dispatch._resolve_result(
        clean, check_publish_failed=True, review_publish_rejected=True,
    ) == "publish_failed"
    assert cr_dispatch._resolve_result(
        clean, check_publish_failed=False, review_publish_failed=True,
        review_publish_rejected=True,
    ) == "publish_failed"
    degraded = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="all_failed",
    )
    assert cr_dispatch._resolve_result(
        degraded, check_publish_failed=False, review_publish_rejected=True,
    ) == "skipped"


def test_summary_does_not_call_an_unopened_cohort_a_failed_one():
    """grug#939: a cohort the scheduler never reached was announced to the
    author as `failed: N`, which reads as "Grug reviewed it and it broke".
    A re-run can fix that; it can never fix ground nobody opened."""
    from personas.code_reviewer.persona import CodeReviewEvaluation

    coverage = ReviewCoverage(
        total_cohorts=4,
        completed_cohorts=1,
        failed_cohorts=(2, 3, 4),
        unattempted_cohorts=(3, 4),
        cohort_labels=("a", "b", "c", "d"),
    )
    ev = CodeReviewEvaluation(
        findings=(),
        conclusion="neutral",
        degraded_reason="partial_review",
        coverage=coverage,
    )

    _, summary = cr_dispatch._summary_markdown(ev)

    assert "Coverage: 1/4 cohorts" in summary
    # Only cohort 2 actually ran and failed.
    assert "failed: 2" in summary
    assert "never attempted: 3, 4" in summary
    assert "failed: 2, 3, 4" not in summary
