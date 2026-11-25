"""Elder publishes the same elapsed seconds on the check and in logs."""

from __future__ import annotations

import logging
import time
from unittest.mock import Mock

import httpx
import pytest
from adapters import install_store
from llm_client import Backend, LlmReviewResponse
from personas import hunt_recorder
from personas.code_reviewer import dispatch, review_timing
from personas.code_reviewer.complexity import ComplexityScan

_HEAD = "a" * 40
_PAYLOAD = {
    "action": "opened",
    "installation": {"id": 1},
    "repository": {"id": 2, "name": "sample", "owner": {"login": "example"}},
    "pull_request": {"number": 7, "head": {"sha": _HEAD}},
}
_DIFF = """diff --git a/value.py b/value.py
--- a/value.py
+++ b/value.py
@@ -1 +1 @@
-value = 1
+value = 2
"""


@pytest.fixture
def review(monkeypatch, caplog):
    now = 100.0
    checks = {}

    def timed(seconds, result):
        def call(*args, **kwargs):
            nonlocal now
            now += seconds
            if isinstance(result, Exception):
                raise result
            return result

        return call

    def post(url, *, json, **kwargs):
        nonlocal now
        if url.endswith("/check-runs"):
            now += 5
            check_id = len(checks) + 1
            checks[check_id] = json["output"]["summary"]
            return httpx.Response(201, json={"id": check_id}, request=httpx.Request("POST", url))
        assert url.endswith("/reviews")
        now += 7
        return httpx.Response(201, json={"id": 3}, request=httpx.Request("POST", url))

    def patch(url, *, json, **kwargs):
        check_id = int(url.rsplit("/", 1)[1])
        assert check_id in checks
        assert json.keys() == {"output"}
        checks[check_id] = json["output"]["summary"]
        return httpx.Response(200, json={"id": check_id}, request=httpx.Request("PATCH", url))

    def with_token(_, fn):
        return fn("test")

    monkeypatch.setattr(time, "monotonic", lambda: now)
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "0")
    monkeypatch.setattr(dispatch, "with_install_token_retry", with_token)
    monkeypatch.setattr(review_timing, "with_install_token_retry", with_token)
    monkeypatch.setattr(hunt_recorder, "with_install_token_retry", lambda *_: None)
    monkeypatch.setattr(install_store, "get_elder_last_reviewed", Mock(return_value=None))
    monkeypatch.setattr(install_store, "get_repo_config", Mock(return_value={}))
    monkeypatch.setattr(install_store, "put_elder_last_reviewed", Mock())
    monkeypatch.setattr(install_store, "get_review_session", Mock(return_value=None))
    monkeypatch.setattr(install_store, "put_review_session", Mock())
    monkeypatch.setattr(dispatch, "get_app_id", lambda: 1)
    monkeypatch.setattr(dispatch, "fetch_pr_diff", timed(9, _DIFF))
    monkeypatch.setattr(dispatch, "fetch_file_contents", lambda *args, **kwargs: {})
    monkeypatch.setattr(dispatch, "review_diff", timed(170, LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.CAVE,
    )))
    monkeypatch.setattr(dispatch, "scan_complexity_full", timed(21, ComplexityScan((), ())))
    monkeypatch.setattr(dispatch, "record_check_verdict", Mock())
    monkeypatch.setattr(dispatch, "list_reviews", lambda *args, **kwargs: [])
    monkeypatch.setattr(httpx, "get", Mock(return_value=Mock(json=dict)))
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "patch", patch)
    caplog.set_level(logging.INFO)
    return timed, checks


def _assert_timing(checks, caplog, *, total, context, model, analyzers, post):
    seconds = {"total": total, "context": context, "model": model, "analyzers": analyzers, "post": post}
    expected = "elder-timing: " + " ".join(f"{key}={value}" for key, value in seconds.items())
    assert checks
    for summary in checks.values():
        assert [line for line in summary.splitlines() if line.startswith("elder-timing:")] == [expected]
    record, = [record for record in caplog.records if record.msg == "elder_timing"]
    assert record.pr_number == 7
    assert record.head_sha == _HEAD
    assert {key: getattr(record, key) for key in seconds} == seconds


