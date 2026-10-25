"""Teller dispatch tests (#554): fetch (incl. phase-tagged failure logging),
LLM-summary fallback, upsert-by-marker (PATCH-else-POST, incl. scan-cap-
exhaustion warning), and the honest degrade-comment path (incl. the
summary_degraded gauge). Network mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from personas.walkthrough import dispatch as wt_dispatch
from personas.walkthrough.render import MARKER

_OWN_APP_ID = "1"  # matches performed_via_github_app.id in the fixtures below


@pytest.fixture(autouse=True)
def _stub_own_app_id(monkeypatch):
    """Every test exercises _find_marker_comment's own-app-identity check
    (#554 peer review round 3) - stub it once rather than per-test, since
    the real get_app_id() reads GITHUB_APP_ID_SSM (unset in this env)."""
    monkeypatch.setattr(wt_dispatch, "get_app_id", lambda: _OWN_APP_ID)


def _payload(pull_number: int = 7, head_sha: str = "a" * 40) -> dict:
    return {
        "action": "opened",
        "pull_request": {"number": pull_number, "head": {"sha": head_sha}},
        "repository": {"owner": {"login": "o"}, "name": "r"},
        "installation": {"id": 1},
    }


def _files_response(files: list[dict]) -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=files)
    return r


def _diff_response(text: str = "diff --git a/x.py b/x.py\n") -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = text
    return r


def _empty_comments_response() -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=[])
    return r


def _marker_comment_response(comment_id: int) -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=[{
        "id": comment_id, "body": MARKER,
        "performed_via_github_app": {"id": 1, "slug": "grug"},
    }])
    return r


def test_dispatch_posts_new_comment_when_no_marker_exists(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    posted = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([
                {"filename": "x.py", "additions": 3, "deletions": 1},
            ])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    def fake_post(url, **kwargs):
        posted["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "degraded"}
    assert MARKER in posted["body"]
    assert "x.py" in posted["body"]
    mock_rcv.assert_called_once()
    assert mock_rcv.call_args.kwargs["conclusion"] == "success"


def test_dispatch_records_the_walkthrough_in_the_canonical_review_session(monkeypatch):
    """#1207: the published summary and related hunts also land in the PR's
    review session, which the Hunt Board renders."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    recorded = []

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([
                {"filename": "x.py", "additions": 3, "deletions": 1},
            ])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    def fake_post(url, **kwargs):
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch.object(
             wt_dispatch.hunt_recorder, "record_walkthrough",
             side_effect=lambda ref, **kw: recorded.append((ref, kw)),
         ), \
         patch("llm_client.summarize_pr", return_value=None):
        wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    [(ref, kw)] = recorded
    assert (ref.install_id, ref.owner, ref.repo, ref.pr_number) == (1, "o", "r", 7)
    assert kw["head_sha"] == "a" * 40 and kw["trigger"] == "opened"
    assert "x.py" in kw["text"] or "file(s) changed" in kw["text"]
    assert kw["model"] == "deterministic-fallback"


def test_dispatch_view_full_review_opens_the_checks_tab_not_the_pr_itself(monkeypatch):
    """The walkthrough is a comment on the PR, so a button linking the PR
    page reloaded the page it sits on; the full review is Elder's check run
    output on the PR's Checks tab."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    posted = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([
                {"filename": "x.py", "additions": 3, "deletions": 1},
            ])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    def fake_post(url, **kwargs):
        posted["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert '<a href="https://github.com/o/r/pull/7/checks">' in posted["body"]
    assert '<a href="https://github.com/o/r/pull/7">' not in posted["body"]


def test_dispatch_posts_nothing_when_the_pr_closed_before_publish(monkeypatch):
    """A lane pull request got its walkthrough 6.5 minutes after the merge
    (2026-10-18), one more email with nothing left to review."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 3, "deletions": 1}])
        if "/comments" in url:
            return _empty_comments_response()
        if kwargs["headers"]["Accept"] == "application/vnd.github+json":
            r = MagicMock(spec=httpx.Response)
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value={"state": "closed", "merged": True})
            return r
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         patch("httpx.patch") as mock_patch, \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "skipped_closed"}
    mock_patch.assert_not_called()
    # No walkthrough is published, and the status resolves to a real
    # message instead of sitting on 'still working' (#695).
    bodies = [c.kwargs["json"]["body"] for c in mock_post.call_args_list]
    assert bodies and all("## Walkthrough" not in b for b in bodies)
    assert "put the map down" in bodies[-1]


