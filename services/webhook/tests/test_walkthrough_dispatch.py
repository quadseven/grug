"""Teller dispatch tests (#554): fetch (incl. phase-tagged failure logging),
LLM-summary fallback, upsert-by-marker (PATCH-else-POST, incl. scan-cap-
exhaustion warning), and the honest degrade-comment path (incl. the
summary_degraded gauge). Network mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from personas.walkthrough import dispatch as wt_dispatch

_REAL_START_CHECK_RUN = wt_dispatch._start_check_run


@pytest.fixture(autouse=True)
def _no_in_progress_run(monkeypatch):
    """The content tests below see only the final check run: the #695
    in-progress run is off here and covered by the lifecycle tests at the
    end of this file."""
    monkeypatch.setattr(wt_dispatch, "_start_check_run", lambda *a: None)


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
        "id": comment_id, "body": "<!-- grug-teller:walkthrough -->",
        "performed_via_github_app": {"id": 1, "slug": "grug"},
    }])
    return r


def test_dispatch_posts_no_new_comment_when_no_marker_exists(monkeypatch):
    """T1432: with no Teller comment on the PR, the one POST is the Teller
    check run, never a new comment."""
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
        posted["url"] = url
        posted["json"] = kwargs["json"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "degraded"}
    assert posted["url"].endswith("/check-runs")
    assert posted["json"]["name"] == "Grug - Teller"
    assert "x.py" in posted["json"]["output"]["summary"]
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
    mock_post.assert_not_called()
    mock_patch.assert_not_called()


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
        posted["json"] = kwargs["json"]
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "degraded"}
    assert posted["json"]["name"] == "Grug - Teller"


def test_dispatch_leaves_an_existing_teller_comment_untouched(monkeypatch):
    """T1432: a comment an older Teller left is neither edited (no edit storm)
    nor deleted, and no new one is created."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "y.py", "additions": 1, "deletions": 0}])
        if "/comments" in url:
            return _marker_comment_response(999)
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.patch") as mock_patch, \
         patch("httpx.delete") as mock_delete, \
         patch("httpx.post", return_value=MagicMock(raise_for_status=MagicMock())) as mock_post, \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        wt_dispatch.dispatch_walkthrough_review(_payload(head_sha="b" * 40), blocking=False)

    mock_patch.assert_not_called()
    mock_delete.assert_not_called()
    assert all(c.args[0].endswith("/check-runs") for c in mock_post.call_args_list)


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
        posted["json"] = kwargs["json"]
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
    summary = posted["json"]["output"]["summary"]
    assert "Adds a null guard to x.py." in summary
    assert "added guard clause" in summary
    assert "quick" in summary


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
    mock_post.assert_not_called()
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
    the same way the posted summary does - the Checks tab shouldn't show
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


def test_dispatch_truncated_files_logs_warning_and_hedges_the_check_run(monkeypatch, caplog):
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
        posted["json"] = kwargs["json"]
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
    summary = posted["json"]["output"]["summary"]
    assert "File list is partial" in summary
    assert "first 500" in summary


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


def _run_capturing(monkeypatch, *, llm_summary):
    """One dispatch with every write captured: -> (comment POSTs, check-run
    JSON bodies, review-session recordings, dispatch result)."""
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("tok"),
    )
    comments: list[dict] = []
    checks: list[dict] = []
    recorded: list[dict] = []

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 3, "deletions": 1}])
        if "/comments" in url:
            return _empty_comments_response()
        return _diff_response()

    def fake_post(url, **kwargs):
        (checks if url.endswith("/check-runs") else comments).append(kwargs["json"])
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value={})
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post", side_effect=fake_post), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch.object(
             wt_dispatch.hunt_recorder, "record_walkthrough",
             side_effect=lambda ref, **kw: recorded.append(kw),
         ), \
         patch("llm_client.summarize_pr", return_value=llm_summary):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)
    return comments, checks, recorded, out


def _llm_summary():
    from llm_client import WalkthroughSummary

    return WalkthroughSummary(
        summary="Adds a null guard to x.py.",
        file_summaries={"x.py": "added guard clause"},
        effort="quick",
    )