def test_completed_review_stamps_stage_seconds_including_posting(review, caplog):
    _, checks = review

    result = dispatch.dispatch_code_review(_PAYLOAD, blocking=True)

    assert result["result"] == "pass"
    _assert_timing(checks, caplog, total=212, context=9, model=170, analyzers=21, post=12)


def test_config_skip_keeps_model_and_analyzers_at_zero(review, caplog, monkeypatch):
    timed, checks = review
    payload = {**_PAYLOAD, "pull_request": {
        **_PAYLOAD["pull_request"], "base": {"sha": "b" * 40}, "title": "Skip this review",
    }}
    monkeypatch.setattr(dispatch, "fetch_file_contents", timed(9, {
        ".grug.yaml": 'skip_prs:\n  title_patterns: ["Skip *"]',
    }))

    result = dispatch.dispatch_code_review(payload, blocking=True)

    assert result["skipped_by"]
    _assert_timing(checks, caplog, total=14, context=9, model=0, analyzers=0, post=5)


def test_failed_context_keeps_elapsed_time_in_total(review, caplog, monkeypatch):
    timed, checks = review
    monkeypatch.setattr(dispatch, "fetch_pr_diff", timed(9, httpx.ConnectError("offline")))

    result = dispatch.dispatch_code_review(_PAYLOAD, blocking=True)

    assert result["degraded_reason"] == "fetch_or_parse_failed"
    assert checks[1].startswith("Degraded review")
    _assert_timing(checks, caplog, total=14, context=0, model=0, analyzers=0, post=5)


def test_failed_model_keeps_elapsed_time_in_total(review, caplog, monkeypatch):
    timed, checks = review
    monkeypatch.setattr(dispatch, "review_diff", timed(170, LlmReviewResponse(
        kind="parse_failed", findings=(),
    )))

    result = dispatch.dispatch_code_review(_PAYLOAD, blocking=True)

    assert result["degraded_reason"] == "parse_failed"
    _assert_timing(checks, caplog, total=205, context=9, model=0, analyzers=21, post=5)


def test_deep_append_stamps_both_checks_and_logs_once(review, caplog, monkeypatch):
    timed, checks = review
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "1")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "tiered")
    monkeypatch.setattr(dispatch, "review_reasoner_diff", timed(30, LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.CAVE_REASONER,
    )))

    payload = {**_PAYLOAD, "pull_request": {**_PAYLOAD["pull_request"], "body": "deep review"}}
    dispatch.dispatch_code_review(payload, blocking=True)

    assert len(checks) == 2
    _assert_timing(checks, caplog, total=247, context=9, model=200, analyzers=21, post=17)


def test_timing_line_survives_a_truncated_summary(review, caplog, monkeypatch):
    _, checks = review
    monkeypatch.setattr(dispatch, "_summary_markdown", lambda *a, **k: ("Verdict", "x" * 70000))

    dispatch.dispatch_code_review(_PAYLOAD, blocking=True)

    assert "(summary truncated)" in checks[1]
    assert len(checks[1]) < 65535
    _assert_timing(checks, caplog, total=212, context=9, model=170, analyzers=21, post=12)


def test_failed_timing_update_keeps_verdict_and_log(review, caplog, monkeypatch):
    timed, checks = review
    monkeypatch.setattr(httpx, "patch", timed(3, httpx.ConnectError("offline")))

    result = dispatch.dispatch_code_review(_PAYLOAD, blocking=True)

    assert result["result"] == "pass"
    assert checks[1].startswith("Reviewed head")
    record, = [record for record in caplog.records if record.msg == "elder_timing"]
    assert record.total == 212
    assert sum(record.msg == "elder_timing_publish_failed" for record in caplog.records) == 1
