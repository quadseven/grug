"""#272 — async Elder offload: enqueue + worker + idempotency.

Covers the three new seams that move the Elder LLM review off the webhook
ACK path:
  - `enqueue_elder_review` — the fire-and-forget offload (on k8s, a
    background thread; degrades to False, never raises).
  - `run_elder_job` — the async worker (idempotent on delivery_id; never
    re-raises so a retry doesn't storm).
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
    """A GitHub pull_request payload with review context plus bulky fields."""
    return {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "head": {"sha": "abc123"},
            "base": {"sha": "base123"},
            "title": "Improve the reviewer",
            "body": body,
            "draft": False,
            "user": {"login": "evan"},
        },
        "repository": {
            "owner": {"login": "githumps", "id": 999, "url": "https://x"},
            "name": "grug",
            "description": "x" * 1000,  # bulky — must be dropped
        },
        "installation": {"id": 555},
        "sender": {"login": "someone", "id": 1, "avatar_url": "y" * 1000},
    }


def test_slim_payload_keeps_bounded_review_context():
    """The async worker needs intent and immutable diff coordinates, not just
    IDs. Preserve those fields while still dropping unrelated event bulk."""
    job_payload = ad._slim_payload(_full_gh_payload(body="m" * 5000))
    assert job_payload == {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "head": {"sha": "abc123"},
            "base": {"sha": "base123"},
            "title": "Improve the reviewer",
            "body": "m" * 5000,
            "draft": False,
            "user": {"login": "evan"},
        },
        "repository": {"owner": {"login": "githumps"}, "name": "grug"},
        "installation": {"id": 555},
    }


def test_slim_payload_bounds_body_and_drops_unrelated_fields():
    """Intent survives, but a huge PR body cannot make the job unbounded."""
    huge_body = "z" * 200_000
    slim = ad._slim_payload(_full_gh_payload(body=huge_body))
    raw = json.dumps(slim)
    assert len(raw) < 20_000
    assert len(slim["pull_request"]["body"]) == ad._MAX_PR_BODY_CHARS
    assert "avatar_url" not in raw


def test_enqueue_returns_false_without_runtime(monkeypatch):
    """Local/test (no GRUG_K8S_RUNTIME) → no offload path → False, no
    exception, and no thread spawned."""
    monkeypatch.delenv("GRUG_K8S_RUNTIME", raising=False)
    monkeypatch.delenv("GRUG_ELDER_DURABLE_QUEUE", raising=False)
    with patch.object(ad.threading, "Thread") as mock_thread:
        ok = ad.enqueue_elder_review(payload={}, delivery_id="d-2", blocking=False)
    assert ok is False
    mock_thread.assert_not_called()


def test_enqueue_k8s_runtime_runs_in_process_thread(monkeypatch):
    """#368: with GRUG_K8S_RUNTIME set, the job runs in-process on a
    background thread - slim-projection job shape, returns True (the review
    is NOT dropped)."""
    monkeypatch.setenv("GRUG_K8S_RUNTIME", "1")
    monkeypatch.delenv("GRUG_ELDER_DURABLE_QUEUE", raising=False)
    ran = threading.Event()
    seen: dict = {}

    def fake_job(event):
        seen.update(event)
        ran.set()
        return {"persona": "code_reviewer", "result": "pass"}

    with patch.object(ad, "run_elder_job", side_effect=fake_job):
        ok = ad.enqueue_elder_review(
            payload=_full_gh_payload(body="m" * 5000),
            delivery_id="d-k8s",
            blocking=True,
        )
        assert ran.wait(5.0), "background thread never ran the job"
    assert ok is True
    assert seen["delivery_id"] == "d-k8s"
    assert seen["blocking"] is True
    assert seen[ad.ASYNC_JOB_KEY] == ad.ELDER_REVIEW_JOB
    # Slim projection applies on the thread path (keep-in-sync contract).
    assert seen["payload"]["pull_request"] == {
        "number": 7,
        "head": {"sha": "abc123"},
        "base": {"sha": "base123"},
        "title": "Improve the reviewer",
        "body": "m" * 5000,
        "draft": False,
        "user": {"login": "evan"},
    }
    assert "sender" not in seen["payload"]


def test_enqueue_elder_uses_durable_quiet_window_queue(monkeypatch):
    """Production Elder jobs use the existing durable consumer lane. The
    requested head scopes FIFO dedup; the consumer owns settling and stale-head
    cancellation before review work starts."""
    monkeypatch.setenv("GRUG_ELDER_DURABLE_QUEUE", "1")
    monkeypatch.setenv("GRUG_ELDER_SETTLE_SECONDS", "120")

    with patch("rerun.enqueue_review") as enqueue, \
         patch.object(ad.threading, "Thread") as thread:
        ok = ad.enqueue_elder_review(
            payload=_full_gh_payload(), delivery_id="delivery-1", blocking=True,
        )

    assert ok is True
    enqueue.assert_called_once_with(
        install_id=555,
        repo="githumps/grug",
        pr_number=7,
        requested_base_sha="base123",
        requested_head_sha="abc123",
        requested_title="Improve the reviewer",
        requested_body="",
        settle_seconds=120,
    )
    thread.assert_not_called()



def test_durable_elder_swift_settle_for_tiny_pr(monkeypatch):
    """Swift Hunt: tiny PR drops the quiet window to 0 while still durable."""
    monkeypatch.setenv("GRUG_ELDER_DURABLE_QUEUE", "1")
    monkeypatch.setenv("GRUG_ELDER_SETTLE_SECONDS", "120")
    payload = _full_gh_payload()
    payload["pull_request"]["additions"] = 10
    payload["pull_request"]["deletions"] = 2
    payload["pull_request"]["changed_files"] = 1

    with patch("rerun.enqueue_review") as enqueue:
        ok = ad.enqueue_elder_review(
            payload=payload, delivery_id="swift-1", blocking=False,
        )

    assert ok is True
    assert enqueue.call_args.kwargs["settle_seconds"] == 0


def test_durable_elder_draft_does_not_consume_ready_event_dedup_key(monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_DURABLE_QUEUE", "1")
    payload = _full_gh_payload()
    payload["pull_request"]["draft"] = True

    with patch("rerun.enqueue_review") as enqueue:
        ok = ad.enqueue_elder_review(
            payload=payload, delivery_id="draft-delivery", blocking=False,
        )

    assert ok is True
    enqueue.assert_not_called()


def test_durable_elder_enqueue_failure_is_recorded_for_replay(monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_DURABLE_QUEUE", "1")
    with patch("rerun.enqueue_review", side_effect=RuntimeError("queue down")), \
         pytest.raises(RuntimeError, match="durable Elder review enqueue failed"):
        ad.enqueue_elder_review(
            payload=_full_gh_payload(),
            delivery_id="delivery-2",
            blocking=False,
        )


def test_enqueue_hashes_snapshot_from_full_body_not_slimmed(monkeypatch):
    """Qodo #585: the claim_review snapshot must hash the FULL PR body, not the
    _slim_payload-truncated one - else a body edit past _MAX_PR_BODY_CHARS is
    invisible to idempotency. The enqueued job must carry the full-body hash."""
    from personas.code_reviewer.snapshot import review_snapshot_id_from_pr

    long_body = "x" * (ad._MAX_PR_BODY_CHARS + 500)
    payload = _full_gh_payload(body=long_body)

    captured: dict = {}
    monkeypatch.delenv("GRUG_ELDER_DURABLE_QUEUE", raising=False)
    monkeypatch.setenv("GRUG_K8S_RUNTIME", "1")
    with patch.object(
        ad, "_spawn_local", lambda spec, job: captured.update(job) or True
    ):
        ad.enqueue_elder_review(
            payload=payload, delivery_id="d-full", blocking=False,
        )

    full_hash = review_snapshot_id_from_pr(payload["pull_request"])
    slim_pr = dict(payload["pull_request"])
    slim_pr["body"] = long_body[: ad._MAX_PR_BODY_CHARS]
    slim_hash = review_snapshot_id_from_pr(slim_pr)

    assert captured["review_snapshot_id"] == full_hash
    # Truncation genuinely changes identity, so the slim hash is the wrong one.
    assert full_hash != slim_hash


def test_enqueue_k8s_spawn_failure_degrades_to_false(monkeypatch):
    """#368: a thread-spawn failure must NOT raise into the ACK path -
    it degrades to False so the caller logs enqueue_failed and still ACKs."""
    monkeypatch.setenv("GRUG_K8S_RUNTIME", "1")
    monkeypatch.delenv("GRUG_ELDER_DURABLE_QUEUE", raising=False)
    with patch.object(ad.threading, "Thread", side_effect=RuntimeError("no threads")):
        ok = ad.enqueue_elder_review(payload={}, delivery_id="d-k8s2", blocking=False)
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


def test_run_elder_job_does_not_retry_a_degraded_result():
    """A degraded dispatch result (any reason) is returned as-is and never
    enqueues a re-run from this lane - only an UNHANDLED exception triggers
    self-recovery (#418). Since #586 a single-backend deep review is a
    complete `reviewed` result, so there is no provisional/partial retry."""
    for reason in ("no_diff", "parse_failed", "all_failed"):
        degraded = {
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": reason,
        }
        with (
            patch("adapters.install_store.claim_delivery", return_value=True),
            patch("adapters.install_store.claim_review", return_value=True),
            patch(
                "personas.code_reviewer.dispatch.dispatch_code_review",
                return_value=degraded,
            ),
            patch("rerun.enqueue_rerun") as mock_enq,
        ):
            out = ad.run_elder_job(_FULL_JOB)
        assert out == degraded
        mock_enq.assert_not_called()


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


# --- per-snapshot idempotency (#397) ---------------------------------------
# run_elder_job gates the review on claim_review (snapshot ID) IN ADDITION to
# claim_delivery (per webhook delivery). claim_delivery catches an exact
# redelivery; claim_review catches an exact base/head/title/body re-trigger
# across DIFFERENT deliveries, while intent/base edits and every new head
# still review. The
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


def test_run_elder_job_skips_when_snapshot_already_reviewed():
    """#397 AC2: an exact-snapshot re-trigger where claim_delivery wins (new
    delivery id) but claim_review LOSES → SKIP, dispatch NOT called (no
    duplicate review of unchanged code on `edited`/`ready_for_review`)."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=False),
        patch("personas.code_reviewer.dispatch.dispatch_code_review") as mock_d,
    ):
        out = ad.run_elder_job(_SHA_JOB)
    mock_d.assert_not_called()
    assert out == {"status": "skipped", "reason": "duplicate_snapshot"}


