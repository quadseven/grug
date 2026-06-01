"""#272 — async Elder offload: enqueue + worker + idempotency.

Covers the three new seams that move the Elder LLM review off the webhook
ACK path:
  - `enqueue_elder_review` — the fire-and-forget self-invoke (shape of the
    boto3 lambda.invoke; degrades to False, never raises).
  - `run_elder_job` — the async worker (idempotent on delivery_id; never
    re-raises so AWS doesn't retry-storm).
  - `install_store.claim_delivery` — the conditional-put idempotency claim.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import async_dispatch as ad
import adapters.install_store as ins


# --- enqueue_elder_review --------------------------------------------------

def _full_gh_payload(body=""):
    """A GitHub pull_request payload with the bulky fields that must NOT be
    forwarded (the 256 KB Event cap)."""
    return {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "head": {"sha": "abc123"},
            "body": body,  # can be ~65 KB of markdown — must be dropped
        },
        "repository": {
            "owner": {"login": "githumps", "id": 999, "url": "https://x"},
            "name": "grug",
            "description": "x" * 1000,  # bulky — must be dropped
        },
        "installation": {"id": 555},
        "sender": {"login": "someone", "id": 1, "avatar_url": "y" * 1000},
    }


def test_enqueue_invokes_self_async_with_slim_job_payload(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "grug-webhook")
    with patch.object(ad._lambda, "invoke") as mock_invoke:
        ok = ad.enqueue_elder_review(
            payload=_full_gh_payload(body="m" * 5000),
            delivery_id="d-1",
            blocking=True,
        )
    assert ok is True
    _, kwargs = mock_invoke.call_args
    assert kwargs["FunctionName"] == "grug-webhook"
    assert kwargs["InvocationType"] == "Event"  # async, doesn't wait
    job = json.loads(kwargs["Payload"])
    assert job[ad.ASYNC_JOB_KEY] == ad.ELDER_REVIEW_JOB
    assert job["delivery_id"] == "d-1"
    assert job["blocking"] is True
    # Only the fields dispatch_code_review reads survive (256 KB Event cap).
    assert job["payload"] == {
        "action": "opened",
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
        "repository": {"owner": {"login": "githumps"}, "name": "grug"},
        "installation": {"id": 555},
    }


def test_enqueue_drops_bulky_payload_fields(monkeypatch):
    """The PR body, sender, and extra repo metadata (the parts that can
    push a payload past the 256 KB async-invoke cap) must NOT be forwarded."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "grug-webhook")
    huge_body = "z" * 200_000  # would blow the 256 KB cap if forwarded
    with patch.object(ad._lambda, "invoke") as mock_invoke:
        ad.enqueue_elder_review(
            payload=_full_gh_payload(body=huge_body),
            delivery_id="d-big", blocking=False,
        )
    raw = mock_invoke.call_args.kwargs["Payload"]
    assert len(raw) < 1000  # slim, bounded — not ~200 KB
    assert b"zzz" not in raw  # the huge body is gone
    assert b"avatar_url" not in raw  # sender stripped


def test_enqueue_returns_false_without_function_name(monkeypatch):
    """Local/test (no AWS_LAMBDA_FUNCTION_NAME) → can't self-invoke → False,
    no exception."""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    with patch.object(ad._lambda, "invoke") as mock_invoke:
        ok = ad.enqueue_elder_review(payload={}, delivery_id="d-2", blocking=False)
    assert ok is False
    mock_invoke.assert_not_called()


