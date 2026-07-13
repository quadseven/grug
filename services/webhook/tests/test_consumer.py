"""#368 — k8s SQS consumer: the long-poll loop that replaces the two
Lambda event-source mappings.

Covers the delivery-semantics seams (the handlers themselves are tested
in test_rerun.py / test_cave_fallback.py):
  - the ESM-shaped batch-of-1 event handed to the handlers
  - delete-on-success for both queues
  - a rerun handler raise leaves the message (visibility redrive → DLQ)
  - a cave-results raise still deletes (defensive; the handler contract
    is never-raise)
  - receive errors back off and never kill the loop
  - the queue table wiring (env var ↔ handler ↔ delete policy)
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

import consumer


def _spec(
    delete_on_error: bool, handler, *, kind: str = "test-queue",
) -> consumer.QueueSpec:
    return consumer.QueueSpec(
        kind=kind,
        url_env="TEST_QUEUE_URL",
        handler=handler,
        delete_on_error=delete_on_error,
    )


_MSG = {
    "MessageId": "m-1",
    "ReceiptHandle": "r-1",
    "Body": '{"k": "v"}',
    "Attributes": {"ApproximateReceiveCount": "1"},
}

_REVIEW_MSG = {
    **_MSG,
    "Body": json.dumps({"kind": "review", "install_id": 11}),
}


def test_poll_dispatches_esm_shaped_event_and_deletes():
    handler = MagicMock()
    with (
        patch.object(
            consumer._sqs, "receive_message", return_value={"Messages": [_MSG]}
        ),
        patch.object(consumer._sqs, "delete_message") as mock_delete,
    ):
        n = consumer._poll_once(
            _spec(False, handler), "https://q", "arn:aws:sqs:us-east-1:1:q.fifo"
        )
    assert n == 1
    event = handler.call_args.args[0]
    assert event["Records"][0]["eventSource"] == "aws:sqs"
    assert event["Records"][0]["eventSourceARN"] == "arn:aws:sqs:us-east-1:1:q.fifo"
    assert event["Records"][0]["body"] == '{"k": "v"}'
    assert event["Records"][0]["messageId"] == "m-1"
    mock_delete.assert_called_once_with(QueueUrl="https://q", ReceiptHandle="r-1")


def test_poll_handler_raise_leaves_message_for_redrive():
    """The rerun contract's consumer half: a raise must NOT delete, so the
    message reappears after the visibility timeout (like the Lambda ESM retry
    path). The DLQ threshold itself (maxReceiveCount=5) is queue config owned
    by infra/pulumi/__main__.py, not observable from a single poll - this
    test only pins the no-delete-on-raise behavior."""
    handler = MagicMock(side_effect=RuntimeError("re-run failed"))
    with (
        patch.object(
            consumer._sqs, "receive_message", return_value={"Messages": [_MSG]}
        ),
        patch.object(consumer._sqs, "delete_message") as mock_delete,
    ):
        n = consumer._poll_once(_spec(False, handler), "https://q", "arn")
    assert n == 1  # handled (not crashed), just not deleted
    mock_delete.assert_not_called()


def test_poll_hot_review_renews_visibility_until_handler_finishes(monkeypatch):
    """Long Elder work owns a renewable SQS lease; other job kinds do not."""
    renewed = threading.Event()
    calls = 0

    def change_visibility(**kwargs):
        nonlocal calls
        calls += 1
        if calls >= 2:
            renewed.set()

    def handler(_event):
        assert renewed.wait(1.0)

    monkeypatch.setattr(consumer, "_REVIEW_VISIBILITY_HEARTBEAT_S", 0.01)
    with (
        patch.object(
            consumer._sqs, "receive_message", return_value={"Messages": [_REVIEW_MSG]},
        ),
        patch.object(
            consumer._sqs,
            "change_message_visibility",
            side_effect=change_visibility,
        ) as mock_change,
        patch.object(consumer._sqs, "delete_message") as mock_delete,
    ):
        n = consumer._poll_once(
            _spec(False, handler, kind="rerun-jobs"), "https://q", "arn",
        )

    assert n == 1
    assert mock_change.call_count >= 2
    assert mock_change.call_args_list[0].kwargs == {
        "QueueUrl": "https://q",
        "ReceiptHandle": "r-1",
        "VisibilityTimeout": consumer._REVIEW_VISIBILITY_TIMEOUT_S,
    }
    mock_delete.assert_called_once()


def test_poll_review_heartbeat_failure_uses_base_visibility_and_still_runs():
    """A transient IAM/SQS failure must not discard the queued review."""
    handler = MagicMock()
    with (
        patch.object(
            consumer._sqs, "receive_message", return_value={"Messages": [_REVIEW_MSG]},
        ),
        patch.object(
            consumer._sqs,
            "change_message_visibility",
            side_effect=RuntimeError("SQS unavailable"),
        ),
        patch.object(consumer._sqs, "delete_message") as mock_delete,
    ):
        n = consumer._poll_once(
            _spec(False, handler, kind="rerun-jobs"), "https://q", "arn",
        )

    assert n == 1
    handler.assert_called_once()
    mock_delete.assert_called_once()


def test_review_job_health_uses_an_absolute_wall_clock_deadline(monkeypatch):
    """A live handler thread must become unhealthy even while leases renew."""
    monkeypatch.setenv("GRUG_REVIEW_JOB_TIMEOUT_S", "120")
    job = consumer._register_active_review_job(
        "https://q", "receipt-1", "message-1", now=1_000.0,
    )
    try:
        assert consumer._expired_review_jobs(now=1_119.9) == []
        assert consumer._expired_review_jobs(now=1_120.0) == [job]
    finally:
        consumer._unregister_active_review_job("receipt-1")


def test_review_job_timeout_clamps_and_falls_back(monkeypatch):
    monkeypatch.setenv("GRUG_REVIEW_JOB_TIMEOUT_S", "10")
    assert consumer._review_job_timeout_s() == consumer._MIN_REVIEW_JOB_TIMEOUT_S

    monkeypatch.setenv("GRUG_REVIEW_JOB_TIMEOUT_S", "10000")
    assert consumer._review_job_timeout_s() == consumer._MAX_REVIEW_JOB_TIMEOUT_S

    monkeypatch.setenv("GRUG_REVIEW_JOB_TIMEOUT_S", "not-a-number")
    assert consumer._review_job_timeout_s() == consumer._DEFAULT_REVIEW_JOB_TIMEOUT_S

    for value in ("nan", "inf", "-inf"):
        monkeypatch.setenv("GRUG_REVIEW_JOB_TIMEOUT_S", value)
        assert (
            consumer._review_job_timeout_s()
            == consumer._DEFAULT_REVIEW_JOB_TIMEOUT_S
        )


def test_shutdown_returns_sqs_leases_before_releasing_review_claims(monkeypatch):
    """Redelivery must be runnable before its durable claim is made available."""
    order: list[str] = []
    monkeypatch.setattr(
        consumer, "_release_active_review_leases",
        lambda: order.append("leases") or 2,
    )
    monkeypatch.setattr(
        consumer, "_release_active_review_claims",
        lambda: order.append("claims") or 2,
    )

    assert consumer._release_inflight_reviews() == (2, 2)
    assert order == ["leases", "claims"]


def test_shutdown_cleanup_claim_failure_is_reported_without_raising(
    monkeypatch, caplog,
):
    monkeypatch.setattr(consumer, "_release_active_review_leases", lambda: 2)
    monkeypatch.setattr(
        consumer,
        "_release_active_review_claims",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )

    assert consumer._release_inflight_reviews() == (2, 0)
    failures = [
        record for record in caplog.records
        if record.msg == "consumer_review_claim_release_failed"
    ]
    assert len(failures) == 1
    assert failures[0].kind == "RuntimeError"


def test_shutdown_cleanup_has_an_absolute_wait_budget(monkeypatch, caplog):
    release = threading.Event()
    monkeypatch.setattr(consumer, "_REVIEW_CLEANUP_TIMEOUT_S", 0.01)
    monkeypatch.setattr(consumer, "_release_inflight_reviews", release.wait)

    try:
        assert consumer._release_inflight_reviews_bounded() == (0, 0)
    finally:
        release.set()
    assert any(
        record.msg == "consumer_review_cleanup_timed_out"
        for record in caplog.records
    )


def test_active_review_lease_release_stops_renewal_and_resets_visibility():
    job = consumer._register_active_review_job(
        "https://q", "receipt-2", "message-2", now=1_000.0,
    )
    try:
        with patch.object(
            consumer._sqs_cleanup, "change_message_visibility",
        ) as change:
            assert consumer._release_active_review_leases() == 1
        assert job.lease_stop.is_set()
        change.assert_called_once_with(
            QueueUrl="https://q", ReceiptHandle="receipt-2", VisibilityTimeout=0,
        )
    finally:
        consumer._unregister_active_review_job("receipt-2")


def test_stopped_heartbeat_cannot_extend_after_visibility_reset_starts(
    monkeypatch,
):
    stop = threading.Event()
    locked = threading.Lock()
    locked.acquire()
    heartbeat_waiting = threading.Event()

    class SignalingLock:
        def __enter__(self):
            heartbeat_waiting.set()
            locked.acquire()

        def __exit__(self, *_args):
            locked.release()

    visibility_lock = SignalingLock()
    extend = MagicMock()
    monkeypatch.setattr(consumer, "_extend_review_visibility", extend)
    monkeypatch.setattr(consumer, "_REVIEW_VISIBILITY_HEARTBEAT_S", 0.0)
    heartbeat = threading.Thread(
        target=consumer._review_visibility_loop,
        args=("https://q", "receipt", "message", stop, visibility_lock),
    )
    heartbeat.start()

    assert heartbeat_waiting.wait(timeout=1.0)
    stop.set()
    locked.release()
    heartbeat.join(timeout=1.0)

    assert not heartbeat.is_alive()
    extend.assert_not_called()


def test_shutdown_hard_exits_when_a_worker_misses_the_join_budget(monkeypatch):
    """Returning from main cannot leave a wedged non-daemon worker alive."""
    exits: list[int] = []
    monkeypatch.setattr(consumer, "_hard_exit", exits.append)
    worker = MagicMock()
    worker.name = "consume-rerun-jobs-2"
    worker.is_alive.return_value = True

    consumer._exit_if_workers_remain([worker], died=False)

    assert exits == [0]


def test_poll_handler_raise_still_deletes_when_policy_says_so():
    """cave-results defensive path: the handler never raises by contract,
    but if it ever does, retrying a poison result message buys nothing —
    delete it."""
    handler = MagicMock(side_effect=RuntimeError("unexpected"))
    with (
        patch.object(
            consumer._sqs, "receive_message", return_value={"Messages": [_MSG]}
        ),
        patch.object(consumer._sqs, "delete_message") as mock_delete,
    ):
        consumer._poll_once(_spec(True, handler), "https://q", "arn")
    mock_delete.assert_called_once()


def test_poll_empty_receive_returns_zero():
    with patch.object(consumer._sqs, "receive_message", return_value={}):
        assert consumer._poll_once(_spec(False, MagicMock()), "https://q", "arn") == 0


def test_poll_receive_error_backs_off_and_survives(monkeypatch):
    """An IAM gap / bad URL must log + back off, never crash the thread."""
    monkeypatch.setattr(consumer, "_RECEIVE_ERROR_BACKOFF_S", 0.01)
    with patch.object(
        consumer._sqs, "receive_message", side_effect=RuntimeError("403")
    ):
        assert consumer._poll_once(_spec(False, MagicMock()), "https://q", "arn") == 0


def test_specs_wire_queues_to_handlers_and_policies():
    """The queue table IS the delivery contract: rerun redrives on error,
    cave-results does not."""
    import cave_fallback
    import rerun

    specs = {s.kind: s for s in consumer._specs()}
    assert specs["rerun-jobs"].url_env == "GRUG_RERUN_QUEUE_URL"
    assert specs["rerun-jobs"].handler is rerun.handle_rerun_jobs
    assert specs["rerun-jobs"].delete_on_error is False
    assert specs["rerun-jobs"].workers == consumer._DEFAULT_RERUN_WORKERS
    assert specs["cave-results"].url_env == "GRUG_CAVE_RESULTS_QUEUE_URL"
    assert specs["cave-results"].handler is cave_fallback.handle_fallback_result
    assert specs["cave-results"].delete_on_error is True
    assert specs["cave-results"].workers == 1


def test_rerun_worker_count_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("GRUG_RERUN_WORKERS", "7")
    assert consumer._rerun_workers() == 7
    monkeypatch.setenv("GRUG_RERUN_WORKERS", "999")
    assert consumer._rerun_workers() == consumer._MAX_RERUN_WORKERS
    monkeypatch.setenv("GRUG_RERUN_WORKERS", "0")
    assert consumer._rerun_workers() == 1
    monkeypatch.setenv("GRUG_RERUN_WORKERS", "invalid")
    assert consumer._rerun_workers() == consumer._DEFAULT_RERUN_WORKERS


def test_consumer_threads_expand_only_configured_queue_workers():
    rerun = _spec(False, lambda event: None, kind="rerun-jobs")
    rerun = consumer.QueueSpec(
        kind=rerun.kind,
        url_env=rerun.url_env,
        handler=rerun.handler,
        delete_on_error=rerun.delete_on_error,
        workers=3,
    )
    cave = _spec(True, lambda event: None, kind="cave-results")

    threads = consumer._consumer_threads([rerun, cave])

    assert [thread.name for thread in threads] == [
        "consume-rerun-jobs-1",
        "consume-rerun-jobs-2",
        "consume-rerun-jobs-3",
        "consume-cave-results",
    ]


def test_failed_delete_is_swallowed():
    """A delete failure re-delivers later; handlers are idempotent — the
    loop must not crash."""
    handler = MagicMock()
    with (
        patch.object(
            consumer._sqs, "receive_message", return_value={"Messages": [_MSG]}
        ),
        patch.object(consumer._sqs, "delete_message", side_effect=RuntimeError("gone")),
    ):
        assert consumer._poll_once(_spec(False, handler), "https://q", "arn") == 1


# --- startup dependency self-check (#405) -----------------------------------
# The consumer has no HTTP /readyz, so it must FAIL FAST at startup if its
# critical deps (SSM/KMS + Postgres) are unreachable - a broken AWS key would
# otherwise let the poll loop back off forever and leave the pod idle "Running".


def test_startup_check_exits_nonzero_when_deps_unreachable(monkeypatch):
    import readiness
    monkeypatch.setattr(
        readiness, "check_readiness",
        lambda: readiness.ReadinessReport(ready=False, deps={"ssm_kms": False, "postgres": True}),
    )
    with pytest.raises(SystemExit) as exc:
        consumer._startup_check()
    assert exc.value.code == 1


def test_startup_check_passes_when_deps_reachable(monkeypatch):
    import readiness
    monkeypatch.setattr(
        readiness, "check_readiness",
        lambda: readiness.ReadinessReport(ready=True, deps={"ssm_kms": True, "postgres": True}),
    )
    consumer._startup_check()  # must not raise


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_main_exits_nonzero_when_a_poll_thread_dies(monkeypatch):
    """Acceptance #1: a poll thread that dies mid-run takes the PROCESS down
    (non-zero exit -> kubelet restart), never a silent idle 'Running' pod.
    Exercises main()'s watchdog with a _consume that raises immediately."""
    import time

    consumer._stop.clear()
    monkeypatch.setattr(consumer, "_startup_check", lambda: None)
    monkeypatch.setattr(consumer, "_specs", lambda: [_spec(True, lambda e: None)])

    def _boom(_spec_arg):
        raise RuntimeError("poll thread died")

    monkeypatch.setattr(consumer, "_consume", _boom)
    # Don't burn the real 5s between watchdog checks; keep the loop tight.
    monkeypatch.setattr(consumer._stop, "wait", lambda _t=None: time.sleep(0.02) or False)

    try:
        with pytest.raises(SystemExit) as exc:
            consumer.main()
        assert exc.value.code == 1
    finally:
        consumer._stop.clear()


