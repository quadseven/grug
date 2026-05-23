# MIRRORED — sibling at services/webhook/observability.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Structured JSON logging configuration.

DD Lambda extension layer (added in Slice 9 via Pulumi) auto-ships
stdout/stderr to Datadog. This module configures Python's stdlib logging
to emit JSON lines so DD ingests them as structured events.

Service tag (`grug-webhook`) + env tag are set via Lambda env vars
that DD's extension reads (`DD_SERVICE`, `DD_ENV`).
"""

from __future__ import annotations

import json
import logging
import os
import sys


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