def test_walkthrough_posts_no_comment_and_the_board_holds_its_text(monkeypatch):
    comments, checks, recorded, out = _run_capturing(
        monkeypatch, llm_summary=_llm_summary(),
    )

    assert out == {"persona": "walkthrough", "result": "pass"}
    assert comments == []
    [kw] = recorded
    assert "Adds a null guard to x.py." in kw["text"]
    assert "grug-teller:walkthrough" not in kw["text"]
    [check] = checks
    assert check["name"] == "Grug - Teller"
    assert check["conclusion"] == "success"


def test_teller_check_run_summary_carries_effort_and_files_without_an_edgeless_diagram(
    monkeypatch,
):
    _, checks, recorded, _ = _run_capturing(monkeypatch, llm_summary=_llm_summary())

    summary = checks[0]["output"]["summary"]
    assert "Review effort" in summary
    assert "- `x.py` (+3/-1) - added guard clause" in summary
    assert "mermaid" not in summary
    assert "Shape of the change" not in summary
    assert "Review effort" in recorded[0]["text"]


def test_deterministic_fallback_renders_no_quiet_sentence_on_board_or_check(monkeypatch):
    comments, checks, recorded, out = _run_capturing(monkeypatch, llm_summary=None)

    assert out == {"persona": "walkthrough", "result": "degraded"}
    assert comments == []
    for text in (checks[0]["output"]["summary"], recorded[0]["text"]):
        assert "teller-voice was quiet" not in text
        assert "x.py" in text  # the file table alone survives


def test_related_hunts_go_to_the_check_run_and_not_twice_on_the_board(monkeypatch):
    """#675: the check run lists related hunts in the walkthrough; the board
    renders its own Related context list, so its walkthrough text leaves
    them out."""
    from personas.walkthrough.related import RelatedPR

    related = [RelatedPR(number=101, title="Fix the thing", reason="touched x.py")]
    monkeypatch.setattr(
        "personas.walkthrough.related.find_related_prs",
        lambda *a, **kw: related,
    )
    _, checks, recorded, _ = _run_capturing(monkeypatch, llm_summary=_llm_summary())

    assert "Possibly related hunts" in checks[0]["output"]["summary"]
    assert "#101 Fix the thing" in checks[0]["output"]["summary"]
    assert "Possibly related hunts" not in recorded[0]["text"]
    assert recorded[0]["related"] == ("#101 Fix the thing (touched x.py)",)


# --- #695: the Teller run goes up in_progress first, then resolves in place --


class _FakeChecks:
    """GitHub behind the httpx seam, recording every check-run write so a
    test can follow ONE run from in_progress to its final state."""

    def __init__(self, *, fail_diff=False, closed=False, fail_final=False):
        self.fail_diff = fail_diff
        self.closed = closed
        self.fail_final = fail_final
        self.writes: list[tuple] = []  # (verb, run id, json body)
        self.comment_posts = 0

    @staticmethod
    def _response(payload):
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value=payload)
        return r

    def get(self, url, **kwargs):
        if url.endswith("/files"):
            return self._response([{"filename": "x.py", "additions": 3, "deletions": 1}])
        if kwargs["headers"]["Accept"] == "application/vnd.github+json":
            return self._response({"state": "closed" if self.closed else "open"})
        if self.fail_diff:
            raise httpx.ConnectError("down")
        return _diff_response()

    def post(self, url, **kwargs):
        if not url.endswith("/check-runs"):
            self.comment_posts += 1
            return self._response({})
        self.writes.append(("POST", None, kwargs["json"]))
        return self._response({"id": 42})

    def patch(self, url, **kwargs):
        body = kwargs["json"]
        if self.fail_final and body.get("conclusion") == "success":
            raise httpx.ConnectError("down")
        self.writes.append(("PATCH", int(url.rsplit("/", 1)[1]), body))
        return self._response({"id": 42})

    def run(self, summarize=None):
        with patch("httpx.get", side_effect=self.get), \
             patch("httpx.post", side_effect=self.post), \
             patch("httpx.patch", side_effect=self.patch), \
             patch("personas.code_reviewer.dispatch.pr_skip_rule", return_value=None), \
             patch("personas.walkthrough.dispatch.record_check_verdict"), \
             patch.object(wt_dispatch.hunt_recorder, "record_walkthrough"), \
             patch.object(wt_dispatch, "_self_recover"), \
             patch("llm_client.summarize_pr", side_effect=summarize or (lambda *a, **k: None)):
            return wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)