def test_watchdog_returns_work_and_hard_exits_for_an_expired_review(monkeypatch):
    """A live-but-stuck worker must take down the process at its deadline."""
    consumer._stop.clear()
    monkeypatch.setattr(consumer, "_warm_trace_writer", lambda: None)
    monkeypatch.setattr(consumer, "_startup_check", lambda: None)
    monkeypatch.setattr(consumer, "_specs", lambda: [_spec(True, lambda e: None)])
    monkeypatch.setattr(consumer, "_consume", lambda _spec: consumer._stop.wait())
    monkeypatch.setattr(consumer, "_flush_traces", lambda: None)
    expired = MagicMock(
        message_id="stuck-1", worker="consume-rerun-jobs-1",
        started_at=100.0, deadline_at=220.0,
    )
    monkeypatch.setattr(consumer, "_expired_review_jobs", lambda: [expired])
    calls: list[object] = []
    monkeypatch.setattr(
        consumer, "_release_inflight_reviews_bounded",
        lambda: calls.append("release") or (1, 1),
    )

    def hard_exit(code: int) -> None:
        calls.append(("exit", code))
        raise SystemExit(code)

    monkeypatch.setattr(consumer, "_hard_exit", hard_exit)
    try:
        with pytest.raises(SystemExit) as exc:
            consumer.main()
        assert exc.value.code == 1
        assert calls == ["release", ("exit", 1)]
    finally:
        consumer._stop.clear()


