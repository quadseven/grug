"""Pure pack/unpack of a job payload for the SQS airlock.

A small payload rides INLINE inside the queue message; a large one (over a
threshold, since SQS caps message size) is spilled to S3 and the message carries
only a pointer. The S3 put/get are INJECTED as callables, so this module is pure
and unit-testable with no boto3.
"""

# Spark-authored: cohere north-mini-code-1.0:bf16 on an on-prem DGX Spark,
# 2026-06-27 -- the model's first production job. The pack/unpack logic below is
# its generation; reviewed + hardened by Claude.
# (git grep "Spark-authored" lists all on-prem-model-generated code.)

from __future__ import annotations

import json
from collections.abc import Callable

from .schema import PayloadRef


def pack(
    payload: dict,
    *,
    threshold_bytes: int,
    put_s3: Callable[[bytes], dict] | None = None,
) -> PayloadRef:
    """Inline when the serialized payload fits under `threshold_bytes`; else spill
    the bytes to S3 via `put_s3` and carry an s3 ref."""
    data = json.dumps(payload).encode("utf-8")
    if len(data) <= threshold_bytes:
        return PayloadRef(kind="inline", inline=payload, s3=None)
    if put_s3 is None:
        raise ValueError("payload exceeds threshold but no put_s3 provided")
    ref = put_s3(data)
    return PayloadRef(kind="s3", inline=None, s3=ref)


def unpack(ref: PayloadRef, *, get_s3: Callable[[dict], bytes] | None = None) -> dict:
    """Reverse `pack`: inline refs return their payload; s3 refs are fetched via
    `get_s3` and JSON-decoded."""
    if ref.kind == "inline":
        return ref.inline or {}
    if ref.kind == "s3":
        if get_s3 is None:
            raise ValueError("s3 payload but no get_s3 provided")
        if ref.s3 is None:
            raise ValueError("s3 payload ref missing its s3 pointer")
        return json.loads(get_s3(ref.s3))
    raise ValueError(f"unknown payload kind: {ref.kind}")