def test_dispatch_still_posts_when_the_state_read_fails(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    posted = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 3, "deletions": 1}])
        if "/comments" in url:
            return _empty_comments_response()
        if kwargs["headers"]["Accept"] == "application/vnd.github+json":
            raise httpx.ConnectError("down")
        return _diff_response()

    def fake_post(url, **kwargs):
        posted["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "degraded"}
    assert MARKER in posted["body"]


def test_dispatch_patches_existing_marker_comment_in_place(monkeypatch):
    """Acceptance criterion: updated IN PLACE on a re-dispatch (synchronize)."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    patched = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "y.py", "additions": 1, "deletions": 0}])
        if "/comments" in url:
            return _marker_comment_response(999)
        return _diff_response()

    def fake_patch(url, **kwargs):
        patched["url"] = url
        patched["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.patch", side_effect=fake_patch), \
         patch("httpx.post") as mock_post, \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        wt_dispatch.dispatch_walkthrough_review(_payload(head_sha="b" * 40), blocking=False)

    assert patched["url"].endswith("/issues/comments/999")
    assert MARKER in patched["body"]
    mock_post.assert_not_called()


def test_dispatch_uses_llm_summary_and_per_file_blurbs(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    from llm_client import WalkthroughSummary

    posted = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    def fake_post(url, **kwargs):
        posted["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    llm_summary = WalkthroughSummary(
        summary="Adds a null guard to x.py.",
        file_summaries={"x.py": "added guard clause"},
        effort="quick",
    )
    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=llm_summary):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "pass"}
    assert "Adds a null guard to x.py." in posted["body"]
    assert "added guard clause" in posted["body"]
    assert "quick" in posted["body"]


def test_dispatch_fetch_failure_returns_failed_without_posting(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    with patch("httpx.get", side_effect=httpx.ConnectError("dns down")), \
         patch("httpx.post") as mock_post, \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv:
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    mock_post.assert_not_called()
    mock_rcv.assert_not_called()


def test_dispatch_publish_failure_does_not_record_activity(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=httpx.ConnectError("dns down")), \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "publish_failed"}
    mock_rcv.assert_not_called()


def test_find_marker_comment_logs_when_scan_cap_exhausted(caplog):
    """#554 audit stage 2: giving up at the 20-page cap without finding
    the marker must be distinguishable from the ordinary 'no marker'
    exit - silent here means an unbounded duplicate-comment bug on an
    extreme PR (>2000 comments) would go unnoticed forever."""

    def full_comments_page(url, **kwargs):
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value=[{"id": i, "body": "unrelated"} for i in range(100)])
        return r

    with patch("httpx.get", side_effect=full_comments_page), \
         caplog.at_level("WARNING", logger="grug.persona.walkthrough"):
        result = wt_dispatch._find_marker_comment("tok", "o", "r", 1)

    assert result is None
    assert "walkthrough_marker_scan_capped" in caplog.text


def test_dispatch_diff_fetch_failure_is_phase_tagged(monkeypatch, caplog):
    """#554 audit stage 2: a diff-fetch failure and a files-fetch failure
    must be distinguishable in logs (not both a bare 'HTTPStatusError')."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        raise httpx.ConnectError("dns down")

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         caplog.at_level("WARNING", logger="grug.persona.walkthrough"):
        wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    mock_post.assert_not_called()
    assert any(
        getattr(r, "phase", None) == "diff" for r in caplog.records
    ), "expected a phase='diff' fetch-failure log line"


def test_dispatch_files_fetch_failure_is_phase_tagged(monkeypatch, caplog):
    """The diff fetch succeeds, then the files fetch fails - must log
    phase='files', not the same undifferentiated message as a diff outage."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            raise httpx.ConnectError("dns down")
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         caplog.at_level("WARNING", logger="grug.persona.walkthrough"):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    bodies = [c.kwargs["json"]["body"] for c in mock_post.call_args_list]
    assert bodies and all("## Walkthrough" not in b for b in bodies)
    assert any(
        getattr(r, "phase", None) == "files" for r in caplog.records
    ), "expected a phase='files' fetch-failure log line"


def test_dispatch_degraded_summary_notes_it_and_emits_gauge(monkeypatch):
    """#554 audit stage 2: the Activity-feed summary text must disclose a
    degraded LLM call (conclusion stays 'success' - the comment DID post -
    but the summary + a best-effort gauge carry the honest signal)."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    gauges = []
    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", return_value=MagicMock(raise_for_status=MagicMock())), \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=None), \
         patch("observability.emit_gauge", side_effect=lambda name, val: gauges.append((name, val)), create=True):
        wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    summary = mock_rcv.call_args.kwargs["summary"]
    assert "degraded to fallback" in summary
    assert mock_rcv.call_args.kwargs["conclusion"] == "success"
    assert gauges == [("grug.teller.summary_degraded", 1)]


def test_emit_degraded_metric_failure_is_logged_not_swallowed_silently(caplog):
    """FLINT: a bare except-pass on the gauge emit would hide a real
    import/signature bug in observability.emit_gauge indefinitely."""
    with patch("observability.emit_gauge", side_effect=RuntimeError("boom"), create=True), \
         caplog.at_level("DEBUG", logger="grug.persona.walkthrough"):
        wt_dispatch._emit_degraded_metric(True)

    assert "walkthrough_emit_degraded_metric_failed" in caplog.text


def test_dispatch_truncated_and_degraded_hedges_the_check_run_summary(monkeypatch):
    """FLINT: record_check_verdict's summary must hedge 'at least N'
    the same way the posted comment does - the Checks tab shouldn't show
    a capped count as exact when the comment body already says otherwise."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([
                {"filename": f"f{i}.py", "additions": 1, "deletions": 0}
                for i in range(100)
            ])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", return_value=MagicMock(raise_for_status=MagicMock())), \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=None):
        wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert "at least 500" in mock_rcv.call_args.kwargs["summary"]


def test_dispatch_token_exchange_runtime_error_degrades_not_raises(monkeypatch):
    """#554 audit stage 6: get_install_token can raise a bare RuntimeError
    on a malformed token-exchange response (github_app_auth), which the
    original except (HTTPStatusError, RequestError) tuple did NOT catch -
    Smasher's sibling dispatch already guards this exact case. Must
    degrade to fetch_failed, never escape as a wire-level exception."""
    def raising_retry(inst_id, fn):
        raise RuntimeError("GitHub token exchange returned no token")

    monkeypatch.setattr(wt_dispatch, "with_install_token_retry", raising_retry)
    with patch("httpx.post") as mock_post, \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv:
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    mock_post.assert_not_called()
    mock_rcv.assert_not_called()


def test_upsert_marker_lookup_failure_during_publish_degrades_to_publish_failed(monkeypatch):
    """#554 audit stage 7: diff+files succeed; the comments-list GET INSIDE
    _find_marker_comment fails during the upsert phase (distinct from the
    initial diff/files fetch phase already tested). Currently caught
    correctly by the outer try around with_install_token_retry(upsert) -
    this pins that so a future refactor that adds local handling inside
    _find_marker_comment (treating a transient error as 'no marker') can't
    silently start double-posting duplicate advisory comments."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        if "/comments" in url:
            raise httpx.ConnectError("dns down")
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         patch("httpx.patch") as mock_patch, \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "publish_failed"}
    mock_post.assert_not_called()
    mock_patch.assert_not_called()
    mock_rcv.assert_not_called()