def test_watchdog_hard_exits_when_cleanup_raises(monkeypatch):
    consumer._stop.clear()
    monkeypatch.setattr(consumer, "_warm_trace_writer", lambda: None)
    monkeypatch.setattr(consumer, "_startup_check", lambda: None)
    monkeypatch.setattr(consumer, "_specs", lambda: [_spec(True, lambda e: None)])
    monkeypatch.setattr(consumer, "_consume", lambda _spec: consumer._stop.wait())
    monkeypatch.setattr(consumer, "_flush_traces", lambda: None)
    expired = MagicMock(
        message_id="stuck-1", worker="consume-rerun-jobs-1",
        started_at=100.0, deadline_at=220.0,
    )
    monkeypatch.setattr(consumer, "_expired_review_jobs", lambda: [expired])
    monkeypatch.setattr(
        consumer,
        "_release_inflight_reviews_bounded",
        MagicMock(side_effect=RuntimeError("cleanup failed")),
    )
    exits: list[int] = []

    def hard_exit(code: int) -> None:
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(consumer, "_hard_exit", hard_exit)
    try:
        with pytest.raises(SystemExit, match="1"):
            consumer.main()
    finally:
        consumer._stop.clear()
    assert exits == [1]


def test_shutdown_checks_stuck_workers_when_cleanup_raises(monkeypatch):
    consumer._stop.set()
    worker = MagicMock()
    worker.is_alive.return_value = True
    monkeypatch.setattr(consumer, "_warm_trace_writer", lambda: None)
    monkeypatch.setattr(consumer, "_startup_check", lambda: None)
    monkeypatch.setattr(consumer, "_consumer_threads", lambda _specs: [worker])
    monkeypatch.setattr(consumer, "_specs", lambda: [])
    monkeypatch.setattr(consumer.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        consumer,
        "_release_inflight_reviews_bounded",
        MagicMock(side_effect=RuntimeError("cleanup failed")),
    )
    checked: list[tuple[list[object], bool]] = []
    monkeypatch.setattr(
        consumer,
        "_exit_if_workers_remain",
        lambda threads, *, died: checked.append((threads, died)),
    )

    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            consumer.main()
    finally:
        consumer._stop.clear()
    assert checked == [([worker], False)]


