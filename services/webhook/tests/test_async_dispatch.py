"""#272 — async Elder offload: enqueue + worker + idempotency.

Covers the three new seams that move the Elder LLM review off the webhook
ACK path:
  - `enqueue_elder_review` — the fire-and-forget self-invoke (shape of the
    boto3 lambda.invoke; degrades to False, never raises).
  - `run_elder_job` — the async worker (idempotent on delivery_id; never
    re-raises so AWS doesn't retry-storm).
  - `install_store.claim_delivery` — the win-once idempotency claim (only
    the error-propagation contract here; behavior is in the real-PG suite).
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import httpx

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
            delivery_id="d-big",
            blocking=False,
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


def test_enqueue_k8s_runtime_runs_in_process_thread(monkeypatch):
    """#368: off-Lambda with GRUG_K8S_RUNTIME set, the job runs in-process
    on a background thread - same slim-projection job shape, no boto3
    invoke, returns True (the review is NOT dropped)."""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("GRUG_K8S_RUNTIME", "1")
    ran = threading.Event()
    seen: dict = {}

    def fake_job(event):
        seen.update(event)
        ran.set()
        return {"persona": "code_reviewer", "result": "pass"}

    with (
        patch.object(ad, "run_elder_job", side_effect=fake_job),
        patch.object(ad._lambda, "invoke") as mock_invoke,
    ):
        ok = ad.enqueue_elder_review(
            payload=_full_gh_payload(body="m" * 5000),
            delivery_id="d-k8s",
            blocking=True,
        )
        assert ran.wait(5.0), "background thread never ran the job"
    assert ok is True
    mock_invoke.assert_not_called()
    assert seen["delivery_id"] == "d-k8s"
    assert seen["blocking"] is True
    assert seen[ad.ASYNC_JOB_KEY] == ad.ELDER_REVIEW_JOB
    # Slim projection applies on the k8s path too (keep-in-sync contract).
    assert seen["payload"]["pull_request"] == {"number": 7, "head": {"sha": "abc123"}}
    assert "sender" not in seen["payload"]


def test_enqueue_k8s_spawn_failure_degrades_to_false(monkeypatch):
    """#368: a thread-spawn failure must NOT raise into the ACK path -
    same degrade-to-False contract as a Lambda invoke error."""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("GRUG_K8S_RUNTIME", "1")
    with patch.object(ad.threading, "Thread", side_effect=RuntimeError("no threads")):
        ok = ad.enqueue_elder_review(payload={}, delivery_id="d-k8s2", blocking=False)
    assert ok is False


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
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "pass"},
        ) as mock_d,
    ):
        out = ad.run_elder_job(_JOB)
    mock_d.assert_called_once()
    _, kwargs = mock_d.call_args
    assert kwargs["blocking"] is False
    assert out == {"persona": "code_reviewer", "result": "pass"}


def test_run_elder_job_skips_when_claim_lost():
    """Duplicate delivery (GitHub redelivery or AWS async retry) → claim
    lost → SKIP, dispatch_code_review NOT called (no double review)."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=False),
        patch("personas.code_reviewer.dispatch.dispatch_code_review") as mock_d,
    ):
        out = ad.run_elder_job(_JOB)
    mock_d.assert_not_called()
    assert out == {"status": "skipped", "reason": "duplicate_delivery"}