def test_run_elder_job_reviews_when_snapshot_unclaimed():
    """#397 AC1: a fresh snapshot dispatches and uses its digest as claim key."""
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
        persona="code_reviewer",
        head_sha=ad.review_snapshot_id_from_pr(
            _SHA_JOB["payload"]["pull_request"],
        ),
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
    assert second == {"status": "skipped", "reason": "duplicate_snapshot"}


def test_same_head_with_changed_intent_claims_distinct_snapshots():
    claims: list[str] = []
    edited = {
        **_SHA_JOB,
        "delivery_id": "d-edited",
        "payload": {
            **_SHA_JOB["payload"],
            "pull_request": {
                **_SHA_JOB["payload"]["pull_request"],
                "body": "New review intent",
            },
        },
    }
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch(
            "adapters.install_store.claim_review",
            side_effect=lambda **kw: claims.append(kw["head_sha"]) or True,
        ),
        patch(
            "personas.code_reviewer.dispatch.dispatch_code_review",
            return_value={"persona": "code_reviewer", "result": "pass"},
        ) as dispatch,
    ):
        ad.run_elder_job(_SHA_JOB)
        ad.run_elder_job(edited)

    assert len(set(claims)) == 2
    assert dispatch.call_count == 2


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


# ── Guard async job (#466) ────────────────────────────────────────────


