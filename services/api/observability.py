# MIRRORED — sibling at services/webhook/observability.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Structured JSON logging configuration.

DD Lambda extension layer (added in Slice 9 via Pulumi) auto-ships
stdout/stderr to Datadog. This module configures Python's stdlib logging
to emit JSON lines so DD ingests them as structured events.

Service tag (`grug-webhook`) + env tag are set via Lambda env vars
that DD's extension reads (`DD_SERVICE`, `DD_ENV`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sys

# Per-process random key for fingerprint(). Stable for the lifetime of a
# warm Lambda container; rotated on every cold start. Identifiers logged
# via fingerprint() correlate within one process but cannot be reversed
# to the underlying value by anyone reading the logs (DD, CloudWatch).
_FP_KEY = secrets.token_bytes(32)


def fingerprint(value: object) -> str:
    """Return a non-reversible per-process correlation id for `value`.

    Use to log identifiers without leaking the underlying value.

        log.info("user_op", extra={"user_fp": fingerprint(github_user_id)})

    The fingerprint is HMAC-style (SHA-256 of a per-process random key +
    the value) truncated to 12 hex chars. Multiple log lines for the same
    value in the same process produce the same fingerprint, so DD/CW
    queries can still correlate. The key never leaves the process.

    Use this for genuinely-secret PII (OAuth tokens, internal user UUIDs,
    email addresses, private keys). github_user_id is logged raw across
    grug today by design — DD is grug's authorized observability sink,
    and the support flow needs the raw id. Migrating to fingerprint() for
    user_id is a deliberate future call, not a blocker.
    """
    return hashlib.sha256(_FP_KEY + str(value).encode("utf-8")).hexdigest()[:12]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        # Anything passed via `extra={...}` lands on the record.
        for key, value in record.__dict__.items():
            if key in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "asctime",
            }:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    level = os.getenv("GRUG_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