def test_run_elder_job_never_reraises_on_dispatch_error():
    """An unhandled error in the Elder dispatch must NOT propagate (that
    would make AWS retry-storm the async invocation) — degrade to a status
    dict."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            side_effect=RuntimeError("boom"),
        ),
    ):
        out = ad.run_elder_job(_JOB)
    assert out == {"persona": "code_reviewer", "result": "unhandled_error"}


# --- self-recovery (#418) --------------------------------------------------

_FULL_JOB = {
    ad.ASYNC_JOB_KEY: ad.ELDER_REVIEW_JOB,
    "delivery_id": "d-42",
    "blocking": False,
    "payload": {
        "installation": {"id": 99},
        "repository": {"owner": {"login": "githumps"}, "name": "grug"},
        "pull_request": {"number": 415, "head": {"sha": "abc"}},
    },
}


def test_run_elder_job_self_recovers_on_dispatch_error():
    """#418: an unhandled Elder failure enqueues exactly ONE durable re-run
    (persona=elder) so the review recovers with no human re-push."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=True),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            side_effect=RuntimeError("boom"),
        ),
        patch("rerun.enqueue_rerun") as mock_enq,
    ):
        out = ad.run_elder_job(_FULL_JOB)
    assert out == {"persona": "code_reviewer", "result": "unhandled_error"}
    mock_enq.assert_called_once_with(
        install_id=99, repo="githumps/grug", pr_number=415, persona="elder"
    )


def test_self_recover_skips_when_ids_missing():
    """#418: a slim payload lacking install/repo/pr ids skips the re-run
    (logged) instead of enqueuing a malformed job."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            side_effect=RuntimeError("boom"),
        ),
        patch("rerun.enqueue_rerun") as mock_enq,
    ):
        out = ad.run_elder_job(_JOB)  # payload has only pull_request.number
    assert out["result"] == "unhandled_error"
    mock_enq.assert_not_called()


def test_self_recover_is_best_effort_on_enqueue_failure():
    """#418: recovery never re-raises - a failure to enqueue the re-run is
    swallowed (the worker already degraded), so the watchdog/thread is safe."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=True),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            side_effect=RuntimeError("boom"),
        ),
        patch("rerun.enqueue_rerun", side_effect=RuntimeError("queue down")),
    ):
        out = ad.run_elder_job(_FULL_JOB)  # must not raise
    assert out == {"persona": "code_reviewer", "result": "unhandled_error"}


def test_two_jobs_same_delivery_dispatch_once():
    """Integrated idempotency: two run_elder_job calls for the SAME delivery
    (first claim wins, second loses) → dispatch_code_review runs exactly
    once. The anti-double-review invariant, not just branch handling."""
    with (
        patch("adapters.install_store.claim_delivery", side_effect=[True, False]),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "pass"},
        ) as mock_d,
    ):
        first = ad.run_elder_job(_JOB)
        second = ad.run_elder_job(_JOB)
    assert mock_d.call_count == 1
    assert first == {"persona": "code_reviewer", "result": "pass"}
    assert second == {"status": "skipped", "reason": "duplicate_delivery"}


# --- THE contract test: _slim_payload output must satisfy the REAL consumer.
# Both are hand-maintained; if _slim_payload drops a field dispatch_code_review
# reads (bracket access → KeyError at runtime, never recovers), the per-half
# unit tests stay green but prod async invocations crash on every PR. This
# wires the real projection into the real consumer so the contract is
# load-bearing, not coincidental.