def test_run_guard_job_claims_namespaced_delivery():
    """Elder and Guard both dispatch from the SAME webhook delivery;
    claim_delivery is keyed on the raw GUID, so Guard MUST claim a
    namespaced key or whichever persona ran first would silently skip
    the other (#466)."""
    claimed: list = []
    with (
        patch(
            "adapters.install_store.claim_delivery",
            side_effect=lambda d: claimed.append(d) or True,
        ),
        patch(
            "personas.guard.dispatch.dispatch_guard_review",
            return_value={"persona": "guard", "result": "pass"},
        ) as mock_d,
    ):
        out = ad.run_guard_job(_JOB)
    assert claimed == [f"{_JOB['delivery_id']}:guard"]
    mock_d.assert_called_once()
    assert out == {"persona": "guard", "result": "pass"}


def test_run_guard_job_skips_when_claim_lost():
    with (
        patch("adapters.install_store.claim_delivery", return_value=False),
        patch("personas.guard.dispatch.dispatch_guard_review") as mock_d,
    ):
        out = ad.run_guard_job(_JOB)
    mock_d.assert_not_called()
    assert out == {"status": "skipped", "reason": "duplicate_delivery"}


def test_run_guard_job_never_reraises_on_dispatch_error():
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch(
            "personas.guard.dispatch.dispatch_guard_review",
            side_effect=RuntimeError("guard exploded"),
        ),
    ):
        out = ad.run_guard_job(_JOB)
    assert out == {"persona": "guard", "result": "unhandled_error"}


