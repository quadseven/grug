"""Tests for personas/code_reviewer/reactions.py.

The reaction engine reads 👍/👎 reactions on Grug inline comments and
submits a `human_verdict` DD annotation (ground-truth for calibrating
the judge). Best-effort + deduped: only submits when the classification
changes from the last-recorded verdict. Mocks GH API + DD seam + the
persistence store; no real network or DDB."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from personas.code_reviewer import reactions as cr_reactions


def _record(comment_id=1, last_verdict=None, span={"trace_id": "t", "span_id": "s"}):
    return {
        "comment_id": comment_id,
        "repo": "o/r",
        "pr_number": 7,
        "review_span_context": span,
        "finding_tags": {"rule_name": "null-deref", "file": "x.py", "line": "2"},
        "last_verdict": last_verdict,
    }


def _reactions_response(contents):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=[{"content": c} for c in contents])
    return r


# --- classification ---

def test_classify_thumbs_down_is_false_positive():
    assert cr_reactions._classify_reactions([{"content": "-1"}]) == "false_positive"


def test_classify_thumbs_up_is_confirmed():
    assert cr_reactions._classify_reactions([{"content": "+1"}]) == "confirmed"


def test_classify_thumbs_down_wins_when_both_present():
    """A developer flagging a false positive is the higher-value
    correction — 👎 takes precedence over 👍 when both are present."""
    out = cr_reactions._classify_reactions([{"content": "+1"}, {"content": "-1"}])
    assert out == "false_positive"


def test_classify_no_thumbs_returns_none():
    """Other reactions (heart, rocket, eyes) carry no verdict signal."""
    assert cr_reactions._classify_reactions([{"content": "heart"}, {"content": "rocket"}]) is None


def test_classify_empty_returns_none():
    assert cr_reactions._classify_reactions([]) is None


# --- poll_and_annotate orchestration ---

@pytest.fixture
def _patch_store(monkeypatch):
    updates: list[dict] = []
    monkeypatch.setattr(
        cr_reactions, "update_comment_record_reaction",
        lambda **kw: updates.append(kw),
    )
    return updates


def _patch_annotate(monkeypatch):
    submitted: list[dict] = []
    monkeypatch.setattr(
        cr_reactions, "submit_reaction_annotation",
        lambda **kw: submitted.append(kw),
    )
    return submitted


def test_poll_and_annotate_submits_on_new_thumbs_down(monkeypatch, _patch_store):
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=5)], install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 1
    assert submitted[0]["verdict"] == "false_positive"
    assert submitted[0]["review_span_context"] == {"trace_id": "t", "span_id": "s"}
    assert submitted[0]["tags"]["rule_name"] == "null-deref"
    # dedup baseline updated.
    assert _patch_store[0] == {"install_id": 1, "comment_id": 5, "verdict": "false_positive"}


def test_poll_and_annotate_dedups_unchanged_verdict(monkeypatch, _patch_store):
    """A comment already recorded as false_positive whose reaction is
    still 👎 must NOT re-submit — the dedup baseline matches."""
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=5, last_verdict="false_positive")],
            install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 0
    assert submitted == []
    assert _patch_store == []  # no baseline update either


def test_poll_and_annotate_resubmits_on_changed_verdict(monkeypatch, _patch_store):
    """Developer flips 👎 → 👍 (changed their mind): the new verdict
    differs from the baseline, so re-submit."""
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["+1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=5, last_verdict="false_positive")],
            install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 1
    assert submitted[0]["verdict"] == "confirmed"


def test_poll_and_annotate_skips_no_reaction(monkeypatch, _patch_store):
    """A comment with no 👍/👎 yet → no classification → no submit, no
    baseline update."""
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["heart"])):
        n = cr_reactions.poll_and_annotate(
            [_record()], install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 0
    assert submitted == []


def test_poll_and_annotate_continues_batch_on_one_failure(monkeypatch, _patch_store):
    """One comment's reaction poll failing (GH 5xx) must not abort the
    whole batch — best-effort per-record."""
    submitted = _patch_annotate(monkeypatch)

    def staged(url, *a, **kw):
        if "/comments/1/" in url:
            raise httpx.ReadTimeout("hung")
        return _reactions_response(["-1"])

    with patch("httpx.get", side_effect=staged):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=1), _record(comment_id=2)],
            install_id=1, fetch_token=lambda: "tok",
        )
    # comment 1 failed; comment 2 still submitted.
    assert n == 1
    assert submitted[0]["tags"]["rule_name"] == "null-deref"


def test_poll_and_annotate_skips_record_without_span_context(monkeypatch, _patch_store):
    """A persisted record missing its review span (degraded review at
    publish time) can't attach an annotation — skip, don't crash."""
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(span=None)], install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 0
    assert submitted == []


def test_poll_comment_reactions_uses_reactions_endpoint(monkeypatch):
    """Confirm the GH reactions REST path + preview Accept header."""
    captured = {}

    def capture(url, *, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return _reactions_response(["+1"])

    with patch("httpx.get", side_effect=capture):
        out = cr_reactions.poll_comment_reactions("tok", "o", "r", 99)
    assert captured["url"] == (
        "https://api.github.com/repos/o/r/pulls/comments/99/reactions"
    )
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert out == [{"content": "+1"}]
