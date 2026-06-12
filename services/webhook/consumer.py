# WEBHOOK-ONLY (NOT mirrored): k8s replacement for the two SQS→Lambda
# event-source mappings. The api service produces to these queues but
# never consumes — like lambda_handler.py, per ADR-0001 only modules
# BOTH services run are mirrored.
"""SQS consumers for the k8s runtime (#368).

On Lambda, AWS event-source mappings deliver `grug-cave-results.fifo`
and `grug-rerun-jobs.fifo` batches into `lambda_handler.handler`. A pod
has no ESM, so this entrypoint long-polls both queues (one thread each)
and feeds each message to the SAME handler the ESM used, wrapped in the
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


def main() -> None:
    logging.basicConfig(level=os.getenv("GRUG_LOG_LEVEL", "INFO"))

    def _terminate(signum: int, _frame: Any) -> None:
        log.info("consumer_terminating", extra={"signal": signum})
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
            log.error("consumer_thread_died")
            _stop.set()
            break
        _stop.wait(5.0)
    # Long-poll wait is 20s, so threads notice _stop well inside the pod's
    # default 30s terminationGracePeriod; in-flight handlers finish first.
    for t in threads:
        t.join(timeout=30.0)
    if died:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