def test_fetch_pr_files_signals_truncation_at_the_page_cap():
    """#554 audit stage 8: GitHub's own /files cap (3000) is far above our
    _MAX_FILE_PAGES bound (500) - a monorepo migration or generated-file
    dump can legitimately exceed it. The fetch must SIGNAL truncation, not
    silently return a partial list that looks complete."""
    def full_page(url, **kwargs):
        return _files_response([
            {"filename": f"f{i}.py", "additions": 1, "deletions": 0}
            for i in range(100)
        ])

    with patch("httpx.get", side_effect=full_page):
        files, truncated = wt_dispatch._fetch_pr_files("tok", "o", "r", 1)

    assert len(files) == 500  # _MAX_FILE_PAGES x 100
    assert truncated is True


def test_fetch_pr_files_not_truncated_under_the_cap():
    def small_page(url, **kwargs):
        return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])

    with patch("httpx.get", side_effect=small_page):
        files, truncated = wt_dispatch._fetch_pr_files("tok", "o", "r", 1)

    assert len(files) == 1
    assert truncated is False


def test_dispatch_truncated_files_logs_warning_and_hedges_the_comment(monkeypatch, caplog):
    """The truncation signal must reach BOTH the log (operator visibility)
    and the posted comment (author visibility) - never silently present a
    partial count as exact in either surface."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    posted = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([
                {"filename": f"f{i}.py", "additions": 1, "deletions": 0}
                for i in range(100)
            ])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    def fake_post(url, **kwargs):
        posted["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None), \
         caplog.at_level("WARNING", logger="grug.persona.walkthrough"):
        wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert "walkthrough_file_fetch_capped" in caplog.text
    assert "at least 500" in posted["body"]
    assert "File list is partial" in posted["body"]


def test_dispatch_diff_fetch_failure_enqueues_self_recovery(monkeypatch):
    """#554 peer review (codex/poolside/spark, CONFIRMED 3x): _run_job
    claims the head-SHA idempotency key BEFORE calling dispatch. Teller
    never publishes anything on fetch_failed, so without a durable retry
    the SHA would be permanently suppressed - one silent skip forever.
    Must enqueue exactly one rerun via the SAME lane Elder/Guard/Smasher
    use for an unhandled dispatch error."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    with patch("httpx.get", side_effect=httpx.ConnectError("dns down")), \
         patch("rerun.enqueue_rerun") as mock_rerun:
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    mock_rerun.assert_called_once_with(
        install_id=1, repo="o/r", pr_number=7, persona="walkthrough",
    )