def test_enqueue_degrades_to_false_on_invoke_error(monkeypatch):
    """A throttle/transport error on the invoke must NOT raise (the caller
    must still ACK GitHub) — it returns False so the caller logs
    enqueue_failed."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "grug-webhook")
    with patch.object(ad._lambda, "invoke", side_effect=RuntimeError("throttled")):
        ok = ad.enqueue_elder_review(payload={}, delivery_id="d-3", blocking=False)
    assert ok is False


# --- run_elder_job ---------------------------------------------------------

_JOB = {
    ad.ASYNC_JOB_KEY: ad.ELDER_REVIEW_JOB,
    "delivery_id": "d-9",
    "blocking": False,
    "payload": {"pull_request": {"number": 1}},
}


def test_run_elder_job_runs_dispatch_when_claim_won():
    with patch("adapters.install_store.claim_delivery", return_value=True), \
         patch("personas.code_reviewer.dispatch.dispatch_code_review",
               return_value={"persona": "code_reviewer", "result": "pass"}) as mock_d:
        out = ad.run_elder_job(_JOB)
    mock_d.assert_called_once()
    _, kwargs = mock_d.call_args
    assert kwargs["blocking"] is False
    assert out == {"persona": "code_reviewer", "result": "pass"}


def test_run_elder_job_skips_when_claim_lost():
    """Duplicate delivery (GitHub redelivery or AWS async retry) → claim
    lost → SKIP, dispatch_code_review NOT called (no double review)."""
    with patch("adapters.install_store.claim_delivery", return_value=False), \
         patch("personas.code_reviewer.dispatch.dispatch_code_review") as mock_d:
        out = ad.run_elder_job(_JOB)
    mock_d.assert_not_called()
    assert out == {"status": "skipped", "reason": "duplicate_delivery"}


def test_run_elder_job_never_reraises_on_dispatch_error():
    """An unhandled error in the Elder dispatch must NOT propagate (that
    would make AWS retry-storm the async invocation) — degrade to a status
    dict."""
    with patch("adapters.install_store.claim_delivery", return_value=True), \
         patch("personas.code_reviewer.dispatch.dispatch_code_review",
               side_effect=RuntimeError("boom")):
        out = ad.run_elder_job(_JOB)
    assert out == {"persona": "code_reviewer", "result": "unhandled_error"}


def test_run_elder_job_fails_open_when_claim_errors():
    """A DDB hiccup on the claim must not drop the review — fail OPEN
    (run it). A possible duplicate beats a silently-skipped review."""
    with patch("adapters.install_store.claim_delivery",
               side_effect=RuntimeError("ddb down")), \
         patch("personas.code_reviewer.dispatch.dispatch_code_review",
               return_value={"persona": "code_reviewer", "result": "pass"}) as mock_d:
        out = ad.run_elder_job(_JOB)
    mock_d.assert_called_once()
    assert out["result"] == "pass"


# --- claim_delivery (idempotency) ------------------------------------------

class _FakeTable:
    def __init__(self, raise_conditional=False):
        self.raise_conditional = raise_conditional
        self.puts = []

    def put_item(self, **kwargs):
        from botocore.exceptions import ClientError
        self.puts.append(kwargs)
        if self.raise_conditional:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem",
            )
        return {}


def test_claim_delivery_first_caller_wins(monkeypatch):
    fake = _FakeTable(raise_conditional=False)
    monkeypatch.setattr(ins, "_table", fake)
    assert ins.claim_delivery("uuid-1") is True
    put = fake.puts[0]
    assert put["Item"]["PK"] == "DELIVERY#uuid-1"
    assert put["ConditionExpression"] == "attribute_not_exists(PK)"
    assert "ttl" in put["Item"]  # auto-expire so the partition stays bounded


def test_claim_delivery_second_caller_skips(monkeypatch):
    """ConditionalCheckFailed → already claimed → False (caller skips)."""
    monkeypatch.setattr(ins, "_table", _FakeTable(raise_conditional=True))
    assert ins.claim_delivery("uuid-1") is False


def test_claim_delivery_empty_id_fails_open(monkeypatch):
    """No delivery id → can't dedup → fail OPEN (process). A double review
    beats a silently-skipped one."""
    fake = _FakeTable()
    monkeypatch.setattr(ins, "_table", fake)
    assert ins.claim_delivery("") is True
    assert fake.puts == []  # no write attempted


def test_claim_delivery_non_conditional_error_propagates(monkeypatch):
    """A real DDB error (not ConditionalCheckFailed) must NOT be swallowed
    into a false 'claimed' that would drop the review — it propagates so
    run_elder_job's fail-open catch runs it anyway."""
    from botocore.exceptions import ClientError

    class _Boom:
        def put_item(self, **kwargs):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceeded"}}, "PutItem")

    monkeypatch.setattr(ins, "_table", _Boom())
    try:
        ins.claim_delivery("uuid-x")
    except ClientError:
        pass
    else:
        raise AssertionError("expected ClientError to propagate")
