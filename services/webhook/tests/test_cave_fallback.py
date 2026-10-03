"""Tests for the Elder cave-fallback producer + consumer (#310/#1610).

External behavior only: the enqueuer's flag-gate / dedup / message-shape /
best-effort-degrade contract, the DiffRef codec, and the result handler's heal.
The SQS/S3 clients are patched (no real AWS).

#1610: grug now rides the SHARED generic spark_cave envelope (one connector reads
all lanes). grug's rich fields ride inside the envelope's `payload.inline`;
principal_id = str(install_id), request_id = "<repo>:<pr>:<head>". The connector
replies with a generic FallbackResult whose `result` carries findings + model.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import cave_fallback as cf
from llm_client import Hunk

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


def test_enqueue_sends_generic_envelope_with_persona_group_and_head_scoped_dedup(monkeypatch):
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "https://sqs/jobs.fifo")
    with patch("secrets_loader.get_fallback_enabled", return_value=True), \
         patch.object(cf._sqs, "send_message") as send:
        assert _enqueue() is True
    kwargs = send.call_args.kwargs
    assert kwargs["QueueUrl"] == "https://sqs/jobs.fifo"
    # Generic envelope: FIFO group "persona:principal"; dedup adds the request_id
    # (which carries head_sha) so a new commit is a new request, not a dup.
    assert kwargs["MessageGroupId"] == "elder:42"
    assert kwargs["MessageDeduplicationId"] == "elder:42:acme/widget:7:deadbeef0000"
    body = json.loads(kwargs["MessageBody"])
    assert body["persona"] == "elder"
    assert body["principal_id"] == "42"
    assert body["request_id"] == "acme/widget:7:deadbeef0000"
    # grug's rich fields ride inside the inline payload (small -> inline).
    assert body["payload"]["kind"] == "inline"
    payload = body["payload"]["inline"]
    assert payload["install_id"] == 42 and payload["repo"] == "acme/widget"
    assert payload["head_sha"] == "deadbeef0000" and payload["payload_version"] == 2
    assert payload["diff_ref"]["kind"] == "inline"
    assert payload["diff_ref"]["hunks"] == [{"path": "a.py", "body": "@@ -1 +1 @@\n-x\n+y"}]
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
    """Generic FallbackResult body (the connector's wire shape). grug coords are
    encoded in principal_id + request_id; findings/model ride in `result`."""
    install_id = over.pop("install_id", 42)
    repo = over.pop("repo", "acme/widget")
    pr = over.pop("pr_number", 7)
    head = over.pop("head_sha", "deadbeef0000")
    ok = over.pop("ok", True)
    findings = over.pop(
        "findings",
        [{"file": "a.py", "line": 4, "severity": "high",
          "rule_name": "null-deref", "message": "x may be None"}],
    )
    model = over.pop("model", "cave-model")
    d = {
        "schema_version": 1,
        "persona": "elder",
        "principal_id": str(install_id),
        "request_id": f"{repo}:{pr}:{head}",
        "ok": ok,
        "result": None if not ok else {"findings": findings, "model": model},
        "error": over.pop("error", None),
    }
    d.update(over)
    return json.dumps(d)


def _event(*bodies) -> dict:
    return {"Records": [{"eventSource": "aws:sqs", "body": b} for b in bodies]}


def test_handle_result_heals_publishes_check_and_records_verdict():
    calls = []
    with patch.object(
        cf, "publish_persona_check",
        side_effect=lambda **kw: calls.append(kw) or {"persona": "code_reviewer", "result": "healed"},
    ):
        out = cf.handle_fallback_result(_event(_result_body()))
    assert out == {"records": 1, "healed": 1, "failed": 0}
    # Heals the SAME check-run (name + head) via the shared seam, with the
    # legacy persona key and the grug-cr: external id (persona_prefix="cr").
    (kw,) = calls
    assert kw["check_name"] == "Grug - Elder"
    assert kw["head_sha"] == "deadbeef0000"
    assert kw["conclusion"] == "neutral"
    # Verdict healed: errored → reviewed, real findings_count, no degraded_reason.
    assert kw["persona_key"] == "code_reviewer"
    assert kw["persona_prefix"] == "cr"
    assert kw["findings_count"] == 1
    assert kw["degraded_reason"] is None
    assert kw["blocking"] is False
    assert kw["owner"] == "acme" and kw["repo"] == "widget"
    assert kw["pr_number"] == 7


def test_handle_result_drops_non_dict_findings():
    calls = []
    with patch.object(
        cf, "publish_persona_check",
        side_effect=lambda **kw: calls.append(kw) or {"persona": "code_reviewer", "result": "healed"},
    ):
        cf.handle_fallback_result(_event(_result_body(findings=["garbage", {"path": "x"}, 7])))
    # Only the one dict finding counts (tolerance preserved from the old shape).
    assert calls[0]["findings_count"] == 1


def test_handle_result_degraded_does_not_fake_a_review():
    # Cave ALSO failed (ok=False) → leave the verdict errored (no publish, no heal).
    with patch.object(cf, "publish_persona_check") as seam:
        out = cf.handle_fallback_result(
            _event(_result_body(ok=False, error="cave_unreachable"))
        )
    assert out == {"records": 1, "healed": 1, "failed": 0}  # processed, not failed
    seam.assert_not_called()


def test_handle_result_malformed_body_is_dropped_not_raised():
    with patch.object(cf, "publish_persona_check") as seam:
        out = cf.handle_fallback_result(_event("this is not json"))
    assert out == {"records": 1, "healed": 0, "failed": 1}
    seam.assert_not_called()


def test_handle_result_publish_error_is_caught():
    # The seam owns the publish now: a failed POST still records the honest
    # degraded verdict (check_publish_failed) via the seam's merge, and the
    # heal still raises so the record counts as failed, never silently healed.
    import personas.publish_check as seam_mod

    with patch.object(
        seam_mod, "post_check_run", side_effect=RuntimeError("GH 503"),
    ), patch.object(seam_mod, "record_check_verdict") as rec, \
         patch.object(seam_mod, "with_install_token_retry",
                      side_effect=lambda iid, fn: fn("tok")), \
         patch.object(seam_mod.time, "sleep"):
        out = cf.handle_fallback_result(_event(_result_body()))
    assert out == {"records": 1, "healed": 0, "failed": 1}  # never raises out
    rkw = rec.call_args.kwargs
    assert rkw["degraded_reason"] == "check_publish_failed"
    assert rkw["persona_key"] == "code_reviewer"


def test_handle_result_empty_records():
    assert cf.handle_fallback_result({"Records": []}) == {"records": 0, "healed": 0, "failed": 0}


def test_handle_result_clean_review_titles_no_omens():
    calls = []
    with patch.object(
        cf, "publish_persona_check",
        side_effect=lambda **kw: calls.append(kw) or {"persona": "code_reviewer", "result": "healed"},
    ):
        cf.handle_fallback_result(_event(_result_body(findings=[])))
    assert "no bad omens" in calls[0]["title"].lower()


# --- peer-review hardenings (#322): size guard + markdown safety -----------


def test_enqueue_skips_oversized_diff(monkeypatch):
    """A diff over the SQS 256KB inline cap with no S3 bucket is skipped with a
    clear signal — not sent, not a generic failure."""
    big = [Hunk(path="big.py", body="x" * 260_000)]
    monkeypatch.setattr(cf, "_JOBS_QUEUE_URL", "https://sqs/jobs.fifo")
    monkeypatch.setattr(cf, "_DIFF_BUCKET", "")
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
    finding_line = [line for line in body.splitlines() if line.startswith("- ")][0]
    assert "\n" not in finding_line
    assert "line1 line2" in finding_line  # newline collapsed to space
    assert "`code`" not in finding_line   # backticks neutralized


# --- DiffRef codec (#311): pack/unpack, inline-vs-S3 boundary, round-trip ---

import io  # noqa: E402

_SMALL = [Hunk(path="a.py", body="@@ -1 +1 @@\n-x\n+y")]
_BIG = [Hunk(path="big.py", body="x" * 260_000)]


def test_pack_diff_inline_for_small_diff(monkeypatch):
    monkeypatch.setattr(cf, "_DIFF_BUCKET", "")
    with patch.object(cf._s3, "put_object") as put:
        ref = cf.pack_diff(_SMALL, install_id=1, head_sha="h")
    assert ref == {"kind": "inline", "hunks": [{"path": "a.py", "body": "@@ -1 +1 @@\n-x\n+y"}]}
    put.assert_not_called()


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
    assert cf.pack_diff(_BIG, install_id=1, head_sha="h") is None


def test_pack_diff_none_when_spill_fails(monkeypatch):
    monkeypatch.setattr(cf, "_DIFF_BUCKET", "grug-cave-diffs")
    with patch.object(cf._s3, "put_object", side_effect=RuntimeError("s3 down")):
        assert cf.pack_diff(_BIG, install_id=1, head_sha="h") is None


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
    put.assert_called_once()  # diff spilled to S3
    body = json.loads(send.call_args.kwargs["MessageBody"])
    # Envelope payload stays INLINE (it's just metadata + an S3 pointer)...
    assert body["payload"]["kind"] == "inline"
    diff_ref = body["payload"]["inline"]["diff_ref"]
    assert diff_ref["kind"] == "s3"        # ...but the big diff is an S3 ref
    assert "hunks" not in diff_ref         # the big diff is NOT inline