def test_main_aborts_before_spawning_threads_when_startup_check_fails(monkeypatch):
    """Ordering guarantee (#405): when the startup self-check fails, main()
    exits non-zero and NEVER starts a poll thread. The check must GATE thread
    spawn, not run beside it - a regression that moved it after t.start() would
    reintroduce 'threads polling against a dead dependency'."""
    consumer._stop.clear()

    def _boom_startup():
        raise SystemExit(1)

    monkeypatch.setattr(consumer, "_startup_check", _boom_startup)
    thread_ctor = MagicMock()
    monkeypatch.setattr(consumer.threading, "Thread", thread_ctor)
    try:
        with pytest.raises(SystemExit) as exc:
            consumer.main()
        assert exc.value.code == 1
        thread_ctor.assert_not_called()
    finally:
        consumer._stop.clear()


def test_warm_trace_writer_never_raises():
    """#406: telemetry warmup must be fail-safe - it runs at consumer startup
    and must never crash the process, with or without ddtrace installed."""
    consumer._warm_trace_writer()  # must not raise


def test_main_warms_trace_writer_before_spawning_threads(monkeypatch):
    """#406: the ddtrace writer must be warmed on the MAIN thread BEFORE any
    poll thread is created - otherwise the first span (in a worker thread)
    fails to start the writer and all consumer spans are dropped."""
    consumer._stop.clear()
    order: list[str] = []
    monkeypatch.setattr(consumer, "_warm_trace_writer", lambda: order.append("warm"))
    # Abort right after warmup so no real threads spin up; the ordering is the
    # point. _startup_check raising proves warmup already ran.
    def _boom_startup():
        order.append("startup")
        raise SystemExit(1)

    monkeypatch.setattr(consumer, "_startup_check", _boom_startup)
    thread_ctor = MagicMock()
    monkeypatch.setattr(consumer.threading, "Thread", thread_ctor)
    try:
        with pytest.raises(SystemExit):
            consumer.main()
        assert order == ["warm", "startup"], order
        thread_ctor.assert_not_called()
    finally:
        consumer._stop.clear()