def test_dispatch_files_fetch_failure_enqueues_self_recovery(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            raise httpx.ConnectError("dns down")
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("rerun.enqueue_rerun") as mock_rerun:
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    mock_rerun.assert_called_once_with(
        install_id=1, repo="o/r", pr_number=7, persona="walkthrough",
    )


def test_dispatch_publish_failure_enqueues_self_recovery(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=httpx.ConnectError("dns down")), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None), \
         patch("rerun.enqueue_rerun") as mock_rerun:
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "publish_failed"}
    mock_rerun.assert_called_once_with(
        install_id=1, repo="o/r", pr_number=7, persona="walkthrough",
    )


def test_self_recover_failure_is_swallowed_never_escalates(monkeypatch, caplog):
    """The self-recovery enqueue is itself best-effort - if the rerun
    queue isn't configured (RuntimeError) or the import fails, that must
    log and never turn a degrade path into an unhandled exception."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )

    def raising_rerun(**kwargs):
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")

    with patch("httpx.get", side_effect=httpx.ConnectError("dns down")), \
         patch("rerun.enqueue_rerun", side_effect=raising_rerun), \
         caplog.at_level("WARNING", logger="grug.persona.walkthrough"):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    assert "walkthrough_self_recover_failed" in caplog.text


def test_find_marker_comment_ignores_user_authored_decoy(monkeypatch):
    """#554 peer review (codex): a PR commenter who posts the literal
    marker string BEFORE Teller's first run must not be mistaken for
    Teller's own comment - only `performed_via_github_app` (set
    server-side, unforgeable by a human commenter) counts."""
    def fake_get(url, **kwargs):
        return _files_response([
            {
                "id": 111, "body": MARKER,
                "performed_via_github_app": None,  # human-authored decoy
            },
            {
                "id": 222, "body": MARKER,
                "performed_via_github_app": {"id": 1, "slug": "grug"},
            },
        ])

    with patch("httpx.get", side_effect=fake_get):
        result = wt_dispatch._find_marker_comment("tok", "o", "r", 1)

    assert result["id"] == 222


def test_dispatch_ignores_decoy_marker_and_posts_fresh_comment(monkeypatch):
    """End-to-end: a human-authored decoy marker comment exists, but no
    genuine app-authored one - Teller must POST a fresh comment, not
    attempt (and fail) a PATCH against the decoy it cannot edit."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    posted = {}

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        if "/comments" in url:
            r = MagicMock(spec=httpx.Response)
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value=[{
                "id": 111, "body": MARKER, "performed_via_github_app": None,
            }])
            return r
        return _diff_response()

    def fake_post(url, **kwargs):
        posted["body"] = kwargs["json"]["body"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("httpx.patch") as mock_patch, \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "degraded"}
    mock_patch.assert_not_called()
    assert MARKER in posted["body"]


