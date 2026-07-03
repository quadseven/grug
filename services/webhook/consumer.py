# WEBHOOK-ONLY (NOT mirrored): the k8s SQS consumer. The api service
# produces to these queues but never consumes — per ADR-0001 only modules
# BOTH services run are mirrored.
"""SQS consumers for the k8s runtime (#368).

This entrypoint long-polls `grug-cave-results.fifo` and
`grug-rerun-jobs.fifo` (one thread each) and feeds each message to its
per-queue handler, wrapped in the
SAME `Records` event shape — the handlers and their tests are untouched.

Delivery semantics preserved per queue (mirrors the ESM config):

- **rerun-jobs** (`rerun.handle_rerun_jobs`): the handler RAISES on a
  failed re-run. We then do NOT delete — the message reappears after the
  queue's 420s visibility timeout and redrives to the DLQ after
  maxReceiveCount=3, exactly like the ESM retry path.
- **cave-results** (`cave_fallback.handle_fallback_result`): the handler
  never raises (logs + skips bad records), so every received message is
  deleted. A defensive catch still deletes on an unexpected raise — the
  ESM equivalent (retrying a poison result message) would just re-log
  three times and DLQ it with no heal.

Batch size is 1 (matches both ESMs), so one raise re-drives exactly one
message and FIFO group ordering is preserved within each queue.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import boto3

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.consumer")

_sqs = boto3.client("sqs")

# Receive errors back off so a misconfigured queue URL or an IAM gap
# logs at a readable rate instead of hot-looping the pod CPU.
_RECEIVE_ERROR_BACKOFF_S = 30.0

_stop = threading.Event()


@dataclass(frozen=True, slots=True)
class QueueSpec:
    """One consumed queue: where to poll, what handles it, and whether a
    handler raise should still delete (False → redrive via visibility
    timeout, the rerun DLQ contract)."""

    kind: str
    url_env: str
    handler: Callable[[dict[str, Any]], Any]
    delete_on_error: bool


def _specs() -> list[QueueSpec]:
    """Build the queue table. Imported lazily so the consumer process
    only loads each handler's dependency graph once, and tests can patch
    the handler modules before the table is built."""
    from cave_fallback import handle_fallback_result
    from rerun import handle_rerun_jobs

    return [
        QueueSpec(
            kind="rerun-jobs",
            url_env="GRUG_RERUN_QUEUE_URL",
            handler=handle_rerun_jobs,
            delete_on_error=False,
        ),
        QueueSpec(
            kind="cave-results",
            url_env="GRUG_CAVE_RESULTS_QUEUE_URL",
            handler=handle_fallback_result,
            delete_on_error=True,
        ),
    ]


def _queue_arn(queue_url: str) -> str:
    """Resolve the queue ARN once at startup — the handlers' routing and
    log context expect the ESM's `eventSourceARN` field to be real."""
    attrs = _sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    return attrs["Attributes"]["QueueArn"]


def _esm_event(arn: str, message: dict[str, Any]) -> dict[str, Any]:
    """Wrap one raw `receive_message` message in the event-source-mapping
    batch shape the handlers were written against (batch of 1)."""
    return {
        "Records": [
            {
                "eventSource": "aws:sqs",
                "eventSourceARN": arn,
                "messageId": message.get("MessageId", ""),
                "body": message.get("Body", ""),
                "attributes": message.get("Attributes", {}),
            }
        ]
    }


def _poll_once(spec: QueueSpec, queue_url: str, arn: str) -> int:
    """One receive → dispatch → delete cycle. Returns the number of
    messages handled (0 or 1). Never raises: receive errors back off,
    handler raises follow the spec's delete policy."""
    try:
        resp = _sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            AttributeNames=["All"],
        )
    except Exception as e:  # noqa: BLE001 — keep the loop alive; backoff logs the cause
        log.error(
            "consumer_receive_error",
            extra={"queue": spec.kind, "kind": type(e).__name__},
        )
        _stop.wait(_RECEIVE_ERROR_BACKOFF_S)
        return 0

    messages = resp.get("Messages", [])
    if not messages:
        return 0
    message = messages[0]
    receipt = message.get("ReceiptHandle", "")
    delete = True
    try:
        spec.handler(_esm_event(arn, message))
    except Exception as e:  # noqa: BLE001 — the rerun contract REQUIRES surviving a raise
        delete = spec.delete_on_error
        log.warning(
            "consumer_handler_raised",
            extra={
                "queue": spec.kind,
                "kind": type(e).__name__,
                "message_id": message.get("MessageId", ""),
                "redrive": not delete,
            },
        )
    if delete and receipt:
        try:
            _sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
        except Exception as e:  # noqa: BLE001 — a failed delete just re-delivers; idempotent handlers
            log.warning(
                "consumer_delete_failed",
                extra={"queue": spec.kind, "kind": type(e).__name__},
            )
    return 1


