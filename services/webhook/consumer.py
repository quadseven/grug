# WEBHOOK-ONLY (NOT mirrored): the k8s SQS consumer. The api service
# produces to these queues but never consumes — per ADR-0001 only modules
# BOTH services run are mirrored.
"""SQS consumers for the k8s runtime (#368).

This entrypoint long-polls `grug-cave-results.fifo` and
`grug-rerun-jobs.fifo` (one cave thread plus a bounded rerun worker pool) and
feeds each message to its
per-queue handler, wrapped in the
SAME `Records` event shape — the handlers and their tests are untouched.

Delivery semantics preserved per queue (mirrors the ESM config):

- **rerun-jobs** (`rerun.handle_rerun_jobs`): the handler RAISES on a
  failed re-run. We then do NOT delete — the message reappears after the
  queue's 900s fallback visibility timeout and redrives to the DLQ after
  maxReceiveCount=5 (raised from 3 for deploy-restart headroom, #607),
  exactly like the ESM retry path.
- **cave-results** (`cave_fallback.handle_fallback_result`): the handler
  never raises (logs + skips bad records), so every received message is
  deleted. A defensive catch still deletes on an unexpected raise — the
  ESM equivalent (retrying a poison result message) would just re-log
  three times and DLQ it with no heal.

Batch size is 1 (matches both ESMs), so one raise re-drives exactly one
message and FIFO group ordering is preserved within each queue.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import boto3
from botocore.config import Config as _BotoConfig

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.consumer")

_sqs = boto3.client("sqs")

# Telemetry gets its OWN client with tight timeouts (poolside peer review,
# PR #516): the default ~60s connect/read timeouts could wedge the
# telemetry thread for minutes on an SQS brownout (6 sequential probes),
# skewing the sweep cadence. Depth probes are disposable - fail fast, log,
# and let the next sweep retry. The consume paths keep the default client:
# a long-poll receive SHOULD wait.
_sqs_telemetry = boto3.client(
    "sqs",
    config=_BotoConfig(
        connect_timeout=5, read_timeout=10, retries={"max_attempts": 2},
    ),
)

# Receive errors back off so a misconfigured queue URL or an IAM gap
# logs at a readable rate instead of hot-looping the pod CPU.
_RECEIVE_ERROR_BACKOFF_S = 30.0

# A deep Elder pass can outlive the queue's base visibility timeout. Review
# messages therefore receive a renewable, per-message lease. The queue-level
# timeout remains a fallback for a deploy race where IAM has not gained
# ChangeMessageVisibility yet; normal rerun/ask messages keep that base lease.
_REVIEW_VISIBILITY_TIMEOUT_S = 600
_REVIEW_VISIBILITY_HEARTBEAT_S = 120.0
_DEFAULT_RERUN_WORKERS = 4
_MAX_RERUN_WORKERS = 16

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
    workers: int = 1


@dataclass(frozen=True, slots=True)
class _VisibilityHeartbeat:
    stop: threading.Event
    thread: threading.Thread


def _rerun_workers() -> int:
    """Bounded worker count so long reviews do not globally block the FIFO.

    SQS still serializes each MessageGroupId. Review producers use a per-PR
    group while operator asks/reruns retain their own groups, so unrelated work
    can proceed without sacrificing ordering for one PR.
    """
    raw = os.getenv("GRUG_RERUN_WORKERS", str(_DEFAULT_RERUN_WORKERS))
    try:
        value = int(raw)
    except ValueError:
        log.warning("consumer_rerun_workers_invalid", extra={"value": raw})
        return _DEFAULT_RERUN_WORKERS
    return min(_MAX_RERUN_WORKERS, max(1, value))


def _specs() -> list[QueueSpec]:
    """Build the queue table. Imported lazily so the consumer process
    only loads each handler's dependency graph once, and tests can patch
    the handler modules before the table is built."""
    from cave_fallback import handle_fallback_result
    from rerun import handle_rerun_jobs  # type: ignore[attr-defined]

    return [
        QueueSpec(
            kind="rerun-jobs",
            url_env="GRUG_RERUN_QUEUE_URL",
            handler=handle_rerun_jobs,
            delete_on_error=False,
            workers=_rerun_workers(),
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


def _is_durable_review(spec: QueueSpec, message: dict[str, Any]) -> bool:
    """Return whether this message owns a potentially long Elder pass."""
    if spec.kind != "rerun-jobs":
        return False
    try:
        body = json.loads(str(message.get("Body", "")))
    except json.JSONDecodeError:
        return False
    return isinstance(body, dict) and body.get("kind") == "review"


def _extend_review_visibility(
    queue_url: str, receipt: str, message_id: str,
) -> bool:
    try:
        _sqs.change_message_visibility(
            QueueUrl=queue_url,
            ReceiptHandle=receipt,
            VisibilityTimeout=_REVIEW_VISIBILITY_TIMEOUT_S,
        )
        return True
    except Exception as e:  # noqa: BLE001 - queue base visibility remains fallback
        log.warning(
            "consumer_review_visibility_heartbeat_failed",
            extra={"message_id": message_id, "kind": type(e).__name__},
        )
        return False


def _review_visibility_loop(
    queue_url: str,
    receipt: str,
    message_id: str,
    stop: threading.Event,
) -> None:
    while not stop.wait(_REVIEW_VISIBILITY_HEARTBEAT_S):
        _extend_review_visibility(queue_url, receipt, message_id)


def _start_review_visibility_heartbeat(
    queue_url: str, receipt: str, message_id: str,
) -> _VisibilityHeartbeat | None:
    """Start a renewable visibility lease, or use queue fallback on failure."""
    if not _extend_review_visibility(queue_url, receipt, message_id):
        return None
    stop = threading.Event()
    thread = threading.Thread(
        target=_review_visibility_loop,
        args=(queue_url, receipt, message_id, stop),
        name="review-visibility-heartbeat",
        daemon=True,
    )
    thread.start()
    return _VisibilityHeartbeat(stop=stop, thread=thread)


def _stop_review_visibility_heartbeat(
    heartbeat: _VisibilityHeartbeat | None,
) -> None:
    if heartbeat is None:
        return
    heartbeat.stop.set()
    heartbeat.thread.join(timeout=1.0)


# --- owned queue-depth telemetry (#379) ------------------------------
# The DD AWS integration does not collect aws.sqs.* in this org, which left
# the queue/DLQ monitors permanently blind; the consumer owns the queues'
# depth signal instead. Full rationale + monitor semantics:
# specs/DESIGN.md "Owned queue-depth telemetry".


def _telemetry_interval_s() -> float:
    """Interval knob, clamped to [10, 300]s: a malformed value must not
    crash the consumer at import for a telemetry knob, near-zero must not
    hot-loop GetQueueAttributes, and an interval above the monitors' 15m
    window would starve every query into permanent No Data (the trap this
    telemetry retires)."""
    raw = os.getenv("GRUG_QUEUE_TELEMETRY_INTERVAL_S", "60")
    try:
        val = float(raw)
    except ValueError:
        log.warning("queue_telemetry_bad_interval", extra={"raw": raw})
        return 60.0
    clamped = min(300.0, max(10.0, val))
    if clamped != val:
        log.warning(
            "queue_telemetry_interval_clamped",
            extra={"raw": raw, "clamped": clamped},
        )
    return clamped


_TELEMETRY_INTERVAL_S = _telemetry_interval_s()

_TELEMETRY_QUEUE_NAMES = (
    "grug-rerun-jobs.fifo",
    "grug-rerun-jobs-dlq.fifo",
    "grug-cave-jobs.fifo",
    "grug-cave-jobs-dlq.fifo",
    "grug-cave-results.fifo",
    "grug-cave-results-dlq.fifo",
)


def _telemetry_base_url() -> str | None:
    """Derive the account-scoped SQS URL prefix from a queue URL the pod
    already carries - queue NAMES are fixed (explicit `name=` in Pulumi),
    so all six URLs are just prefix + fixed name, no extra env plumbing.
    Returns None (and the caller warns) if no env URL is present or the
    value has no path segment to strip."""
    for env_name in ("GRUG_RERUN_QUEUE_URL", "GRUG_CAVE_RESULTS_QUEUE_URL"):
        url = os.getenv(env_name, "")
        if not url:
            continue
        base, sep, tail = url.rpartition("/")
        if sep and tail:
            return base
    return None


def _emit_queue_depth_once() -> int:
    """One depth sweep. Returns queues successfully PROBED (emit_gauge is
    fire-and-forget UDP - delivery is the monitors' No Data problem, not
    ours). Per-queue best-effort: an AccessDenied on one queue must not
    cost the others their gauge. Each queue also emits a 1/0
    telemetry_queue_ok boolean per sweep: a queue whose success rate sinks
    below half over the monitor window pages BY NAME via the
    '[grug-consumer] Queue telemetry degraded' monitor - so neither a
    sustained nor an INTERMITTENT partial failure can silently re-blind
    that queue's depth monitor (audit stage-2 HIGH + codex peer review,
    PR #516)."""
    from observability import emit_gauge  # late: patchable, and webhook-image only

    if not os.getenv("DD_AGENT_HOST", ""):
        # Sweep-level guard: without an agent host every emit would
        # skip-warn individually (12 lines/min forever); one line per
        # sweep says the same thing. Total metric silence still reaches
        # the health monitor's notify_no_data pager.
        log.warning("queue_telemetry_no_agent_host")
        return 0
    base = _telemetry_base_url()
    if base is None:
        log.warning("queue_telemetry_no_base_url")
        return 0
    probed = 0
    for name in _TELEMETRY_QUEUE_NAMES:
        try:
            attrs = _sqs_telemetry.get_queue_attributes(
                QueueUrl=f"{base}/{name}",
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )["Attributes"]
            emit_gauge(
                "grug.sqs.messages_visible",
                float(attrs.get("ApproximateNumberOfMessages", 0)),
                {"queue": name},
            )
            emit_gauge(
                "grug.sqs.messages_not_visible",
                float(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
                {"queue": name},
            )
            probed += 1
            emit_gauge("grug.sqs.telemetry_queue_ok", 1.0, {"queue": name})
        except Exception as e:  # noqa: BLE001 - per-queue best-effort
            # botocore folds most service errors (AccessDenied, throttle)
            # into ClientError; the wire Code is the diagnosable bit.
            code = getattr(e, "response", None)
            code = (code or {}).get("Error", {}).get("Code") if isinstance(code, dict) else None
            log.warning(
                "queue_depth_probe_failed",
                extra={"queue": name, "kind": type(e).__name__, "code": code},
            )
            emit_gauge("grug.sqs.telemetry_queue_ok", 0.0, {"queue": name})
    return probed


def _telemetry_loop() -> None:
    """Thread body: depth sweep every interval until shutdown. Hard-wrapped
    never-raise; if this loop somehow dies anyway, the telemetry-health
    monitor's notify_no_data pages on the metric going silent - the pod is
    NOT restarted for a telemetry failure (the watchdog excludes this
    thread on purpose; review data flow > metric flow)."""
    log.info(
        "queue_telemetry_started",
        extra={"interval_s": _TELEMETRY_INTERVAL_S,
               "queues": len(_TELEMETRY_QUEUE_NAMES)},
    )
    while not _stop.is_set():
        try:
            _emit_queue_depth_once()
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders
            log.warning(
                "queue_telemetry_cycle_failed", extra={"kind": type(e).__name__},
            )
        _stop.wait(_TELEMETRY_INTERVAL_S)


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
    heartbeat = None
    if receipt and _is_durable_review(spec, message):
        heartbeat = _start_review_visibility_heartbeat(
            queue_url, receipt, str(message.get("MessageId", "")),
        )
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
    finally:
        _stop_review_visibility_heartbeat(heartbeat)
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


def _consumer_threads(specs: list[QueueSpec]) -> list[threading.Thread]:
    """Build one poll thread per configured queue worker."""
    threads: list[threading.Thread] = []
    for spec in specs:
        for worker in range(spec.workers):
            suffix = f"-{worker + 1}" if spec.workers > 1 else ""
            threads.append(
                threading.Thread(
                    target=_consume,
                    args=(spec,),
                    name=f"consume-{spec.kind}{suffix}",
                )
            )
    return threads


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
        with ddtrace.tracer.trace("grug.consumer.startup"):  # type: ignore
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
    ddtrace.tracer.flush()  # type: ignore


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
    # Roles Anywhere boot proof (#389): credentials are the DEEPEST dep -
    # prove the cert path (or crash loud) before probing anything else.
    from aws_identity import prove_roles_anywhere_identity

    prove_roles_anywhere_identity()

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

    threads = _consumer_threads(_specs())
    for t in threads:
        t.start()

    # Owned queue-depth telemetry (#379). Daemon + OUTSIDE the watchdog
    # list: a telemetry failure surfaces as the telemetry-health monitor's
    # No Data page, never as a pod restart that would interrupt review
    # message flow.
    threading.Thread(
        target=_telemetry_loop, name="queue-telemetry", daemon=True,
    ).start()
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
    # SHARED join deadline (not per-thread): five threads x a per-thread 30s
    # join could eat 150s while the pod is SIGKILLed at its grace period,
    # which would skip the claim sweep below (review-bot findings on #607). 15s
    # total gives quick handlers a chance to finish and still leaves most of
    # the grace window for the sweep. Long-poll wait is 20s, so idle threads
    # notice _stop almost immediately.
    join_deadline = time.monotonic() + 15.0
    for t in threads:
        t.join(timeout=max(0.0, join_deadline - time.monotonic()))
    # Release any snapshot claims still held by handlers that did NOT finish
    # inside the join budget (Elder reviews run minutes; every deploy rolls
    # this pod). Without this the orphaned lease makes the SQS redelivery
    # bounce off "claim busy" for up to the 900s lease, burning receives
    # toward the DLQ while the PR waits on its REQUIRED check (grug#515).
    from rerun import release_active_review_claims  # type: ignore[attr-defined]

    released = release_active_review_claims()
    if released:
        log.info("consumer_shutdown_released_claims", extra={"count": released})
    if died:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