def test_find_marker_comment_ignores_different_app_decoy(monkeypatch):
    """#554 peer review round 3 (codex): a bare non-null performed_via_
    github_app check only proves SOME GitHub App posted the comment, not
    that WE did - a decoy from a DIFFERENT installed app (own numeric ID
    mismatched) must still be ignored, not just a human-authored one."""
    def fake_get(url, **kwargs):
        return _files_response([
            {
                "id": 111, "body": MARKER,
                "performed_via_github_app": {"id": 999, "slug": "some-other-app"},
            },
            {
                "id": 222, "body": MARKER,
                "performed_via_github_app": {"id": int(_OWN_APP_ID), "slug": "grug"},
            },
        ])

    with patch("httpx.get", side_effect=fake_get):
        result = wt_dispatch._find_marker_comment("tok", "o", "r", 1)

    assert result["id"] == 222


# --- #695: bot-comment lifecycle (placeholder, update in place, history) ----


class _FakeGitHub:
    """An in-memory PR comment thread behind the httpx seam, so a test can
    follow ONE comment from placeholder to final content."""

    def __init__(self, comments=None, fail_diff=False):
        self.comments = comments if comments is not None else []
        self.fail_diff = fail_diff
        self.calls: list[tuple[str, int]] = []  # (verb, comment id)

    def _response(self, payload):
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value=payload)
        return r

    def get(self, url, **kwargs):
        if url.endswith("/files"):
            return self._response(
                [{"filename": "new.py", "additions": 2, "deletions": 0}]
            )
        if "/comments" in url:
            return self._response(self.comments)
        if self.fail_diff:
            raise httpx.ConnectError("down")
        if kwargs["headers"]["Accept"] == "application/vnd.github+json":
            return self._response({"state": "open"})
        return _diff_response()

    def post(self, url, **kwargs):
        comment = {
            "id": 42, "body": kwargs["json"]["body"],
            "performed_via_github_app": {"id": 1},
        }
        self.comments.append(comment)
        self.calls.append(("POST", 42))
        return self._response(comment)

    def patch(self, url, **kwargs):
        cid = int(url.rsplit("/", 1)[1])
        for c in self.comments:
            if c["id"] == cid:
                c["body"] = kwargs["json"]["body"]
        self.calls.append(("PATCH", cid))
        return self._response({})

    def run(self, summarize):
        with patch("httpx.get", side_effect=self.get), \
             patch("httpx.post", side_effect=self.post), \
             patch("httpx.patch", side_effect=self.patch), \
             patch("personas.walkthrough.dispatch.record_check_verdict"), \
             patch("llm_client.summarize_pr", side_effect=summarize):
            return wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)


