"""Enqueue a `FallbackJob` onto a Spark-Cave jobs FIFO (SQS).

Packs the payload (inline or S3 spillover) then `SendMessage` with the FIFO
`MessageGroupId` (`persona:principal`, so one lane's backlog can't starve
another's) + `MessageDeduplicationId` (the job's `dedup_key`). The SQS client +
the S3 putter are injected, so this is unit-testable with fakes -- no live AWS.
"""

from __future__ import annotations

from collections.abc import Callable

from .packing import pack
from .schema import SCHEMA_VERSION, FallbackJob

# SQS standard max message body is 256 KB; spill well below it to leave room for
# the JSON envelope around the payload.
DEFAULT_INLINE_THRESHOLD_BYTES = 200_000


def enqueue(
    *,
    sqs,
    queue_url: str,
    persona: str,
    principal_id: str,
    request_id: str,
    payload: dict,
    put_s3: Callable[[bytes], dict] | None = None,
    inline_threshold_bytes: int = DEFAULT_INLINE_THRESHOLD_BYTES,
) -> FallbackJob:
    """Pack `payload` and send a `FallbackJob` to the jobs FIFO. Returns the
    job that was sent (so callers can record its `request_id`/`dedup_key`)."""
    ref = pack(payload, threshold_bytes=inline_threshold_bytes, put_s3=put_s3)
    job = FallbackJob(
        schema_version=SCHEMA_VERSION,
        persona=persona,
        principal_id=principal_id,
        request_id=request_id,
        payload=ref,
    )
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=job.to_json(),
        MessageGroupId=f"{persona}:{principal_id}",
        MessageDeduplicationId=job.dedup_key,
    )
    return job
