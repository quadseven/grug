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

from unittest.mock import MagicMock, patch

import pytest

import consumer


def _spec(delete_on_error: bool, handler) -> consumer.QueueSpec:
    return consumer.QueueSpec(
        kind="test-queue",
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
    """The rerun contract: a raise must NOT delete — the message reappears
    after the visibility timeout and redrives to the DLQ after 3 receives,
    exactly like the Lambda ESM retry path."""
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
    assert specs["cave-results"].url_env == "GRUG_CAVE_RESULTS_QUEUE_URL"
    assert specs["cave-results"].handler is cave_fallback.handle_fallback_result
    assert specs["cave-results"].delete_on_error is True


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


def test_flush_failure_is_visible_but_rate_limited(monkeypatch, caplog):
    """#412 audit: a REAL flush failure must NOT be silent (that's the
    zero-spans-and-blind state this slice kills) - log WARNING - but rate-limit
    so a sustained agent outage logs ~once/min, not on every 5s tick."""
    import logging
    import types

    import ddtrace

    monkeypatch.setattr(consumer, "_last_flush_warn", 0.0)

    def _boom():
        raise RuntimeError("agent unreachable")

    # Replace the WHOLE tracer object, not just `.flush` — under ddtrace's
    # proxy/lazy tracer, patching the `.flush` attribute is order/version
    # dependent and silently no-ops on some hosted runners (the real flush
    # then succeeds and nothing warns -> a deterministic CI failure that
    # cannot reproduce locally). `_flush_traces` only calls `ddtrace.tracer
    # .flush()`, so a SimpleNamespace stands in deterministically.
    monkeypatch.setattr(ddtrace, "tracer", types.SimpleNamespace(flush=_boom))
    with caplog.at_level(logging.WARNING):
        consumer._flush_traces()  # first failure -> warns
        consumer._flush_traces()  # immediate retry -> rate-limited, no second warn
    warns = [r for r in caplog.records if r.msg == "trace_flush_failed"]
    assert len(warns) == 1, "real flush failure must warn exactly once (rate-limited)"
    assert warns[0].levelno == logging.WARNING


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