def test_slim_payload_satisfies_dispatch_code_review_consumer(monkeypatch):
    from personas.code_reviewer import dispatch as cr_dispatch
    from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse

    full = _full_gh_payload(body="x" * 4000)
    slim = ad._slim_payload(full)

    # Stub dispatch_code_review's IO so it runs offline but exercises EVERY
    # payload field read (fetch→parse→review→PUBLISH) against the slim dict.
    monkeypatch.setattr(
        cr_dispatch,
        "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )
    diff_resp = MagicMock(spec=httpx.Response)
    diff_resp.status_code = 200
    diff_resp.raise_for_status = MagicMock()
    diff_resp.text = "diff --git a/x.py b/x.py\n@@ -0,0 +1 @@\n+pass\n"
    # Return a REAL finding (on a line in the diff) so the publish path —
    # post_review + comment capture, which read owner/repo/pull_number/
    # head_sha from the slim payload — is actually traversed, not skipped
    # by an empty-findings early return (codex WARN-3).
    monkeypatch.setattr(
        cr_dispatch,
        "review_diff",
        lambda *a, **kw: LlmReviewResponse(
            kind="reviewed",
            findings=(
                LlmFinding(
                    path="x.py",
                    line=1,
                    rule="silent-failure",
                    severity="medium",
                    message="m",  # type: ignore[arg-type]
                ),
            ),
            backend_used=Backend.POOLSIDE,
        ),
    )
    posted = []
    monkeypatch.setattr(
        cr_dispatch,
        "post_check_run",
        lambda *a, **kw: posted.append("check") or {"id": 1},
    )
    monkeypatch.setattr(
        cr_dispatch,
        "post_review",
        lambda *a, **kw: posted.append("review") or {"id": 2},
    )
    # Comment-capture (best-effort, post-publish) also reads the payload-
    # derived ids before short-circuiting on an empty comment list; stub its
    # GH fetch so it runs without network.
    monkeypatch.setattr(cr_dispatch, "get_review_comments", lambda *a, **kw: [])

    with patch("httpx.get", return_value=diff_resp):
        # If _slim_payload dropped a field the consumer (or its publish/
        # capture callees) brackets-into, this raises KeyError instead of
        # returning a result.
        out = cr_dispatch.dispatch_code_review(slim, blocking=False)

    assert out["persona"] == "code_reviewer"
    assert "check" in posted and "review" in posted  # reached BOTH publishes


def test_run_elder_job_fails_open_when_claim_errors():
    """A store hiccup on the claim must not drop the review — fail OPEN
    (run it). A possible duplicate beats a silently-skipped review."""
    with (
        patch(
            "adapters.install_store.claim_delivery",
            side_effect=RuntimeError("ddb down"),
        ),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "pass"},
        ) as mock_d,
    ):
        out = ad.run_elder_job(_JOB)
    mock_d.assert_called_once()
    assert out["result"] == "pass"


# --- claim_delivery (idempotency) ------------------------------------------
# Behavioral coverage (win-once, redelivery skips, empty-id fails open,
# expired-claim takeover, concurrent single-winner) lives in the real-PG
# suite: services/api/tests/test_pg_stores.py. Only the error-propagation
# contract is asserted here because it needs a broken pool, not a real one.


def test_claim_delivery_db_error_propagates(monkeypatch):
    """A real database error must NOT be swallowed into a false 'claimed'
    that would drop the review — it propagates so run_elder_job's fail-open
    catch runs it anyway."""
    import psycopg

    import adapters.pg_install_store as pg_ins

    class _BoomPool:
        def connection(self):
            raise psycopg.OperationalError("pg down")

    monkeypatch.setattr(pg_ins.pg_base, "maybe_purge_expired", lambda: None)
    monkeypatch.setattr(pg_ins, "get_pool", lambda: _BoomPool())
    try:
        ins.claim_delivery("uuid-x")
    except psycopg.Error:
        pass
    else:
        raise AssertionError("expected psycopg.Error to propagate")


# --- per-head-SHA idempotency (#397) ---------------------------------------
# run_elder_job gates the review on claim_review (head SHA) IN ADDITION to
# claim_delivery (per webhook delivery). claim_delivery catches an exact
# redelivery; claim_review catches a same-SHA re-trigger across DIFFERENT
# deliveries (a non-push `edited`/`ready_for_review` event), so unchanged
# code is not re-reviewed while every NEW head SHA still reviews. The
# claim_review win-once/expired-takeover behavior lives in the real-PG suite
# (test_pg_stores.py); these assert the run_elder_job GATE wiring.

_SHA_JOB = {
    ad.ASYNC_JOB_KEY: ad.ELDER_REVIEW_JOB,
    "delivery_id": "d-100",
    "blocking": False,
    "payload": {
        "installation": {"id": 7},
        "repository": {"owner": {"login": "githumps"}, "name": "grug"},
        "pull_request": {"number": 12, "head": {"sha": "sha-aaa"}},
    },
}


