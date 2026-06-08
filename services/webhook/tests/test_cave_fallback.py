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
        diff_ref={"kind": "inline", "hunks": [{"path": "a.py", "body": "@@ x"}]},
    )
    d = json.loads(job.to_json())
    assert d["install_id"] == 42
    assert d["repo"] == "acme/widget"
    assert d["persona"] == "elder"
    assert d["schema_version"] == 2  # #311 bumped the wire shape
    assert d["diff_ref"]["kind"] == "inline"
    assert d["diff_ref"]["hunks"] == [{"path": "a.py", "body": "@@ x"}]


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
    # Small diff → inline DiffRef (no S3).
    assert body["diff_ref"]["kind"] == "inline"
    assert body["diff_ref"]["hunks"] == [{"path": "a.py", "body": "@@ -1 +1 @@\n-x\n+y"}]
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


# --- consumer: handle_fallback_result (heal) ------------------------------

def _result_body(**over) -> str:
    d = {
        "schema_version": 1,
        "install_id": 42,
        "repo": "acme/widget",
        "pr_number": 7,
        "head_sha": "deadbeef0000",
        "persona": "elder",
        "findings": [{"file": "a.py", "line": 4, "severity": "high",
                      "rule_name": "null-deref", "message": "x may be None"}],
    }
    d.update(over)
    return json.dumps(d)


def _event(*bodies) -> dict:
    return {"Records": [{"eventSource": "aws:sqs", "body": b} for b in bodies]}