def test_flush_traces_never_raises():
    """#412: span-flush is fail-safe - it runs in the consumer's hot watchdog
    loop and must never crash it, with or without ddtrace installed."""
    consumer._flush_traces()  # must not raise


def test_flush_failure_is_visible_but_rate_limited(monkeypatch):
    """#412 audit: a REAL flush failure must NOT be silent (that's the
    zero-spans-and-blind state this slice kills) - log WARNING - but rate-limit
    so a sustained agent outage logs ~once/min, not on every 5s tick.

    Forces the failure by patching the `_flush_tracer` SEAM and asserts on a
    patched logger. Both deliberately avoid ddtrace internals AND caplog: the
    old approach (swap `ddtrace.tracer`, read `caplog`) was order/version/
    log-propagation dependent and flaked green-local / red-CI on hosted
    runners. This path is deterministic everywhere."""
    from unittest.mock import MagicMock

    # -inf = "never warned" (NOT 0.0: the rate-limit compares against
    # time.monotonic() = since-boot, which on a fresh runner can be < the
    # interval, suppressing the first warning -> the real green-local/red-CI
    # flake this fixes).
    monkeypatch.setattr(consumer, "_last_flush_warn", float("-inf"))
    monkeypatch.setattr(
        consumer,
        "_flush_tracer",
        MagicMock(side_effect=RuntimeError("agent unreachable")),
    )
    mock_log = MagicMock()
    monkeypatch.setattr(consumer, "log", mock_log)

    consumer._flush_traces()  # first failure -> warns
    consumer._flush_traces()  # immediate retry -> rate-limited, no second warn

    warns = [
        c for c in mock_log.warning.call_args_list
        if c.args and c.args[0] == "trace_flush_failed"
    ]
    assert len(warns) == 1, "real flush failure must warn exactly once (rate-limited)"
    assert warns[0].kwargs.get("exc_info") is True


def test_watchdog_flushes_traces_each_tick(monkeypatch):
    """#412: the main-thread watchdog flushes buffered worker-thread APM spans
    via the main thread (the path proven to deliver) on each healthy tick."""
    import threading as _threading
    import time

    consumer._stop.clear()
    monkeypatch.setattr(consumer, "_warm_trace_writer", lambda: None)
    monkeypatch.setattr(consumer, "_startup_check", lambda: None)
    monkeypatch.setattr(consumer, "_specs", lambda: [_spec(True, lambda e: None)])
    monkeypatch.setattr(consumer, "_consume", lambda _spec: consumer._stop.wait())
    flushes: list[int] = []
    monkeypatch.setattr(consumer, "_flush_traces", lambda: flushes.append(1))
    handlers: dict = {}
    monkeypatch.setattr(
        consumer.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn)
    )

    def _term_soon():
        time.sleep(0.05)
        handlers[consumer.signal.SIGTERM](consumer.signal.SIGTERM, None)

    _threading.Thread(target=_term_soon, daemon=True).start()
    try:
        consumer.main()
        assert len(flushes) >= 1  # flushed on at least the healthy tick(s)
    finally:
        consumer._stop.clear()