def test_run_elder_job_skips_when_head_sha_already_reviewed():
    """#397 AC2: a same-head-SHA re-trigger where claim_delivery wins (new
    delivery id) but claim_review LOSES → SKIP, dispatch NOT called (no
    duplicate review of unchanged code on `edited`/`ready_for_review`)."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=False),
        patch("personas.code_reviewer.dispatch.dispatch_code_review") as mock_d,
    ):
        out = ad.run_elder_job(_SHA_JOB)
    mock_d.assert_not_called()
    assert out == {"status": "skipped", "reason": "duplicate_head_sha"}


def test_run_elder_job_reviews_when_head_sha_unclaimed():
    """#397 AC1: a fresh head SHA (claim_review won) → dispatch runs, claimed
    with the exact (install, repo, pr, persona, head_sha) tuple."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=True) as mock_c,
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "pass"},
        ) as mock_d,
    ):
        out = ad.run_elder_job(_SHA_JOB)
    mock_d.assert_called_once()
    mock_c.assert_called_once_with(
        install_id=7, repo="githumps/grug", pr_number=12,
        persona="code_reviewer", head_sha="sha-aaa",
    )
    assert out == {"persona": "code_reviewer", "result": "pass"}


def test_two_commits_distinct_sha_both_review():
    """#397 AC1: two pushes with DIFFERENT head SHAs each produce a fresh
    review (claim_review wins for each distinct SHA — no silent skip)."""
    job_b = {
        **_SHA_JOB, "delivery_id": "d-b",
        "payload": {
            **_SHA_JOB["payload"],
            "pull_request": {"number": 12, "head": {"sha": "sha-bbb"}},
        },
    }
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=True),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "pass"},
        ) as mock_d,
    ):
        ad.run_elder_job(_SHA_JOB)
        ad.run_elder_job(job_b)
    assert mock_d.call_count == 2


def test_same_sha_two_deliveries_dispatch_once():
    """#397 AC2 integrated: the SAME head SHA across two DIFFERENT deliveries
    (e.g. a push then an `edited`) — claim_delivery wins both (distinct ids)
    but claim_review wins once → dispatch runs exactly once."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", side_effect=[True, False]),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "pass"},
        ) as mock_d,
    ):
        first = ad.run_elder_job(_SHA_JOB)
        second = ad.run_elder_job({**_SHA_JOB, "delivery_id": "d-101"})
    assert mock_d.call_count == 1
    assert first == {"persona": "code_reviewer", "result": "pass"}
    assert second == {"status": "skipped", "reason": "duplicate_head_sha"}


def test_run_elder_job_fails_open_when_review_claim_errors():
    """#397: a claim_review DB error must NOT drop the review — fail OPEN
    (run it), the same best-effort contract as claim_delivery."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch(
            "adapters.install_store.claim_review",
            side_effect=RuntimeError("db down"),
        ),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "pass"},
        ) as mock_d,
    ):
        out = ad.run_elder_job(_SHA_JOB)
    mock_d.assert_called_once()
    assert out == {"persona": "code_reviewer", "result": "pass"}


def test_run_elder_job_engaged_gate_preserves_blocking():
    """#397 AC3: with the head-SHA gate ENGAGED (claim_review won, real ids +
    head SHA present), the `blocking` flag still flows through to
    dispatch_code_review unchanged — the new gate must not regress the
    advisory/blocking publish path."""
    job = {**_SHA_JOB, "blocking": True}
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=True),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "fail"},
        ) as mock_d,
    ):
        ad.run_elder_job(job)
    assert mock_d.call_args.kwargs["blocking"] is True


def test_run_elder_job_no_head_sha_falls_through_to_review():
    """#397: a payload missing head SHA does not engage the per-SHA gate
    (fail open) — claim_review is never called and the review still runs."""
    no_sha = {
        ad.ASYNC_JOB_KEY: ad.ELDER_REVIEW_JOB, "delivery_id": "d-x",
        "blocking": False, "payload": {"pull_request": {"number": 5}},
    }
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review") as mock_c,
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "pass"},
        ) as mock_d,
    ):
        ad.run_elder_job(no_sha)
    mock_c.assert_not_called()
    mock_d.assert_called_once()