def test_enqueue_guard_review_no_runtime_returns_false(monkeypatch):
    monkeypatch.delenv("GRUG_K8S_RUNTIME", raising=False)
    assert ad.enqueue_guard_review(
        payload={"pull_request": {}, "repository": {}, "installation": {}},
        delivery_id="d1", blocking=False,
    ) is False


def test_run_guard_job_self_recovers_on_dispatch_error():
    """Codex PR #482: run_guard_job takes the head-SHA claim BEFORE
    dispatching, so an unhandled dispatch error must enqueue a durable
    guard rerun - otherwise that SHA's security check is suppressed
    until a new push."""
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=True),
        patch(
            "personas.guard.dispatch.dispatch_guard_review",
            side_effect=RuntimeError("guard exploded"),
        ),
        patch("rerun.enqueue_rerun") as mock_rr,
    ):
        out = ad.run_guard_job({**_FULL_JOB, ad.ASYNC_JOB_KEY: ad.GUARD_REVIEW_JOB})
    assert out == {"persona": "guard", "result": "unhandled_error"}
    mock_rr.assert_called_once()
    assert mock_rr.call_args.kwargs["persona"] == "guard"


# ── Smasher async job (#469) + generic wrapper binding (#77) ──────────


def test_run_smasher_job_claims_namespaced_delivery_and_binds_smasher_row():
    """The generic _run_job serves three personas off one spec table (#77);
    this pins the SMASHER wrapper's row binding - a copy-paste of another
    persona's row would claim the wrong namespace and run the wrong
    dispatch while every other test stays green (audit stage 7)."""
    claimed: list = []
    with (
        patch(
            "adapters.install_store.claim_delivery",
            side_effect=lambda d: claimed.append(d) or True,
        ),
        patch(
            "personas.smasher.dispatch.dispatch_smasher_review",
            return_value={"persona": "smasher", "result": "pass"},
        ) as mock_d,
    ):
        out = ad.run_smasher_job(_JOB)
    assert claimed == [f"{_JOB['delivery_id']}:smasher"]
    mock_d.assert_called_once()
    assert out == {"persona": "smasher", "result": "pass"}


def test_run_walkthrough_job_claims_namespaced_delivery_and_binds_walkthrough_row():
    """The generic _run_job serves four personas off one spec table (#77);
    this pins the TELLER wrapper's row binding - a copy-paste of another
    persona's row would claim the wrong namespace and run the wrong
    dispatch while every other test stays green (audit stage 7, #554)."""
    claimed: list = []
    with (
        patch(
            "adapters.install_store.claim_delivery",
            side_effect=lambda d: claimed.append(d) or True,
        ),
        patch(
            "personas.walkthrough.dispatch.dispatch_walkthrough_review",
            return_value={"persona": "walkthrough", "result": "pass"},
        ) as mock_d,
    ):
        out = ad.run_walkthrough_job(_JOB)
    assert claimed == [f"{_JOB['delivery_id']}:walkthrough"]
    mock_d.assert_called_once()
    assert out == {"persona": "walkthrough", "result": "pass"}


