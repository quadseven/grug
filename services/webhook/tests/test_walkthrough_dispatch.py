"""Teller dispatch tests (#554): the walkthrough is written to the board's
Walkthrough section and the Teller check-run summary, never to a comment of
its own. Fetch + LLM-summary fallback + check-run summary. Network mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from personas.walkthrough import dispatch as wt_dispatch
from personas.walkthrough.render import FileStat


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


@pytest.fixture(autouse=True)
def _stub_token_retry(monkeypatch):
    monkeypatch.setattr(
        wt_dispatch, "with_install_token_retry", lambda inst_id, fn: fn("tok")
    )


@pytest.fixture(autouse=True)
def _noop_degraded_metric(monkeypatch):
    monkeypatch.setattr(wt_dispatch, "_emit_degraded_metric", lambda degraded: None)


# --- acceptance: no comment, board text, check-run summary ----------------


def test_dispatch_posts_no_comment_writes_walkthrough_to_board(monkeypatch):
    """Acceptance: a walkthrough-enabled PR produces no new comment carrying
    the grug-teller:walkthrough marker, and the board's Walkthrough section
    holds the walkthrough text."""
    recorded = []

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 3, "deletions": 1}])
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         patch("httpx.patch") as mock_patch, \
         patch.object(
             wt_dispatch.hunt_recorder,
             "record_walkthrough",
             side_effect=lambda ref, **kw: recorded.append(kw),
         ), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "degraded"}
    # No advisory comment is posted, created or edited.
    mock_post.assert_not_called()
    mock_patch.assert_not_called()
    [(kw)] = recorded
    # The board's Walkthrough section holds the walkthrough text.
    assert "Review effort:" in kw["text"]
    assert "x.py" in kw["text"]  # the changed-files table
    assert "teller-voice was quiet" not in kw["text"]


def test_dispatch_check_run_summary_carries_effort_and_table(monkeypatch):
    """Acceptance: the Teller check-run summary carries the effort line and
    the changed-files table, and omits a change diagram whose graph has no
    edges."""
    from llm_client import WalkthroughSummary

    recorded = []
    llm_summary = WalkthroughSummary(
        summary="Adds a null guard to x.py.",
        file_summaries={"x.py": "added guard clause"},
        effort="quick",
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         patch("httpx.patch") as mock_patch, \
         patch.object(
             wt_dispatch.hunt_recorder,
             "record_walkthrough",
             side_effect=lambda ref, **kw: recorded.append(kw),
         ), \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=llm_summary):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "pass"}
    mock_post.assert_not_called()
    mock_patch.assert_not_called()
    summary = mock_rcv.call_args.kwargs["summary"]
    assert "Review effort: quick (~5 min)" in summary  # effort line
    assert "x.py" in summary  # changed-files table
    assert "added guard clause" in summary
    assert "```mermaid" not in summary  # no diagram (no edges)
    assert "graph TD" not in summary


def test_dispatch_fallback_omits_teller_voice_sentence(monkeypatch):
    """Acceptance: when the walkthrough falls back to the deterministic
    summary, neither the board nor the check run contains the
    "teller-voice was quiet" sentence."""
    recorded = []

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         patch("httpx.patch") as mock_patch, \
         patch.object(
             wt_dispatch.hunt_recorder,
             "record_walkthrough",
             side_effect=lambda ref, **kw: recorded.append(kw),
         ), \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv, \
         patch("llm_client.summarize_pr", return_value=None):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "degraded"}
    mock_post.assert_not_called()
    mock_patch.assert_not_called()
    [(kw)] = recorded
    assert "teller-voice was quiet" not in kw["text"]
    summary = mock_rcv.call_args.kwargs["summary"]
    assert "teller-voice was quiet" not in summary
    # The fallback renders the file table alone.
    assert "x.py" in summary
    assert "Review effort:" in summary


# --- LLM summary path renders paragraph into the board -------------------


def test_dispatch_board_text_uses_llm_summary(monkeypatch):
    from llm_client import WalkthroughSummary

    recorded = []
    llm_summary = WalkthroughSummary(
        summary="Adds a null guard to x.py.",
        file_summaries={"x.py": "added guard clause"},
        effort="moderate",
    )

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         patch("httpx.patch") as mock_patch, \
         patch.object(
             wt_dispatch.hunt_recorder,
             "record_walkthrough",
             side_effect=lambda ref, **kw: recorded.append(kw),
         ), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=llm_summary):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "pass"}
    mock_post.assert_not_called()
    mock_patch.assert_not_called()
    [(kw)] = recorded
    assert "Adds a null guard to x.py." in kw["text"]
    assert "added guard clause" in kw["text"]
    assert "Review effort: moderate (~15 min)" in kw["text"]
    assert "x.py" in kw["text"]


# --- fetch / closed-PR behavior unchanged ---------------------------------


def test_dispatch_fetch_failure_returns_failed_without_posting(monkeypatch):
    with patch("httpx.get", side_effect=httpx.ConnectError("dns down")), \
         patch("httpx.post") as mock_post, \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv:
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "fetch_failed"}
    mock_post.assert_not_called()
    mock_rcv.assert_not_called()


def test_dispatch_skips_closed_pr(monkeypatch):
    """A walkthrough that lands after the merge is not published."""

    def fake_get(url, **kwargs):
        if kwargs.get("headers", {}).get("Accept") == "application/vnd.github.diff":
            return _diff_response()
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value={"state": "closed", "merged": True})
        return r

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         patch("httpx.patch") as mock_patch, \
         patch.object(wt_dispatch.hunt_recorder, "record_walkthrough") as mock_rw, \
         patch("personas.walkthrough.dispatch.record_check_verdict") as mock_rcv:
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)

    assert out == {"persona": "walkthrough", "result": "skipped_closed"}
    mock_post.assert_not_called()
    mock_patch.assert_not_called()
    mock_rw.assert_not_called()
    mock_rcv.assert_not_called()


def test_dispatch_records_walkthrough_ref_and_model(monkeypatch):
    recorded = []

    def fake_get(url, **kwargs):
        if url.endswith("/files"):
            return _files_response([{"filename": "x.py", "additions": 1, "deletions": 0}])
        return _diff_response()

    with patch("httpx.get", side_effect=fake_get), \
         patch("httpx.post") as mock_post, \
         patch("httpx.patch") as mock_patch, \
         patch.object(
             wt_dispatch.hunt_recorder,
             "record_walkthrough",
             side_effect=lambda ref, **kw: recorded.append((ref, kw)),
         ), \
         patch("personas.walkthrough.dispatch.record_check_verdict"), \
         patch("llm_client.summarize_pr", return_value=None):
        wt_dispatch.dispatch_walkthrough_review(_payload(head_sha="b" * 40), blocking=False)

    [(ref, kw)] = recorded
    assert (ref.install_id, ref.owner, ref.repo, ref.pr_number) == (1, "o", "r", 7)
    assert kw["head_sha"] == "b" * 40 and kw["trigger"] == "opened"
    assert kw["model"] == "deterministic-fallback"
