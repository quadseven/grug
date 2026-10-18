"""#1208: one off-by-default switch for every runtime Datadog path.

With GRUG_DATADOG_ENABLED unset, nothing may reach Datadog or a node
agent (no UDP datagram, no HTTP request, no SSM read of a Datadog key, no
ddtrace bootstrap) and nothing may log about it. Every test here starts
from the switch UNSET, with the environment otherwise wired the way the
deployed manifests wire it (DD_AGENT_HOST set, DD_LLMOBS_ENABLED=true).
"""
from __future__ import annotations

import json
import logging
import re
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import datadog_switch
import llm_client as lc
import observability

_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _deployed_env_switch_unset(monkeypatch):
    monkeypatch.delenv("GRUG_DATADOG_ENABLED", raising=False)
    monkeypatch.setenv("DD_AGENT_HOST", "192.0.2.99")
    monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
    monkeypatch.setenv("DD_API_KEY", "not-a-real-key")


@pytest.fixture
def no_sockets(monkeypatch):
    """Fail the test on any socket creation: the off path opens none."""
    def _boom(*a, **kw):
        raise AssertionError("socket opened while Datadog is switched off")
    monkeypatch.setattr(socket, "socket", _boom)


# --- the switch itself ------------------------------------------------


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_switch_is_off_unless_truthy(monkeypatch, value):
    monkeypatch.setenv("GRUG_DATADOG_ENABLED", value)
    assert datadog_switch.datadog_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " True "])
def test_switch_truthy_values_turn_it_on(monkeypatch, value):
    monkeypatch.setenv("GRUG_DATADOG_ENABLED", value)
    assert datadog_switch.datadog_enabled() is True


def test_switch_defaults_off():
    assert datadog_switch.datadog_enabled() is False


# --- the ddtrace-run launcher -------------------------------------------


def test_launcher_off_runs_command_bare_with_tracing_forced_off():
    argv, env = datadog_switch.launch_argv(["python", "consumer.py"])
    assert argv == ["python", "consumer.py"]
    assert env["DD_TRACE_ENABLED"] == "false"
    # The manifests still say DD_LLMOBS_ENABLED=true; off must override it.
    assert env["DD_LLMOBS_ENABLED"] == "false"


def test_launcher_on_prepends_ddtrace_run(monkeypatch):
    monkeypatch.setenv("GRUG_DATADOG_ENABLED", "true")
    argv, env = datadog_switch.launch_argv(["uvicorn", "main:app"])
    assert argv == ["ddtrace-run", "uvicorn", "main:app"]
    assert env["DD_LLMOBS_ENABLED"] == "true"  # untouched when on


def test_launcher_main_execs_the_resolved_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        datadog_switch.os, "execvpe",
        lambda f, args, env: seen.update(f=f, args=args, env=env),
    )
    datadog_switch.main(["python", "-c", "pass"])
    assert seen["f"] == "python"
    assert seen["args"] == ["python", "-c", "pass"]


def test_launcher_main_without_a_command_exits_2():
    with pytest.raises(SystemExit) as e:
        datadog_switch.main([])
    assert e.value.code == 2


def _commands_in(text: str) -> list[list[str]]:
    text = re.sub(r"\\\n\s*", " ", text)  # join Dockerfile line continuations
    return [json.loads(m) for m in re.findall(r"(?:command:|CMD)\s*(\[.*\])", text)]


def test_no_process_is_launched_under_bare_ddtrace_run():
    """Every service entry point goes through the switch, so a deploy with
    the switch off never bootstraps ddtrace."""
    files = [
        _REPO / "services" / "Dockerfile",
        *sorted((_REPO / "k8s").glob("*.yaml")),
    ]
    launched = []
    for f in files:
        for cmd in _commands_in(f.read_text()):
            assert cmd[0] != "ddtrace-run", f"{f.name}: bare ddtrace-run in {cmd}"
            launched.append(cmd)
    via_switch = [c for c in launched if c[:3] == ["python", "-m", "datadog_switch"]]
    # api/webhook (Dockerfile CMD), consumer, poller.
    assert len(via_switch) >= 3


def test_grugthink_cmd_only_uses_ddtrace_run_when_switched_on():
    text = (_REPO / "grugthink" / "Dockerfile").read_text()
    (cmd,) = [c for c in _commands_in(text) if c[0] == "/bin/bash"]
    script = cmd[-1]
    assert 'case "${GRUG_DATADOG_ENABLED:-}" in' in script
    on, off = script.split(";;")[0], script.split(";;")[1]
    assert "exec ddtrace-run python grugthink.py" in on
    assert "ddtrace-run" not in off
    assert "DD_TRACE_ENABLED=false DD_LLMOBS_ENABLED=false exec python grugthink.py" in off


# --- DogStatsD emitters -------------------------------------------------


def test_emitters_send_nothing_and_log_nothing_when_off(no_sockets, caplog):
    with patch.object(observability, "_send_dogstatsd") as send, \
            caplog.at_level(logging.DEBUG, logger="grug.observability"):
        observability.emit_gauge("grug.test.gauge", 1.0, {"k": "v"})
        observability.emit_count("grug.test.count", 1)
        observability.emit_enforcement_metric("o/r", "grug_managed")
    assert send.call_count == 0
    assert caplog.records == []