def test_main_returns_zero_on_graceful_signal_shutdown(monkeypatch):
    """A real SIGTERM (graceful scale-down) must return 0, NOT a non-zero exit
    - otherwise every normal pod termination would read as a crash. Guards the
    watchdog's signal-vs-death distinction from regressing to 'always died'."""
    import threading as _threading
    import time

    consumer._stop.clear()
    monkeypatch.setattr(consumer, "_startup_check", lambda: None)
    monkeypatch.setattr(consumer, "_specs", lambda: [_spec(True, lambda e: None)])

    def _quiet(_spec_arg):
        # The real _consume contract: run until _stop is set, then return.
        consumer._stop.wait()

    monkeypatch.setattr(consumer, "_consume", _quiet)

    # Capture the SIGTERM handler main() registers without touching real signal
    # disposition, then invoke it from a helper thread = a graceful term.
    handlers: dict = {}
    monkeypatch.setattr(
        consumer.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn)
    )

    def _term_soon():
        time.sleep(0.05)
        handlers[consumer.signal.SIGTERM](consumer.signal.SIGTERM, None)

    _threading.Thread(target=_term_soon, daemon=True).start()
    try:
        consumer.main()  # returns normally (exit 0); must NOT raise SystemExit
    finally:
        consumer._stop.clear()


def test_startup_check_runs_identity_proof_before_readiness(monkeypatch):
    """#389 audit stage-7 (mirror of the #388 poller pin): the consumer's
    proof call site is the FIRST thing _startup_check does - deleting it
    (or demoting it below the readiness probe) must be a red test, not a
    silent un-proving with Recreate-strategy blast radius."""
    import aws_identity
    import pytest
    import readiness

    import consumer

    def _boom():
        raise RuntimeError("proof ran")

    monkeypatch.setattr(aws_identity, "prove_roles_anywhere_identity", _boom)
    monkeypatch.setattr(
        readiness, "check_readiness",
        lambda: pytest.fail("readiness probed before the proof"),
    )
    with pytest.raises(RuntimeError, match="proof ran"):
        consumer._startup_check()



# --- #379: owned queue-depth telemetry -------------------------------


_RERUN_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/grug-rerun-jobs.fifo"
_BASE = "https://sqs.us-east-1.amazonaws.com/123456789012"
_BOTH_ATTRS = {"ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"}


@pytest.fixture
def telemetry_env(monkeypatch):
    """Baseline env for a sweep: known queue URL + agent host present."""
    monkeypatch.setenv("GRUG_RERUN_QUEUE_URL", _RERUN_URL)
    monkeypatch.setenv("DD_AGENT_HOST", "10.0.0.99")
    return _BASE


@pytest.fixture
def captured_gauges(monkeypatch):
    """Capture observability.emit_gauge calls as (metric, value, tags)."""
    emitted = []
    monkeypatch.setattr(
        "observability.emit_gauge",
        lambda metric, value, tags=None: emitted.append((metric, value, tags)),
    )
    return emitted


def test_telemetry_base_url_derived_from_known_queue_env(monkeypatch):
    monkeypatch.setenv("GRUG_RERUN_QUEUE_URL", _RERUN_URL)
    assert consumer._telemetry_base_url() == _BASE


def test_telemetry_base_url_falls_back_to_cave_results(monkeypatch):
    monkeypatch.delenv("GRUG_RERUN_QUEUE_URL", raising=False)
    monkeypatch.setenv(
        "GRUG_CAVE_RESULTS_QUEUE_URL", f"{_BASE}/grug-cave-results.fifo",
    )
    assert consumer._telemetry_base_url() == _BASE


def test_telemetry_base_url_none_without_env_or_on_malformed(monkeypatch):
    monkeypatch.delenv("GRUG_RERUN_QUEUE_URL", raising=False)
    monkeypatch.delenv("GRUG_CAVE_RESULTS_QUEUE_URL", raising=False)
    assert consumer._telemetry_base_url() is None
    # No path segment to strip -> refuse rather than derive garbage.
    monkeypatch.setenv("GRUG_RERUN_QUEUE_URL", "no-slashes-here")
    assert consumer._telemetry_base_url() is None


def test_telemetry_interval_clamped_and_never_raises(monkeypatch):
    monkeypatch.setenv("GRUG_QUEUE_TELEMETRY_INTERVAL_S", "not-a-number")
    assert consumer._telemetry_interval_s() == 60.0
    monkeypatch.setenv("GRUG_QUEUE_TELEMETRY_INTERVAL_S", "0")
    assert consumer._telemetry_interval_s() == 10.0   # floor: no hot loop
    monkeypatch.setenv("GRUG_QUEUE_TELEMETRY_INTERVAL_S", "1800")
    assert consumer._telemetry_interval_s() == 300.0  # cap: < the 15m window
    monkeypatch.setenv("GRUG_QUEUE_TELEMETRY_INTERVAL_S", "45")
    assert consumer._telemetry_interval_s() == 45.0


