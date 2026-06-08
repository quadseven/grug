"""#272 — lambda_handler routes self-invoked async jobs to the worker
BEFORE Mangum (which can't parse a non-HTTP event), and leaves HTTP
(Function-URL) events to Mangum unchanged."""
from __future__ import annotations

from unittest.mock import patch

import async_dispatch as ad
import lambda_handler as lh


def test_async_job_event_routes_to_worker():
    event = {ad.ASYNC_JOB_KEY: ad.ELDER_REVIEW_JOB, "delivery_id": "d", "payload": {}}
    with patch("async_dispatch.run_elder_job", return_value={"ok": True}) as mock_run, \
         patch.object(lh, "_http_handler") as mock_http:
        out = lh.handler(event, context=None)
    mock_run.assert_called_once_with(event)
    mock_http.assert_not_called()  # Mangum never sees the raw job event
    assert out == {"ok": True}


def test_http_event_routes_to_mangum():
    # A Function-URL HTTP event has no async-job sentinel.
    event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/webhook/github"}
    with patch("async_dispatch.run_elder_job") as mock_run, \
         patch.object(lh, "_http_handler", return_value={"statusCode": 200}) as mock_http:
        out = lh.handler(event, context=None)
    mock_http.assert_called_once()
    mock_run.assert_not_called()
    assert out == {"statusCode": 200}


def test_non_dict_event_routes_to_mangum():
    """Defensive: a non-dict event (shouldn't happen) must not crash the
    sentinel check — falls through to Mangum."""
    with patch("async_dispatch.run_elder_job") as mock_run, \
         patch.object(lh, "_http_handler", return_value="ok") as mock_http:
        out = lh.handler("not-a-dict", context=None)
    mock_http.assert_called_once()
    mock_run.assert_not_called()
    assert out == "ok"


_CAVE_ARN = "arn:aws:sqs:us-east-1:1:grug-cave-results.fifo"
_RERUN_ARN = "arn:aws:sqs:us-east-1:1:grug-rerun-jobs.fifo"


def test_cave_results_sqs_event_routes_to_fallback_handler():
    # #310 — the Cave connector's results (discriminated by queue ARN, #305).
    event = {"Records": [{"eventSource": "aws:sqs", "eventSourceARN": _CAVE_ARN, "body": "{}"}]}
    with patch("cave_fallback.handle_fallback_result", return_value={"healed": 1}) as mock_h, \
         patch("rerun.handle_rerun_jobs") as mock_r, \
         patch.object(lh, "_http_handler") as mock_http:
        out = lh.handler(event, context=None)
    mock_h.assert_called_once_with(event)
    mock_r.assert_not_called()
    mock_http.assert_not_called()  # Mangum never sees the raw SQS event
    assert out == {"healed": 1}


def test_rerun_jobs_sqs_event_routes_to_rerun_consumer():
    # #305 — re-run jobs go to the rerun consumer, NOT the cave handler.
    event = {"Records": [{"eventSource": "aws:sqs", "eventSourceARN": _RERUN_ARN, "body": "{}"}]}
    with patch("rerun.handle_rerun_jobs", return_value={"records": 1}) as mock_r, \
         patch("cave_fallback.handle_fallback_result") as mock_h, \
         patch.object(lh, "_http_handler") as mock_http:
        out = lh.handler(event, context=None)
    mock_r.assert_called_once_with(event)
    mock_h.assert_not_called()
    mock_http.assert_not_called()
    assert out == {"records": 1}


def test_unknown_queue_sqs_event_falls_through_to_mangum():
    # An SQS event from a queue we don't recognize must not guess a handler.
    event = {"Records": [{"eventSource": "aws:sqs", "eventSourceARN": "arn:aws:sqs:us-east-1:1:some-other.fifo", "body": "{}"}]}
    with patch("cave_fallback.handle_fallback_result") as mock_h, \
         patch("rerun.handle_rerun_jobs") as mock_r, \
         patch.object(lh, "_http_handler", return_value="ok") as mock_http:
        out = lh.handler(event, context=None)
    mock_h.assert_not_called()
    mock_r.assert_not_called()
    mock_http.assert_called_once()
    assert out == "ok"


def test_non_sqs_records_event_falls_through_to_mangum():
    # A Records event from a non-SQS source must NOT misroute to the fallback
    # handler (defensive — only eventSource==aws:sqs is ours).
    event = {"Records": [{"eventSource": "aws:s3"}]}
    with patch("cave_fallback.handle_fallback_result") as mock_h, \
         patch.object(lh, "_http_handler", return_value="ok") as mock_http:
        out = lh.handler(event, context=None)
    mock_h.assert_not_called()
    mock_http.assert_called_once()
    assert out == "ok"


def test_mixed_batch_is_not_treated_as_sqs():
    # #322 peer-review: a batch with any non-SQS record must NOT route to the
    # fallback handler (all-records check, not first-only).
    event = {"Records": [{"eventSource": "aws:sqs"}, {"eventSource": "aws:s3"}]}
    with patch("cave_fallback.handle_fallback_result") as mock_h, \
         patch.object(lh, "_http_handler", return_value="ok") as mock_http:
        out = lh.handler(event, context=None)
    mock_h.assert_not_called()
    mock_http.assert_called_once()
    assert out == "ok"