def test_run_walkthrough_job_skips_when_snapshot_already_reviewed():
    """#554: the same head-sha idempotency Elder/Guard/Smasher get for
    free from the generic machinery - a same-head-SHA re-trigger where
    claim_delivery wins (new delivery id) but claim_review LOSES must
    SKIP, never re-post/re-PATCH the walkthrough comment."""
    job = {**_SHA_JOB, ad.ASYNC_JOB_KEY: ad.WALKTHROUGH_REVIEW_JOB}
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=False),
        patch("personas.walkthrough.dispatch.dispatch_walkthrough_review") as mock_d,
    ):
        out = ad.run_walkthrough_job(job)
    mock_d.assert_not_called()
    assert out == {"status": "skipped", "reason": "duplicate_snapshot"}


def test_run_walkthrough_job_reviews_when_snapshot_unclaimed():
    """A fresh snapshot dispatches and uses its digest as claim key."""
    job = {**_SHA_JOB, ad.ASYNC_JOB_KEY: ad.WALKTHROUGH_REVIEW_JOB}
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=True) as mock_c,
        patch(
            "personas.walkthrough.dispatch.dispatch_walkthrough_review",
            return_value={"persona": "walkthrough", "result": "pass"},
        ) as mock_d,
    ):
        out = ad.run_walkthrough_job(job)
    mock_d.assert_called_once()
    mock_c.assert_called_once_with(
        install_id=7, repo="githumps/grug", pr_number=12,
        persona="walkthrough",
        head_sha=ad.review_snapshot_id_from_pr(
            _SHA_JOB["payload"]["pull_request"],
        ),
    )
    assert out == {"persona": "walkthrough", "result": "pass"}


def test_run_smasher_job_self_recovers_with_smasher_persona():
    with (
        patch("adapters.install_store.claim_delivery", return_value=True),
        patch("adapters.install_store.claim_review", return_value=True),
        patch(
            "personas.smasher.dispatch.dispatch_smasher_review",
            side_effect=RuntimeError("boom"),
        ),
        patch("rerun.enqueue_rerun") as mock_rr,
    ):
        out = ad.run_smasher_job({**_FULL_JOB, ad.ASYNC_JOB_KEY: ad.SMASHER_REVIEW_JOB})
    assert out == {"persona": "smasher", "result": "unhandled_error"}
    mock_rr.assert_called_once()
    assert mock_rr.call_args.kwargs["persona"] == "smasher"


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "enqueue_name, runner_name, job_kind",
    [
        ("enqueue_elder_review", "run_elder_job", "elder_review"),
        ("enqueue_guard_review", "run_guard_job", "guard_review"),
        ("enqueue_smasher_review", "run_smasher_job", "smasher_review"),
    ],
)
def test_enqueue_wrappers_bind_their_own_runner_and_job_kind(
    monkeypatch, enqueue_name, runner_name, job_kind
):
    """Wrapper-to-spec-row binding for ALL three personas: each enqueue
    must spawn ITS runner with ITS job kind (#77 audit stage 7)."""
    monkeypatch.setenv("GRUG_K8S_RUNTIME", "1")
    ran = threading.Event()
    seen: dict = {}

    def fake_job(event):
        seen.update(event)
        ran.set()
        return {"result": "pass"}

    with patch.object(ad, runner_name, side_effect=fake_job):
        ok = getattr(ad, enqueue_name)(
            payload=_full_gh_payload(), delivery_id="d-bind", blocking=False,
        )
        assert ran.wait(5.0), f"{runner_name} never ran"
    assert ok is True
    assert seen[ad.ASYNC_JOB_KEY] == job_kind


def test_self_recover_log_extras_carry_persona(caplog):
    """Recovery log lines keep their legacy elder_ NAMES for monitor
    continuity; the persona extra is the only thing making a Guard or
    Smasher recovery attributable in DD - pin it (#77 audit stage 7)."""
    import logging

    with (
        patch("rerun.enqueue_rerun") as mock_rr,
        caplog.at_level(logging.INFO, logger=ad.log.name),
    ):
        ad.self_recover_review(
            _FULL_JOB["payload"], "d-attr", persona="guard",
        )
    mock_rr.assert_called_once()
    enq = [r for r in caplog.records if r.msg == "elder_self_recover_enqueued"]
    assert len(enq) == 1
    assert enq[0].persona == "guard"