def test_emit_queue_depth_emits_both_gauges_per_queue(telemetry_env, captured_gauges):
    """Every telemetry queue gets messages_visible + messages_not_visible
    gauges tagged with its exact name, requesting exactly the two
    attributes (real SQS returns only what is requested - a dropped
    AttributeName would silently emit fake zeros forever)."""
    with patch.object(
        consumer._sqs_telemetry,
        "get_queue_attributes",
        return_value={"Attributes": {
            "ApproximateNumberOfMessages": "2",
            "ApproximateNumberOfMessagesNotVisible": "1",
        }},
    ) as mock_attrs:
        n = consumer._emit_queue_depth_once()
    assert n == len(consumer._TELEMETRY_QUEUE_NAMES)
    urls = [c.kwargs["QueueUrl"] for c in mock_attrs.call_args_list]
    assert urls == [f"{_BASE}/{name}" for name in consumer._TELEMETRY_QUEUE_NAMES]
    assert all(
        set(c.kwargs["AttributeNames"]) == _BOTH_ATTRS
        for c in mock_attrs.call_args_list
    )
    visible = [(t or {}).get("queue") for m, v, t in captured_gauges
               if m == "grug.sqs.messages_visible"]
    assert visible == list(consumer._TELEMETRY_QUEUE_NAMES)
    assert all(v == 2.0 for m, v, t in captured_gauges
               if m == "grug.sqs.messages_visible")
    assert all(v == 1.0 for m, v, t in captured_gauges
               if m == "grug.sqs.messages_not_visible")


def test_emit_queue_depth_emits_per_queue_ok_boolean(telemetry_env, captured_gauges):
    """Every queue emits a 1/0 telemetry_queue_ok boolean per sweep - the
    per-queue heartbeat monitor input (audit stage-2 HIGH + codex peer
    review: partial telemetry death, sustained OR intermittent, must page
    by queue name, not silently re-blind the depth monitors)."""
    with patch.object(
        consumer._sqs_telemetry, "get_queue_attributes",
        return_value={"Attributes": {"ApproximateNumberOfMessages": "0",
                                     "ApproximateNumberOfMessagesNotVisible": "0"}},
    ):
        consumer._emit_queue_depth_once()
    ok = [((t or {}).get("queue"), v) for m, v, t in captured_gauges
          if m == "grug.sqs.telemetry_queue_ok"]
    assert ok == [(name, 1.0) for name in consumer._TELEMETRY_QUEUE_NAMES]


def test_emit_queue_depth_per_queue_best_effort(telemetry_env, captured_gauges, caplog):
    """One queue's failure (real botocore ClientError shape, e.g.
    AccessDenied before the infra IAM grant lands) logs the WIRE error
    code and the OTHER queues still emit; the health gauge reports the
    reduced count."""
    import logging as _logging

    from botocore.exceptions import ClientError

    def _attrs(QueueUrl, AttributeNames):
        if "dlq" in QueueUrl:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "GetQueueAttributes",
            )
        return {"Attributes": {"ApproximateNumberOfMessages": "0",
                               "ApproximateNumberOfMessagesNotVisible": "0"}}

    with patch.object(consumer._sqs_telemetry, "get_queue_attributes", side_effect=_attrs):
        with caplog.at_level(_logging.WARNING):
            n = consumer._emit_queue_depth_once()
    assert n == 3  # the three non-DLQ queues probed
    queues = [(t or {}).get("queue") for m, v, t in captured_gauges
              if m == "grug.sqs.messages_visible"]
    assert queues and not any("dlq" in q for q in queues)
    fails = [r for r in caplog.records if r.msg == "queue_depth_probe_failed"]
    assert len(fails) == 3
    assert all(r.code == "AccessDenied" for r in fails)
    ok = {(t or {}).get("queue"): v for m, v, t in captured_gauges
          if m == "grug.sqs.telemetry_queue_ok"}
    assert len(ok) == len(consumer._TELEMETRY_QUEUE_NAMES)
    assert all(v == 0.0 for q, v in ok.items() if "dlq" in q)
    assert all(v == 1.0 for q, v in ok.items() if "dlq" not in q)


def test_emit_queue_depth_non_clienterror_logs_kind_without_code(
    telemetry_env, captured_gauges, caplog,
):
    import logging as _logging

    with patch.object(
        consumer._sqs_telemetry, "get_queue_attributes", side_effect=RuntimeError("boom"),
    ):
        with caplog.at_level(_logging.WARNING):
            n = consumer._emit_queue_depth_once()
    assert n == 0
    fails = [r for r in caplog.records if r.msg == "queue_depth_probe_failed"]
    assert fails and all(r.kind == "RuntimeError" and r.code is None for r in fails)


def test_emit_queue_depth_missing_attributes_key_is_probe_failure(
    telemetry_env, captured_gauges, caplog,
):
    """A response without Attributes must count as a FAILED probe (never
    silently emit fake zeros) - pins the ["Attributes"] KeyError path."""
    import logging as _logging

    with patch.object(consumer._sqs_telemetry, "get_queue_attributes", return_value={}):
        with caplog.at_level(_logging.WARNING):
            n = consumer._emit_queue_depth_once()
    assert n == 0
    assert not any(m == "grug.sqs.messages_visible" for m, v, t in captured_gauges)
    assert sum(1 for r in caplog.records if r.msg == "queue_depth_probe_failed") == 6


