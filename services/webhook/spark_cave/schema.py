"""Wire schema for the Spark-Cave SQS airlock.

A cloud service enqueues a `FallbackJob` (carrying a `persona` so an on-prem
connector can route it to the right GPU model); the connector publishes a
`FallbackResult` back. Persona-generic so distinct services (e.g. a
`code-review` and a `meal-gen` persona) share one contract. Pure dataclasses +
JSON round-trip + a deterministic FIFO dedup key. No I/O, no boto3.
"""

# Spark-authored: first-drafted on an on-prem DGX Spark by its then-resident
# coder model (qwen3-coder-next), 2026-06-27; substantially cleaned, typed, and
# finalized by Claude.
# (git grep "Spark-authored" lists all on-prem-model-generated code.)

from __future__ import annotations

import json
from dataclasses import dataclass

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PayloadRef:
    kind: str  # "inline" or "s3"
    inline: dict | None  # the payload when kind == "inline"
    s3: dict | None  # {"bucket": str, "key": str} when kind == "s3"


@dataclass(frozen=True, slots=True)
class FallbackJob:
    schema_version: int
    persona: str
    principal_id: str
    request_id: str
    payload: PayloadRef

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "persona": self.persona,
                "principal_id": self.principal_id,
                "request_id": self.request_id,
                "payload": {
                    "kind": self.payload.kind,
                    "inline": self.payload.inline,
                    "s3": self.payload.s3,
                },
            }
        )

    @classmethod
    def from_json(cls, s: str) -> FallbackJob:
        data = json.loads(s)
        p = data["payload"]
        return cls(
            schema_version=data["schema_version"],
            persona=data["persona"],
            principal_id=data["principal_id"],
            request_id=data["request_id"],
            payload=PayloadRef(kind=p["kind"], inline=p.get("inline"), s3=p.get("s3")),
        )

    @property
    def dedup_key(self) -> str:
        """FIFO dedup id: a (persona, principal, request) is processed once."""
        return f"{self.persona}:{self.principal_id}:{self.request_id}"


@dataclass(frozen=True, slots=True)
class FallbackResult:
    schema_version: int
    persona: str
    principal_id: str
    request_id: str
    ok: bool
    result: dict | None
    error: str | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "persona": self.persona,
                "principal_id": self.principal_id,
                "request_id": self.request_id,
                "ok": self.ok,
                "result": self.result,
                "error": self.error,
            }
        )

    @classmethod
    def from_json(cls, s: str) -> FallbackResult:
        data = json.loads(s)
        return cls(
            schema_version=data["schema_version"],
            persona=data["persona"],
            principal_id=data["principal_id"],
            request_id=data["request_id"],
            ok=data["ok"],
            result=data.get("result"),
            error=data.get("error"),
        )
