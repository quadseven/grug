"""Tests for observability.JsonFormatter + configure_logging.

Covers:
- Standard fields (level, logger, msg, ts)
- extra={} keys lifted into payload
- Reserved LogRecord fields excluded
- exc_info → exc_info string field
- non-JSON-serialisable values stringified via default=str
- configure_logging sets level from GRUG_LOG_LEVEL env
- configure_logging defaults to INFO when env unset
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from observability import JsonFormatter, configure_logging, emit_enforcement_metric


def _format_record(level=logging.INFO, msg="hello", **extra):
    record = logging.LogRecord(
        name="grug.test", level=level, pathname="/tmp/x.py", lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return JsonFormatter().format(record)


def test_standard_fields_in_output():
    out = json.loads(_format_record(level=logging.INFO, msg="hi"))
    assert out["level"] == "info"
    assert out["logger"] == "grug.test"
    assert out["msg"] == "hi"
    assert "ts" in out


def test_extra_kwargs_lifted_into_payload():
    out = json.loads(_format_record(installation_id=42, owner="myorg"))
    assert out["installation_id"] == 42
    assert out["owner"] == "myorg"


def test_reserved_logrecord_keys_excluded():
    out = json.loads(_format_record())
    # Internal LogRecord plumbing must not leak into payload
    for key in ("pathname", "filename", "module", "lineno", "funcName",
                "process", "thread", "args", "levelname", "levelno"):
        assert key not in out


def test_exc_info_field_when_exception_attached():
    try:
        raise ValueError("oh no")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="grug.test", level=logging.ERROR, pathname="/x.py", lineno=1,
        msg="boom", args=(), exc_info=exc_info,
    )
    out = json.loads(JsonFormatter().format(record))
    assert "exc_info" in out
    assert "ValueError" in out["exc_info"]
    assert "oh no" in out["exc_info"]


def test_non_serialisable_extra_values_use_default_str():
    class _Custom:
        def __str__(self):
            return "<custom-repr>"

    out = json.loads(_format_record(blob=_Custom()))
    assert out["blob"] == "<custom-repr>"


def test_configure_logging_uses_env_level(monkeypatch):
    monkeypatch.setenv("GRUG_LOG_LEVEL", "WARNING")
    configure_logging()
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_defaults_to_info(monkeypatch):
    monkeypatch.delenv("GRUG_LOG_LEVEL", raising=False)
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_replaces_existing_handlers():
    """Idempotent re-configure: second call doesn't accumulate handlers."""
    configure_logging()
    handler_count_first = len(logging.getLogger().handlers)
    configure_logging()
    handler_count_second = len(logging.getLogger().handlers)
    assert handler_count_first == handler_count_second == 1


# ── emit_enforcement_metric ─────────────────────────────────────────

def _sent_datagrams(monkeypatch):
    """Route the emit's UDP socket to a capture list; returns the list."""
    from unittest.mock import MagicMock
    sent = []
    sock = MagicMock()
    sock.sendto.side_effect = lambda payload, addr: sent.append((payload, addr))
    monkeypatch.setattr("observability.socket.socket", lambda *a, **k: sock)
    return sent


def test_emit_enforcement_metric_sends_dogstatsd_datagram(monkeypatch):
    monkeypatch.setenv("DD_AGENT_HOST", "10.0.0.99")
    monkeypatch.setenv("GRUG_ENV", "prod")
    monkeypatch.delenv("DD_ENV", raising=False)
    sent = _sent_datagrams(monkeypatch)
    emit_enforcement_metric("githumps/infra", "grug_managed")
    assert sent == [(
        b"grug.enforcement.state:1.0|g|#repo:githumps/infra,persona:tpm,"
        b"enforcement_type:grug_managed,env:prod",
        ("10.0.0.99", 8125),
    )]


def test_emit_enforcement_metric_value_mapping(monkeypatch):
    monkeypatch.setenv("DD_AGENT_HOST", "10.0.0.99")
    monkeypatch.setenv("GRUG_ENV", "prod")
    monkeypatch.delenv("DD_ENV", raising=False)
    sent = _sent_datagrams(monkeypatch)
    emit_enforcement_metric("o/r", "grug_managed")
    emit_enforcement_metric("o/r", "external")
    emit_enforcement_metric("o/r", "none")
    values = [p.split(b":")[1].split(b"|")[0] for p, _ in sent]
    assert values == [b"1.0", b"0.5", b"0.0"]


def test_emit_enforcement_metric_skips_without_agent_host(monkeypatch, caplog):
    monkeypatch.delenv("DD_AGENT_HOST", raising=False)
    sent = _sent_datagrams(monkeypatch)
    with caplog.at_level("WARNING", logger="grug.observability"):
        emit_enforcement_metric("o/r", "grug_managed")
    assert sent == []
    assert "enforcement_metric_skipped_no_agent_host" in caplog.text


def test_emit_enforcement_metric_does_not_raise_on_socket_failure(monkeypatch, caplog):
    monkeypatch.setenv("DD_AGENT_HOST", "10.0.0.99")

    def _boom(*a, **k):
        raise OSError("network unreachable")

    monkeypatch.setattr("observability.socket.socket", _boom)
    with caplog.at_level("WARNING", logger="grug.observability"):
        emit_enforcement_metric("o/r", "grug_managed")
    assert "enforcement_metric_emit_failed" in caplog.text
