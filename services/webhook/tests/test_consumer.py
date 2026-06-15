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
import pytest  # noqa: E402


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