@pytest.fixture
def _inline_token(monkeypatch):
    monkeypatch.setattr(wt_dispatch, "with_install_token_retry", lambda _iid, fn: fn("tok"))


def test_placeholder_posts_before_the_llm_call_and_the_same_comment_is_patched(_inline_token):
    gh = _FakeGitHub()
    seen_at_llm_call = []

    def summarize(*args, **kwargs):
        seen_at_llm_call.append([dict(c) for c in gh.comments])
        return None

    gh.run(summarize)

    # Before the LLM call: exactly one comment, already posted, saying "working".
    assert len(seen_at_llm_call[0]) == 1
    assert "drawing the map" in seen_at_llm_call[0][0]["body"]
    # After: that SAME comment id was PATCHed to the final content - no second POST.
    assert gh.calls == [("POST", 42), ("PATCH", 42)]
    assert len(gh.comments) == 1
    assert "## Walkthrough" in gh.comments[0]["body"]
    assert "drawing the map" not in gh.comments[0]["body"]


def test_a_rerun_keeps_the_last_walkthrough_collapsed_with_resolved_files(_inline_token):
    first = _FakeGitHub()
    first.run(lambda *a, **k: None)
    # The first run walked new.py; the next run walks a different file.
    gh = _FakeGitHub(comments=first.comments)

    def files_now(url, **kwargs):
        if url.endswith("/files"):
            return gh._response([{"filename": "other.py", "additions": 1, "deletions": 0}])
        return _FakeGitHub.get(gh, url, **kwargs)

    with patch("httpx.get", side_effect=files_now), \
         patch("httpx.post", side_effect=gh.post), \
         patch("httpx.patch", side_effect=gh.patch), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    body = gh.comments[0]["body"]
    assert gh.calls == [("PATCH", 42), ("PATCH", 42)]  # status, then final: one comment
    assert "<summary>Previous walkthrough" in body
    assert "`new.py` - resolved (no longer in the change)" in body
    assert "other.py" in body


def test_a_failed_fetch_resolves_the_placeholder_to_a_real_message(_inline_token):
    gh = _FakeGitHub(fail_diff=True)

    with patch("httpx.get", side_effect=gh.get), \
         patch("httpx.post", side_effect=gh.post), \
         patch("httpx.patch", side_effect=gh.patch), \
         patch("personas.walkthrough.dispatch._self_recover"):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    assert gh.calls == [("POST", 42), ("PATCH", 42)]
    body = gh.comments[0]["body"]
    assert "could not finish the walkthrough" in body
    assert "drawing the map" not in body


def test_an_unexpected_error_resolves_the_placeholder_and_still_raises(_inline_token):
    gh = _FakeGitHub()

    with patch("httpx.get", side_effect=gh.get), \
         patch("httpx.post", side_effect=gh.post), \
         patch("httpx.patch", side_effect=gh.patch), \
         patch("llm_client.summarize_pr", return_value=None), \
         patch("personas.walkthrough.mermaid.build_diagram", side_effect=ValueError("boom")):
        with pytest.raises(ValueError):
            wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert "something broke on Grug's side" in gh.comments[0]["body"]
    assert "drawing the map" not in gh.comments[0]["body"]


def test_a_failed_publish_resolves_the_placeholder_to_a_real_message(_inline_token):
    gh = _FakeGitHub()
    real_patch = gh.patch

    def patch_final_fails(url, **kwargs):
        if "## Walkthrough" in kwargs["json"]["body"]:
            raise httpx.ConnectError("down")
        return real_patch(url, **kwargs)

    with patch("httpx.get", side_effect=gh.get), \
         patch("httpx.post", side_effect=gh.post), \
         patch("httpx.patch", side_effect=patch_final_fails), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None), \
         patch("personas.walkthrough.dispatch._self_recover"):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "publish_failed"}
    body = gh.comments[0]["body"]
    assert "could not write the final map" in body
    assert "drawing the map" not in body