def test_emitters_do_not_warn_about_a_missing_agent_host_when_off(monkeypatch, caplog):
    """Before #1208 an unset DD_AGENT_HOST logged a WARNING on every emit."""
    monkeypatch.delenv("DD_AGENT_HOST", raising=False)
    with caplog.at_level(logging.DEBUG, logger="grug.observability"):
        observability.emit_gauge("grug.test.gauge", 1.0)
        observability.emit_count("grug.test.count")
        observability.emit_enforcement_metric("o/r", "none")
    assert caplog.records == []


# --- LLM Observability seams --------------------------------------------


def test_llmobs_seams_never_touch_the_sdk_when_off(monkeypatch, caplog):
    sdk = MagicMock()
    monkeypatch.setattr(lc, "_LLMObs", sdk)
    with caplog.at_level(logging.DEBUG):
        with lc._llmobs_llm(model_name="m", model_provider="p", name="x") as span:
            lc._llmobs_annotate(span=span, metrics={"input_tokens": 1})
        assert lc._llmobs_export(span) is None
        lc._llmobs_submit_evaluation(label="l", metric_type="score", value=1)
    assert sdk.mock_calls == []
    assert not [r for r in caplog.records if "llmobs" in r.getMessage()]


def test_llmobs_seams_call_the_sdk_when_on(monkeypatch):
    monkeypatch.setenv("GRUG_DATADOG_ENABLED", "true")
    sdk = MagicMock()
    monkeypatch.setattr(lc, "_LLMObs", sdk)
    lc._llmobs_llm(model_name="m", model_provider="p", name="x")
    lc._llmobs_annotate(metrics={"input_tokens": 1})
    lc._llmobs_submit_evaluation(label="l", metric_type="score", value=1)
    assert sdk.llm.call_count == 1
    assert sdk.annotate.call_count == 1
    assert sdk.submit_evaluation.call_count == 1


# --- Datadog HTTP API readers: Omen + Warder SLO gate ---------------------


def test_omen_reads_no_key_and_queries_nothing_when_off(monkeypatch):
    from personas.code_reviewer import omen
    from personas.code_reviewer.diff_parser import parse_diff

    key_reads = []
    monkeypatch.setattr(omen, "get_omen_service_map", lambda: {"o/r": "grug-webhook"})
    monkeypatch.setattr(omen, "get_dd_api_key", lambda: key_reads.append("api") or "api")
    monkeypatch.setattr(omen, "get_dd_app_key", lambda: key_reads.append("app") or "app")
    post = MagicMock()
    monkeypatch.setattr(omen.httpx, "post", post)
    diff = (
        "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n"
        "@@ -1,1 +1,2 @@\n import os\n+y = 1\n"
    )
    assert omen.build_runtime_context("o", "r", parse_diff(diff)) is None
    assert key_reads == []
    assert post.call_count == 0


def test_warder_slo_gate_reads_no_key_and_queries_nothing_when_off(monkeypatch):
    from personas.warder import dispatch as warder

    key_reads = []
    monkeypatch.setattr(warder, "get_warder_slo_map", lambda: {"o/r": 123})
    monkeypatch.setattr(warder, "get_dd_api_key", lambda: key_reads.append("api") or "api")
    monkeypatch.setattr(warder, "get_dd_app_key", lambda: key_reads.append("app") or "app")
    query = MagicMock(return_value="Alert")
    monkeypatch.setattr(warder, "query_monitor_state", query)
    assert warder._gate_verdict("o", "r", blocking=True) == ("", None, False)
    assert key_reads == []
    assert query.call_count == 0


# --- consumer: APM warmup/flush and the queue-depth sweep ----------------


def test_consumer_trace_flush_and_warmup_are_noops_when_off(monkeypatch, caplog):
    import consumer
    import ddtrace

    tracer = MagicMock()
    monkeypatch.setattr(ddtrace, "tracer", tracer)
    with caplog.at_level(logging.DEBUG):
        consumer._warm_trace_writer()
        consumer._flush_traces()
    assert tracer.mock_calls == []
    assert caplog.records == []


def test_consumer_queue_depth_sweep_probes_nothing_when_off(monkeypatch, caplog):
    import consumer

    monkeypatch.setenv("GRUG_RERUN_QUEUE_URL", "https://sqs.example.com/123/q.fifo")
    with patch.object(consumer._sqs_telemetry, "get_queue_attributes") as attrs, \
            caplog.at_level(logging.DEBUG):
        assert consumer._emit_queue_depth_once() == 0
    assert attrs.call_count == 0
    assert caplog.records == []


# --- review_latency bench LLMObs export ---------------------------------


def test_bench_llmobs_export_stays_off_even_with_dd_llmobs_enabled(monkeypatch):
    from review_latency import llmobs_export as le

    monkeypatch.setattr(le, "_enabled", False)
    monkeypatch.setattr(le, "_enable_attempted", False)
    monkeypatch.setenv("GRUG_BENCH_LLMOBS", "1")
    enable = MagicMock()
    monkeypatch.setattr("ddtrace.llmobs.LLMObs.enable", enable)
    assert le.maybe_enable() is False
    assert enable.call_count == 0
