"""Teller dispatch tests (#554): fetch (incl. phase-tagged failure logging),
LLM-summary fallback, upsert-by-marker (PATCH-else-POST, incl. scan-cap-
exhaustion warning), and the honest degrade-comment path (incl. the
summary_degraded gauge). Network mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from personas.walkthrough import dispatch as wt_dispatch
from personas.walkthrough.render import MARKER


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
    r.json = MagicMock(return_value=[{"id": comment_id, "body": MARKER}])
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
    assert "sprawl wide" in posted["body"]
