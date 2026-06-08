"""Lambda entry point. Mangum wraps the FastAPI app for Lambda Function URL.

Module-scope import → FastAPI app initialized once per warm container.
Cold-start cost is paid here; subsequent invocations reuse the warm app.

Two invocation shapes reach this handler (#272):
  1. **Function-URL HTTP events** (GitHub webhooks via Cloudflare) → Mangum.
  2. **Self-invoked async jobs** (`InvocationType="Event"` from
     `async_dispatch.enqueue_elder_review`) → a RAW JSON event carrying the
     `grug_async_job` sentinel. Mangum can't parse a non-HTTP event, so we
     sniff the sentinel and route to the worker FIRST.
"""

from __future__ import annotations

from typing import Any

from mangum import Mangum

from async_dispatch import ASYNC_JOB_KEY
from main import app

_http_handler = Mangum(app, lifespan="off")


def handler(event: Any, context: Any) -> Any:
    """Route self-invoked async jobs to the worker; everything else is an
    HTTP (Function-URL) event handled by Mangum."""
    if isinstance(event, dict) and event.get(ASYNC_JOB_KEY):
        # Lazy import keeps the worker's Elder dependency graph off the
        # HTTP (sync) cold-start path.
        from async_dispatch import run_elder_job
        return run_elder_job(event)
    # Two SQS event-source mappings reach this Lambda, discriminated by which
    # queue the batch came from (the `eventSourceARN`): the Cave connector's
    # results (#310) and operator-triggered re-runs (#305). Mangum can't parse
    # a raw `Records` event, so route SQS FIRST.
    sqs_kind = _sqs_queue_kind(event)
    if sqs_kind == "cave-results":
        from cave_fallback import handle_fallback_result
        return handle_fallback_result(event)
    if sqs_kind == "rerun-jobs":
        from rerun import handle_rerun_jobs
        return handle_rerun_jobs(event)
    return _http_handler(event, context)


def _sqs_queue_kind(event: Any) -> str | None:
    """Classify an SQS event-source-mapping batch by its source queue:
    `"cave-results"`, `"rerun-jobs"`, or `None` (not SQS / unknown queue → fall
    through to Mangum). Requires EVERY record to be `aws:sqs` from the SAME
    queue, so a Function-URL HTTP event (no `Records`) or a mixed/foreign batch
    never misroutes."""
    if not isinstance(event, dict):
        return None
    records = event.get("Records")
    if not isinstance(records, list) or not records:
        return None
    if not all(
        isinstance(r, dict) and r.get("eventSource") == "aws:sqs" for r in records
    ):
        return None
    arns = {r.get("eventSourceARN", "") for r in records}
    if len(arns) != 1:
        return None  # a mixed batch (shouldn't happen) — don't guess
    arn = arns.pop()
    if arn.endswith(":grug-cave-results.fifo"):
        return "cave-results"
    if arn.endswith(":grug-rerun-jobs.fifo"):
        return "rerun-jobs"
    return None