def test_handle_result_heals_publishes_check_and_records_verdict():
    # with_install_token_retry(install_id, fn) → invoke fn with a fake token so
    # the real post_check_run call path is exercised (not stubbed away).
    with patch.object(cf, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch.object(cf, "post_check_run") as post, \
         patch.object(cf, "record_check_verdict") as rec:
        out = cf.handle_fallback_result(_event(_result_body()))
    assert out == {"records": 1, "healed": 1, "failed": 0}
    # Published to the SAME check name + head, so it heals (not duplicates).
    _, args, kwargs = post.mock_calls[0]
    assert args[0] == "tok" and args[1] == "acme" and args[2] == "widget"
    check = args[3]
    assert check.name == "Grug — Code Review"
    assert check.head_sha == "deadbeef0000"
    assert check.conclusion == "neutral"
    # Verdict healed: errored → reviewed, real findings_count, no degraded_reason.
    rkw = rec.call_args.kwargs
    assert rkw["persona_key"] == "code_reviewer"
    assert rkw["findings_count"] == 1
    assert rkw["degraded_reason"] is None
    assert rkw["head_sha"] == "deadbeef0000"


def test_handle_result_degraded_does_not_fake_a_review():
    # Cave ALSO failed → leave the verdict errored (no publish, no heal).
    with patch.object(cf, "with_install_token_retry") as tok, \
         patch.object(cf, "post_check_run") as post, \
         patch.object(cf, "record_check_verdict") as rec:
        out = cf.handle_fallback_result(
            _event(_result_body(degraded=True, degraded_reason="cave_unreachable", findings=[]))
        )
    assert out == {"records": 1, "healed": 1, "failed": 0}  # processed, not failed
    post.assert_not_called()
    rec.assert_not_called()
    tok.assert_not_called()


def test_handle_result_malformed_body_is_dropped_not_raised():
    with patch.object(cf, "post_check_run") as post:
        out = cf.handle_fallback_result(_event("this is not json"))
    assert out == {"records": 1, "healed": 0, "failed": 1}
    post.assert_not_called()


def test_handle_result_publish_error_is_caught():
    with patch.object(cf, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch.object(cf, "post_check_run", side_effect=RuntimeError("GH 503")), \
         patch.object(cf, "record_check_verdict") as rec:
        out = cf.handle_fallback_result(_event(_result_body()))
    assert out == {"records": 1, "healed": 0, "failed": 1}  # never raises out
    rec.assert_not_called()  # publish failed before the heal


def test_handle_result_empty_records():
    assert cf.handle_fallback_result({"Records": []}) == {"records": 0, "healed": 0, "failed": 0}


def test_handle_result_clean_review_titles_no_omens():
    with patch.object(cf, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch.object(cf, "post_check_run") as post, \
         patch.object(cf, "record_check_verdict"):
        cf.handle_fallback_result(_event(_result_body(findings=[])))
    check = post.mock_calls[0].args[3]
    assert "no bad omens" in check.title.lower()


# --- peer-review hardenings (#322): size guard + markdown safety -----------

def test_enqueue_skips_oversized_diff(monkeypatch):
    """A diff over the SQS 256KB inline cap is skipped with a clear signal
    (S3 spillover is #311) — not sent, not a generic failure."""
    big = [Hunk(path="big.py", body="x" * 260_000)]
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "https://sqs/jobs.fifo")
    with patch("secrets_loader.get_fallback_enabled", return_value=True), \
         patch.object(cf._sqs, "send_message") as send:
        out = cf.enqueue_fallback(big, installation_id=1, repo="a/b", pr_number=1, head_sha="h")
    assert out is False
    send.assert_not_called()  # never attempts a doomed >256KB send


def test_summarize_neutralizes_markdown_from_connector_findings():
    """Connector findings come from an LLM over a PR diff — backticks/pipes/
    newlines must not break or inject into the check-run summary."""
    title, body = cf._summarize((
        {"severity": "high", "rule_name": "x`y|z",
         "file": "a.py", "line": 1, "message": "line1\nline2 `code` |pipe|"},
    ))
    # No raw newlines or pipes survive into the per-finding line.
    finding_line = [l for l in body.splitlines() if l.startswith("- ")][0]
    assert "\n" not in finding_line
    assert "line1 line2" in finding_line  # newline collapsed to space
    assert "`code`" not in finding_line   # backticks neutralized


# --- DiffRef codec (#311): pack/unpack, inline-vs-S3 boundary, round-trip ---

import io  # noqa: E402

_SMALL = [Hunk(path="a.py", body="@@ -1 +1 @@\n-x\n+y")]
_BIG = [Hunk(path="big.py", body="x" * 260_000)]


def test_pack_diff_inline_for_small_diff(monkeypatch):
    monkeypatch.setattr(cf, "_DIFF_BUCKET", "")  # no bucket needed for inline
    with patch.object(cf._s3, "put_object") as put:
        ref = cf.pack_diff(_SMALL, install_id=1, head_sha="h")
    assert ref == {"kind": "inline", "hunks": [{"path": "a.py", "body": "@@ -1 +1 @@\n-x\n+y"}]}
    put.assert_not_called()  # small diff never touches S3


def test_pack_diff_spills_large_diff_to_s3(monkeypatch):
    monkeypatch.setattr(cf, "_DIFF_BUCKET", "grug-cave-diffs")
    with patch.object(cf._s3, "put_object") as put:
        ref = cf.pack_diff(_BIG, install_id=42, head_sha="cafef00d")
    assert ref == {"kind": "s3", "bucket": "grug-cave-diffs", "key": "diffs/42/cafef00d.json"}
    put.assert_called_once()
    pk = put.call_args.kwargs
    assert pk["Bucket"] == "grug-cave-diffs" and pk["Key"] == "diffs/42/cafef00d.json"


def test_pack_diff_none_when_large_and_no_bucket(monkeypatch):
    monkeypatch.setattr(cf, "_DIFF_BUCKET", "")
    assert cf.pack_diff(_BIG, install_id=1, head_sha="h") is None  # can't spill → no-op


def test_pack_diff_none_when_spill_fails(monkeypatch):
    monkeypatch.setattr(cf, "_DIFF_BUCKET", "grug-cave-diffs")
    with patch.object(cf._s3, "put_object", side_effect=RuntimeError("s3 down")):
        assert cf.pack_diff(_BIG, install_id=1, head_sha="h") is None  # best-effort


def test_unpack_diff_inline_round_trips():
    ref = cf.pack_diff(_SMALL, install_id=1, head_sha="h")
    hunks = cf.unpack_diff(ref)
    assert [(h.path, h.body) for h in hunks] == [("a.py", "@@ -1 +1 @@\n-x\n+y")]


def test_unpack_diff_s3_fetches_and_reconstructs(monkeypatch):
    payload = json.dumps([{"path": "big.py", "body": "x" * 10}]).encode("utf-8")
    with patch.object(cf._s3, "get_object", return_value={"Body": io.BytesIO(payload)}) as get:
        hunks = cf.unpack_diff({"kind": "s3", "bucket": "b", "key": "k"})
    get.assert_called_once_with(Bucket="b", Key="k")
    assert [(h.path, h.body) for h in hunks] == [("big.py", "x" * 10)]


def test_unpack_diff_round_trips_large_via_s3(monkeypatch):
    # pack (spill) then unpack (fetch) reconstructs the SAME hunks.
    monkeypatch.setattr(cf, "_DIFF_BUCKET", "grug-cave-diffs")
    stored = {}
    monkeypatch.setattr(cf._s3, "put_object",
                        lambda **kw: stored.__setitem__("body", kw["Body"]))
    ref = cf.pack_diff(_BIG, install_id=7, head_sha="hh")
    monkeypatch.setattr(cf._s3, "get_object",
                        lambda **kw: {"Body": io.BytesIO(stored["body"])})
    hunks = cf.unpack_diff(ref)
    assert [(h.path, h.body) for h in hunks] == [("big.py", "x" * 260_000)]


def test_unpack_diff_rejects_unknown_kind():
    import pytest
    with pytest.raises(ValueError):
        cf.unpack_diff({"kind": "carrier-pigeon"})


def test_enqueue_spills_large_diff_and_sends_s3_ref(monkeypatch):
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "https://sqs/jobs.fifo")
    monkeypatch.setattr(cf, "_DIFF_BUCKET", "grug-cave-diffs")
    with patch("secrets_loader.get_fallback_enabled", return_value=True), \
         patch.object(cf._s3, "put_object") as put, \
         patch.object(cf._sqs, "send_message") as send:
        out = cf.enqueue_fallback(_BIG, installation_id=42, repo="a/b", pr_number=1, head_sha="deadbeef")
    assert out is True
    put.assert_called_once()  # spilled
    body = json.loads(send.call_args.kwargs["MessageBody"])
    assert body["diff_ref"]["kind"] == "s3"  # message carries only the pointer
    assert "hunks" not in body["diff_ref"]   # the big diff is NOT inline
