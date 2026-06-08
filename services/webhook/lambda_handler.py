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
    if _is_sqs_event(event):
        # SQS event-source mapping delivers the Cave connector's results
        # (grug-cave-results) as a raw `Records` event — Mangum can't parse
        # it, so route to the fallback result handler FIRST (#310, ADR-0005).
        from cave_fallback import handle_fallback_result
        return handle_fallback_result(event)
    return _http_handler(event, context)


def _is_sqs_event(event: Any) -> bool:
    """True for an SQS event-source-mapping batch (`Records[*].eventSource ==
    'aws:sqs'`). Defensive against non-dict events + empty/foreign Records so a
    Function-URL HTTP event (which has no `Records`) never misroutes here."""
    if not isinstance(event, dict):
        return False
    records = event.get("Records")
    if not isinstance(records, list) or not records:
        return False
    # peer-review (OpenRouter + Poolside + Spark, CONFIRMED 3x): require EVERY
    # record to be SQS, not just the first. batch_size=1 makes this moot today,
    # but a future ESM reconfig (batch>1) or a mixed batch must not misroute a
    # foreign record into handle_fallback_result.
    return all(
        isinstance(r, dict) and r.get("eventSource") == "aws:sqs" for r in records
    )
