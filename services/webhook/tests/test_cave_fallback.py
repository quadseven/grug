"""Tests for the Elder cave-fallback producer (#310, ADR-0005, spec 0018).

External behavior only: the message-schema round-trip, and the enqueuer's
flag-gate / dedup-key / message-shape / best-effort-degrade contract. The
SQS client is patched (no real AWS) — same style as test_async_dispatch.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import cave_fallback as cf
from llm_client import Hunk


# --- schema contract ------------------------------------------------------


def test_fallback_job_to_json_round_trips():
    job = cf.FallbackJob(
        schema_version=cf.SCHEMA_VERSION,
        install_id=42,
        repo="acme/widget",
        pr_number=7,
        head_sha="deadbeef",
        persona="elder",
        hunks=(("a.py", "@@ -1 +1 @@\n-x\n+y"),),
    )
    d = json.loads(job.to_json())
    assert d["install_id"] == 42
    assert d["repo"] == "acme/widget"
    assert d["persona"] == "elder"
    assert d["hunks"] == [{"path": "a.py", "body": "@@ -1 +1 @@\n-x\n+y"}]


def test_fallback_result_from_json_parses_findings():
    raw = json.dumps(
        {
            "schema_version": 1,
            "install_id": 9,
            "repo": "acme/widget",
            "pr_number": 3,
            "head_sha": "cafef00d",
            "persona": "elder",
            "findings": [{"path": "a.py", "line": 4, "severity": "high"}],
            "model": "cave-model",
        }
    )
    res = cf.FallbackResult.from_json(raw)
    assert res.install_id == 9
    assert res.head_sha == "cafef00d"
    assert res.findings[0]["severity"] == "high"
    assert res.degraded is False
    assert res.model == "cave-model"


def test_fallback_result_from_json_drops_non_dict_findings_and_reads_degraded():
    raw = json.dumps(
        {
            "schema_version": 1,
            "install_id": 1,
            "repo": "a/b",
            "pr_number": 1,
            "head_sha": "abc",
            "findings": ["garbage", {"path": "x"}, 7],
            "degraded": True,
            "degraded_reason": "cave_unreachable",
        }
    )
    res = cf.FallbackResult.from_json(raw)
    assert res.findings == ({"path": "x"},)  # non-dicts dropped
    assert res.degraded is True
    assert res.degraded_reason == "cave_unreachable"


# --- enqueuer contract ----------------------------------------------------

_HUNKS = [Hunk(path="a.py", body="@@ -1 +1 @@\n-x\n+y")]


def _enqueue(**over):
    kw = dict(installation_id=42, repo="acme/widget", pr_number=7, head_sha="deadbeef0000")
    kw.update(over)
    return cf.enqueue_fallback(_HUNKS, **kw)


def test_enqueue_is_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "https://sqs/jobs.fifo")
    with patch("secrets_loader.get_fallback_enabled", return_value=False), \
         patch.object(cf._sqs, "send_message") as send:
        assert _enqueue() is False
    send.assert_not_called()


def test_enqueue_is_noop_without_queue_url(monkeypatch):
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "")
    with patch("secrets_loader.get_fallback_enabled", return_value=True), \
         patch.object(cf._sqs, "send_message") as send:
        assert _enqueue() is False
    send.assert_not_called()


def test_enqueue_is_noop_on_empty_hunks(monkeypatch):
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "https://sqs/jobs.fifo")
    with patch("secrets_loader.get_fallback_enabled", return_value=True), \
         patch.object(cf._sqs, "send_message") as send:
        assert cf.enqueue_fallback([], installation_id=1, repo="a/b", pr_number=1, head_sha="h") is False
    send.assert_not_called()


def test_enqueue_sends_with_install_group_and_head_scoped_dedup(monkeypatch):
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "https://sqs/jobs.fifo")
    with patch("secrets_loader.get_fallback_enabled", return_value=True), \
         patch.object(cf._sqs, "send_message") as send:
        assert _enqueue() is True
    kwargs = send.call_args.kwargs
    assert kwargs["QueueUrl"] == "https://sqs/jobs.fifo"
    # Per-install ordering/rate-control.
    assert kwargs["MessageGroupId"] == "42"
    # Dedup includes head_sha so a new commit isn't deduped against the old job.
    assert kwargs["MessageDeduplicationId"] == "42:acme/widget:7:elder:deadbeef0000"
    body = json.loads(kwargs["MessageBody"])
    assert body["repo"] == "acme/widget" and body["head_sha"] == "deadbeef0000"
    assert body["hunks"] == [{"path": "a.py", "body": "@@ -1 +1 @@\n-x\n+y"}]
    # The job must NOT carry any GitHub credential.
    assert "token" not in kwargs["MessageBody"].lower()


def test_distinct_heads_get_distinct_dedup_ids(monkeypatch):
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "https://sqs/jobs.fifo")
    with patch("secrets_loader.get_fallback_enabled", return_value=True), \
         patch.object(cf._sqs, "send_message") as send:
        _enqueue(head_sha="aaaa")
        _enqueue(head_sha="bbbb")
    ids = {c.kwargs["MessageDeduplicationId"] for c in send.call_args_list}
    assert len(ids) == 2


def test_enqueue_degrades_to_false_on_send_error(monkeypatch):
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "https://sqs/jobs.fifo")
    with patch("secrets_loader.get_fallback_enabled", return_value=True), \
         patch.object(cf._sqs, "send_message", side_effect=RuntimeError("throttled")):
        assert _enqueue() is False  # never raises