def test_emit_queue_depth_skips_sweep_without_agent_host(monkeypatch, caplog):
    """Sweep-level guard: no DD_AGENT_HOST -> one warning per sweep (not
    12 per-emit lines) and zero pointless SQS calls."""
    import logging as _logging

    monkeypatch.setenv("GRUG_RERUN_QUEUE_URL", _RERUN_URL)
    monkeypatch.delenv("DD_AGENT_HOST", raising=False)
    with patch.object(consumer._sqs_telemetry, "get_queue_attributes") as mock_attrs:
        with caplog.at_level(_logging.WARNING):
            assert consumer._emit_queue_depth_once() == 0
    assert mock_attrs.call_count == 0
    assert sum(1 for r in caplog.records
               if r.msg == "queue_telemetry_no_agent_host") == 1


def test_emit_queue_depth_warns_without_base_url(monkeypatch, caplog):
    import logging as _logging

    monkeypatch.setenv("DD_AGENT_HOST", "10.0.0.99")
    monkeypatch.delenv("GRUG_RERUN_QUEUE_URL", raising=False)
    monkeypatch.delenv("GRUG_CAVE_RESULTS_QUEUE_URL", raising=False)
    with caplog.at_level(_logging.WARNING):
        assert consumer._emit_queue_depth_once() == 0
    assert any(r.msg == "queue_telemetry_no_base_url" for r in caplog.records)


def test_telemetry_loop_exits_on_stop_and_survives_cycle_failure(monkeypatch):
    """The loop is belt-and-suspenders never-raise, and honors _stop."""
    calls = {"n": 0}

    def _cycle():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first cycle blows up")
        consumer._stop.set()

    monkeypatch.setattr(consumer, "_emit_queue_depth_once", _cycle)
    monkeypatch.setattr(consumer, "_TELEMETRY_INTERVAL_S", 0.01)
    consumer._stop.clear()
    try:
        consumer._telemetry_loop()   # returns only via _stop
    finally:
        consumer._stop.clear()
    assert calls["n"] == 2


def test_main_starts_daemon_telemetry_thread_outside_watchdog(monkeypatch):
    """The design's safety wiring, pinned (audit stage-7): the telemetry
    thread IS started, IS daemon, and is NOT watchdog-tracked - its death
    must surface as monitor No Data, never as a pod restart. main() must
    exit 0 on graceful shutdown even though the telemetry thread died."""
    built = []
    real_thread = consumer.threading.Thread

    class _RecordingThread(real_thread):
        def __init__(self, *a, **k):
            built.append(k)
            super().__init__(*a, **k)

    monkeypatch.setattr(consumer.threading, "Thread", _RecordingThread)
    monkeypatch.setattr(consumer, "_warm_trace_writer", lambda: None)
    monkeypatch.setattr(consumer, "_startup_check", lambda: None)
    monkeypatch.setattr(consumer, "_flush_traces", lambda: None)
    monkeypatch.setattr(consumer, "_flush_tracer", lambda: None)
    # A clean quick-return (NOT a raise): the safety property - telemetry
    # death is non-fatal to main() - is proven STRUCTURALLY below (the
    # thread is daemon + NOT in the watchdog `threads` list, so main()
    # never gates on it). Raising in the daemon thread made pytest's
    # thread-exception capture fail the test non-deterministically by
    # timing (green locally, red in CI).
    monkeypatch.setattr(consumer, "_telemetry_loop", lambda: None)
    monkeypatch.setattr(
        consumer, "_specs",
        lambda: [consumer.QueueSpec(
            kind="t", url_env="TEST_QUEUE_URL",
            handler=lambda e: None, delete_on_error=True,
        )],
    )
    monkeypatch.setenv("TEST_QUEUE_URL", f"{_BASE}/t.fifo")
    monkeypatch.setattr(consumer, "_queue_arn", lambda url: "arn:aws:sqs:t")
    monkeypatch.setattr(consumer, "_poll_once", lambda *a: consumer._stop.wait(0.01))

    captured_sig = {}

    def _fake_signal(signum, handler):
        captured_sig[signum] = handler

    monkeypatch.setattr(consumer.signal, "signal", _fake_signal)
    consumer._stop.clear()

    import threading as _threading

    def _send_sigterm():
        import time as _time
        _time.sleep(0.1)
        captured_sig[consumer.signal.SIGTERM](consumer.signal.SIGTERM, None)

    killer = _threading.Thread(target=_send_sigterm)
    killer.start()
    try:
        consumer.main()   # must NOT raise SystemExit: telemetry death is non-fatal
    finally:
        killer.join()
        consumer._stop.clear()

    telemetry = [k for k in built if k.get("target") is not None
                 and getattr(k.get("target"), "__name__", "") == "<lambda>"
                 and k.get("name") == "queue-telemetry"]
    assert telemetry and telemetry[0].get("daemon") is True