def _consume(spec: QueueSpec) -> None:
    """Thread body: poll one queue until shutdown."""
    queue_url = os.environ[spec.url_env]
    arn = _queue_arn(queue_url)
    log.info("consumer_started", extra={"queue": spec.kind, "arn": arn})
    while not _stop.is_set():
        _poll_once(spec, queue_url, arn)
    log.info("consumer_stopped", extra={"queue": spec.kind})


def _warm_trace_writer() -> None:
    """Open a ddtrace span on the MAIN thread at startup (#406).

    Observed: grug-consumer emits ZERO APM spans while grug-webhook (same image,
    same node-local agent) traces fine, and the pre-#405 consumer logged
    'ddtrace ... failed to start writer service' from a botocore SQS call on a
    poll thread. The spans that matter (per-message work) are created on those
    worker poll threads, where ddtrace's lazy writer-service start can fail and
    silently drop every span. Forcing one span on the MAIN thread before the
    poll threads spawn initializes the writer in the main-thread context as a
    mitigation. (This is belt-and-suspenders with #405's startup boto3 call,
    which is also main-thread; efficacy is confirmed by checking that
    grug-consumer spans actually appear in Datadog AFTER deploy - if they still
    don't, the cause is elsewhere and tracked separately, not assumed fixed.)

    Fail-safe: telemetry must NEVER break consumer startup, so warmup errors
    are swallowed - but at the RIGHT level. ddtrace genuinely absent (tests) is
    expected -> debug; ddtrace present but warmup raised (typically the
    writer-service start failure) -> warning (visible at the default level), so
    a regression to zero-spans is not itself silent."""
    try:
        import ddtrace
    except ImportError:
        log.debug("trace_writer_warmup_skipped_no_ddtrace")
        return
    try:
        with ddtrace.tracer.trace("grug.consumer.startup"):
            pass
    except Exception:  # noqa: BLE001 - telemetry is never worth crashing on
        log.warning("trace_writer_warmup_failed", exc_info=True)


# -inf, NOT 0.0: the rate-limit compares against time.monotonic() (seconds
# since boot, NOT epoch). On a freshly-booted pod monotonic() can be < the
# interval, so a 0.0 sentinel makes `now - 0.0 > INTERVAL` False and SILENTLY
# DROPS the first flush warning in the first 60s after start - exactly when
# startup trouble matters. -inf means the first real failure always warns.
_last_flush_warn = float("-inf")
_FLUSH_WARN_INTERVAL_S = 60.0


def _flush_traces() -> None:
    """Flush buffered APM spans from the MAIN thread (#412).

    The consumer's per-poll botocore SQS spans are created on the worker poll
    threads. In prod those weren't reaching Datadog while the main-thread
    startup span did - and a local repro under the same ddtrace (3.19.7) could
    NOT reproduce a generic worker-thread bug (worker spans flush fine there),
    so the prod cause is environmental, not a code bug in span creation.
    `tracer.flush()` on the MAIN thread (the path PROVEN to deliver - the
    startup span arrives) drains the shared span buffer each watchdog tick, so
    buffered worker-thread spans get delivered regardless of whatever stalls
    the long-running periodic writer in the threaded consumer.

    `tracer.flush()` is SYNCHRONOUS: when the trace agent is unreachable it
    blocks for the ddtrace agent timeout (measured ~1.5s; pinned via
    DD_TRACE_AGENT_TIMEOUT_SECONDS in the deploy), so it adds at most that to
    the watchdog's ~5s dead-thread detection cadence - bounded and tolerable.

    Fail-safe: telemetry must never break the consumer hot loop. ddtrace absent
    (tests) is expected -> debug; a real recurring flush failure means we are
    back in the zero-spans state #412 exists to kill, so surface it at WARNING -
    but rate-limited (this runs every ~5s) so a sustained agent outage logs
    ~once/min, not 12x/min and not never."""
    global _last_flush_warn
    try:
        _flush_tracer()
    except Exception:  # noqa: BLE001 - telemetry is never worth crashing on
        now = time.monotonic()
        if now - _last_flush_warn > _FLUSH_WARN_INTERVAL_S:
            log.warning("trace_flush_failed", exc_info=True)
            _last_flush_warn = now