@pytest.fixture
def _lifecycle(monkeypatch):
    monkeypatch.setattr(wt_dispatch, "_start_check_run", _REAL_START_CHECK_RUN)
    monkeypatch.setattr(wt_dispatch, "with_install_token_retry", lambda _iid, fn: fn("tok"))


def test_in_progress_run_posts_before_the_llm_call_and_the_same_run_is_patched(_lifecycle):
    gh = _FakeChecks()
    seen_at_llm_call = []

    def summarize(*args, **kwargs):
        seen_at_llm_call.append(list(gh.writes))
        return None

    gh.run(summarize)

    # Before the LLM call: one run, already up, saying "working".
    [(verb, _, start)] = seen_at_llm_call[0]
    assert verb == "POST"
    assert start["status"] == "in_progress" and "conclusion" not in start
    assert "drawing the map" in start["output"]["summary"]
    # After: that SAME run id was PATCHed to the walkthrough - no second run.
    assert [(v, rid) for v, rid, _ in gh.writes] == [("POST", None), ("PATCH", 42)]
    final = gh.writes[1][2]
    assert final["status"] == "completed" and final["conclusion"] == "success"
    assert "x.py" in final["output"]["summary"]
    assert gh.comment_posts == 0


def test_a_failed_fetch_resolves_the_run_to_a_real_message(_lifecycle):
    gh = _FakeChecks(fail_diff=True)

    out = gh.run()

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    assert [(v, rid) for v, rid, _ in gh.writes] == [("POST", None), ("PATCH", 42)]
    done = gh.writes[1][2]
    assert done["status"] == "completed" and done["conclusion"] == "neutral"
    assert "could not read the PR from GitHub" in done["output"]["summary"]


def test_a_failed_publish_resolves_the_run_to_a_real_message(_lifecycle):
    gh = _FakeChecks(fail_final=True)

    out = gh.run()

    assert out == {"persona": "walkthrough", "result": "publish_failed"}
    done = gh.writes[-1][2]
    assert gh.writes[-1][1] == 42
    assert done["conclusion"] == "neutral"
    assert "could not write the final map" in done["output"]["summary"]


def test_a_closed_pr_resolves_the_run_as_skipped(_lifecycle):
    gh = _FakeChecks(closed=True)

    out = gh.run()

    assert out == {"persona": "walkthrough", "result": "skipped_closed"}
    done = gh.writes[-1][2]
    assert done["conclusion"] == "skipped"
    assert "closed" in done["output"]["summary"]


def test_an_unexpected_error_resolves_the_run_and_still_raises(_lifecycle, caplog):
    gh = _FakeChecks()

    with patch.object(wt_dispatch, "estimate_effort", side_effect=ValueError("boom")), \
         caplog.at_level("WARNING", logger="grug.persona.walkthrough"):
        with pytest.raises(ValueError):
            gh.run()

    done = gh.writes[-1][2]
    assert done["conclusion"] == "neutral"
    assert "something broke on Grug's side" in done["output"]["summary"]
    assert "walkthrough_failed" in caplog.text


def test_a_refused_start_still_publishes_the_walkthrough(_lifecycle, caplog):
    """The in-progress run is a sign, not the work: when it does not land,
    the final walkthrough is POSTed as a fresh completed run."""
    gh = _FakeChecks()
    real_post = gh.post

    def refuse_in_progress(url, **kwargs):
        if kwargs["json"].get("status") == "in_progress":
            raise httpx.ConnectError("down")
        return real_post(url, **kwargs)

    gh.post = refuse_in_progress
    with caplog.at_level("WARNING", logger="grug.persona.walkthrough"):
        out = gh.run()

    assert out["result"] == "degraded"
    [(verb, _, final)] = gh.writes
    assert verb == "POST" and final["conclusion"] == "success"
    assert "walkthrough_status_failed" in caplog.text


def test_a_config_skipped_pr_gets_no_run_at_all(_lifecycle):
    gh = _FakeChecks()

    with patch("personas.code_reviewer.dispatch.pr_skip_rule", return_value="title:wip"), \
         patch("httpx.post", side_effect=gh.post):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out["result"] == "skipped_config"
    assert gh.writes == []