def _flush_tracer() -> None:
    """Flush the global APM tracer once (the testable seam for #412).

    ddtrace absent (tests run without it) is a no-op. Isolated from
    `_flush_traces` so a test can force a flush FAILURE deterministically by
    patching THIS function. Patching `ddtrace.tracer` (or its `.flush`)
    directly is order/version dependent and silently no-ops on some hosted
    runners - it passed locally and flaked red on CI, which is what this seam
    retires. grug-local: this is a webhook-service test seam, NOT a reusable /
    CI-workflow concern."""
    try:
        import ddtrace
    except ImportError:
        log.debug("trace_flush_skipped_no_ddtrace")
        return
    ddtrace.tracer.flush()


def _startup_check() -> None:
    """Fail FAST (non-zero exit) if a critical dependency is unreachable at
    startup. The consumer has no HTTP /readyz for the kubelet to gate on, so
    without this a pod with broken AWS credentials (the 2026-06-14 deleted-key
    incident) would start its poll threads, hit the receive-error backoff
    forever, and sit idle "Running" while its queues silently back up. A
    non-zero exit surfaces as a visible CrashLoopBackOff instead.

    Reuses the SAME dependency-health module the /readyz handlers use
    (`readiness.check_readiness`) so there is one definition of "is AWS
    reachable", not a second drifting copy. Imported lazily so the check picks
    up test monkeypatches and the module's boto3 client is built only when the
    consumer actually runs."""
    from readiness import check_readiness

    rep = check_readiness()
    # Independent fail-CLOSED guard, not just `rep.ready`: an empty/degenerate
    # report reads as healthy under all([])==True, which is exactly the
    # "false-healthy" the gate exists to refuse. Start ONLY on an explicit,
    # non-empty, every-dep-up report.
    if not rep.deps or not all(rep.deps.values()):
        log.error("consumer_startup_check_failed", extra={"deps": rep.deps})
        raise SystemExit(1)
    log.info("consumer_startup_check_passed", extra={"deps": rep.deps})


def main() -> None:
    logging.basicConfig(level=os.getenv("GRUG_LOG_LEVEL", "INFO"))

    # Initialize the ddtrace writer on the MAIN thread BEFORE any poll thread
    # creates a (worker-thread) span (#406) - otherwise all consumer APM spans
    # are silently dropped.
    _warm_trace_writer()

    # Gate startup on critical deps BEFORE spawning poll threads (#405).
    _startup_check()

    # Records that _stop was set by a SIGNAL (graceful shutdown), to tell a
    # deliberate stop apart from a thread death. _consume only ever returns
    # once _stop is set, so any non-alive thread while _stop is clear is a
    # death — but a SIGTERM landing in the same instant as a death would
    # otherwise let the loop exit with died=False. The post-loop ground-truth
    # check below uses this flag to refuse to mask a death.
    terminated_by_signal = threading.Event()

    def _terminate(signum: int, _frame: Any) -> None:
        log.info("consumer_terminating", extra={"signal": signum})
        terminated_by_signal.set()
        _stop.set()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    threads = [
        threading.Thread(target=_consume, args=(spec,), name=f"consume-{spec.kind}")
        for spec in _specs()
    ]
    for t in threads:
        t.start()
    # Watchdog: there is no HTTP probe on this pod, so a consumer thread
    # that dies (missing env, ARN resolve failure, anything escaping
    # _consume) must take the PROCESS down — a half-dead consumer would
    # otherwise silently strand its queue. Exit nonzero → kubelet
    # restarts the pod (CrashLoopBackOff paces a persistent failure).
    died = False
    while not _stop.is_set():
        if not all(t.is_alive() for t in threads):
            died = True
            log.error(
                "consumer_thread_died",
                extra={"threads": [t.name for t in threads if not t.is_alive()]},
            )
            _stop.set()
            break
        # Deliver buffered worker-thread APM spans via the main thread (#412).
        _flush_traces()
        _stop.wait(5.0)
    # Ground-truth re-check: if _stop was set by anything OTHER than a signal
    # (i.e. not a graceful shutdown) and a thread is down, that's a death —
    # even if a concurrent SIGTERM raced the loop condition and skipped the
    # branch above. Never let a dead thread exit 0.
    if not terminated_by_signal.is_set() and not all(t.is_alive() for t in threads):
        died = True
    # Long-poll wait is 20s, so threads notice _stop well inside the pod's
    # default 30s terminationGracePeriod; in-flight handlers finish first.
    for t in threads:
        t.join(timeout=30.0)
    if died:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
