"""Grug Omen tests (#470) - production-signal fusion. The load-bearing
contracts: EXPLICIT ALLOW (no mapping = zero DD calls), fail-open
degrade, noise floor, budget, and the prompt injection shape.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx

from personas.code_reviewer import omen
from personas.code_reviewer.diff_parser import parse_diff

_DIFF = """diff --git a/src/dispatcher.py b/src/dispatcher.py
--- a/src/dispatcher.py
+++ b/src/dispatcher.py
@@ -1,2 +1,3 @@
 import os
+data = payload[key]
"""


def _dd_response(count):
    return httpx.Response(
        200,
        json={"data": {"buckets": [{"computes": {"c0": count}}]}},
        request=httpx.Request("POST", "https://api.datadoghq.com/x"),
    )


def _wire(monkeypatch, *, mapping, count=50):
    monkeypatch.setattr(omen, "get_omen_service_map", lambda: mapping)
    monkeypatch.setattr(omen, "get_dd_api_key", lambda: "api")
    monkeypatch.setattr(omen, "get_dd_app_key", lambda: "app")
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return _dd_response(count)

    monkeypatch.setattr(omen.httpx, "post", fake_post)
    return calls


def test_no_mapping_means_zero_dd_calls(monkeypatch):
    calls = _wire(monkeypatch, mapping={})
    assert omen.build_runtime_context("o", "r", parse_diff(_DIFF)) is None
    assert calls == []


def test_mapped_repo_with_errors_produces_signal(monkeypatch):
    calls = _wire(monkeypatch, mapping={"o/r": "grug-webhook"}, count=47)
    ctx = omen.build_runtime_context("o", "r", parse_diff(_DIFF))
    assert ctx is not None
    assert "dispatcher.py" in ctx and "47 error" in ctx
    assert "grug-webhook" in ctx and "evidence" in ctx
    assert len(calls) == 1


def test_below_noise_floor_produces_nothing(monkeypatch):
    _wire(monkeypatch, mapping={"o/r": "svc"}, count=omen._MIN_ERROR_COUNT - 1)
    assert omen.build_runtime_context("o", "r", parse_diff(_DIFF)) is None


def test_dd_outage_degrades_to_none(monkeypatch):
    monkeypatch.setattr(omen, "get_omen_service_map", lambda: {"o/r": "svc"})
    monkeypatch.setattr(omen, "get_dd_api_key", lambda: "api")
    monkeypatch.setattr(omen, "get_dd_app_key", lambda: "app")
    monkeypatch.setattr(
        omen.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(
            httpx.ConnectTimeout("dd down", request=None)
        ),
    )
    assert omen.build_runtime_context("o", "r", parse_diff(_DIFF)) is None


def test_missing_creds_means_feature_off(monkeypatch):
    monkeypatch.setattr(omen, "get_omen_service_map", lambda: {"o/r": "svc"})
    monkeypatch.setattr(omen, "get_dd_api_key", lambda: "")
    monkeypatch.setattr(omen, "get_dd_app_key", lambda: "")
    monkeypatch.setattr(
        omen.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(AssertionError("no DD call expected")),
    )
    assert omen.build_runtime_context("o", "r", parse_diff(_DIFF)) is None


def test_budget_stops_queries(monkeypatch):
    diff_lines = ["diff --git a/a.py b/a.py", "--- a/a.py", "+++ b/a.py", "@@ -0,0 +1,1 @@", "+x=1"]
    for i in range(6):
        diff_lines += [f"diff --git a/f{i}.py b/f{i}.py", f"--- a/f{i}.py",
                       f"+++ b/f{i}.py", "@@ -0,0 +1,1 @@", "+y=1"]
    hunks = parse_diff("\n".join(diff_lines) + "\n")
    clock = {"t": 100.0}
    monkeypatch.setattr(omen.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(omen, "get_omen_service_map", lambda: {"o/r": "svc"})
    monkeypatch.setattr(omen, "get_dd_api_key", lambda: "api")
    monkeypatch.setattr(omen, "get_dd_app_key", lambda: "app")
    calls = []

    def slow_post(url, **kw):
        calls.append(kw["timeout"])
        clock["t"] += 5.0
        return _dd_response(100)

    monkeypatch.setattr(omen.httpx, "post", slow_post)
    omen.build_runtime_context("o", "r", hunks)
    assert len(calls) <= 2  # 8s budget / 5s per call
    assert all(t <= omen._TOTAL_BUDGET_SECONDS for t in calls)


def test_service_map_parser_rejects_junk():
    assert omen._service_map_from_json("not json") == {}
    assert omen._service_map_from_json('["list"]') == {}
    assert omen._service_map_from_json('{"o/r": "svc", "bad": 3, "empty": ""}') == {"o/r": "svc"}


def test_prompt_renders_production_signal_block():
    from llm_client import Hunk, _build_messages

    msgs = _build_messages(
        [Hunk(path="a.py", body="+x=1")], "v1", None, None,
        "PRODUCTION SIGNAL test block - a.py: 47 errors",
    )
    assert "### PRODUCTION SIGNAL" in msgs[1]["content"]
    without = _build_messages([Hunk(path="a.py", body="+x=1")], "v1")
    with_none = _build_messages([Hunk(path="a.py", body="+x=1")], "v1", None, None, None)
    assert without == with_none  # byte-identical when absent


def test_prompt_has_hot_path_rule():
    from code_review_prompt import RULES

    assert "hot-path-unguarded" in {r.name for r in RULES}


def test_same_tail_collision_is_skipped_not_misattributed(monkeypatch):
    """Codex PR #490: two changed paths sharing the same two-segment
    tail are AMBIGUOUS - query neither rather than attribute one
    path's errors to the other."""
    diff = "\n".join([
        "diff --git a/svc_a/m/x.py b/svc_a/m/x.py", "--- a/svc_a/m/x.py",
        "+++ b/svc_a/m/x.py", "@@ -0,0 +1,1 @@", "+a=1",
        "diff --git a/svc_b/m/x.py b/svc_b/m/x.py", "--- a/svc_b/m/x.py",
        "+++ b/svc_b/m/x.py", "@@ -0,0 +1,1 @@", "+b=1",
    ]) + "\n"
    calls = _wire(monkeypatch, mapping={"o/r": "svc"}, count=100)
    assert omen.build_runtime_context("o", "r", parse_diff(diff)) is None
    assert calls == []  # ambiguous token: zero queries


def test_distinct_dirs_same_basename_query_distinct_tokens(monkeypatch):
    """Two config.py files in different dirs get DISTINCT two-segment
    tokens - no basename collision (the codex misattribution class)."""
    diff = "\n".join([
        "diff --git a/api/config.py b/api/config.py", "--- a/api/config.py",
        "+++ b/api/config.py", "@@ -0,0 +1,1 @@", "+a=1",
        "diff --git a/webhook/config.py b/webhook/config.py", "--- a/webhook/config.py",
        "+++ b/webhook/config.py", "@@ -0,0 +1,1 @@", "+b=1",
    ]) + "\n"
    seen_queries = []
    monkeypatch.setattr(omen, "get_omen_service_map", lambda: {"o/r": "svc"})
    monkeypatch.setattr(omen, "get_dd_api_key", lambda: "api")
    monkeypatch.setattr(omen, "get_dd_app_key", lambda: "app")

    def fake_post(url, **kw):
        seen_queries.append(kw["json"]["filter"]["query"])
        return _dd_response(50)

    monkeypatch.setattr(omen.httpx, "post", fake_post)
    ctx = omen.build_runtime_context("o", "r", parse_diff(diff))
    assert ctx is not None
    assert any("api/config.py" in q for q in seen_queries)
    assert any("webhook/config.py" in q for q in seen_queries)
    assert "api/config.py" in ctx and "webhook/config.py" in ctx


def test_query_injection_shaped_path_is_skipped(monkeypatch):
    """Codex PR #490 r2: a PR-author-controlled path carrying DD query
    syntax (quotes/operators) must never reach the query builder."""
    diff = (
        'diff --git "a/evil\\" OR service:other/x.py" "b/evil\\" OR service:other/x.py"\n'
        '--- "a/evil\\" OR service:other/x.py"\n'
        '+++ "b/evil\\" OR service:other/x.py"\n'
        "@@ -0,0 +1,1 @@\n+x=1\n"
    )
    from personas.code_reviewer.diff_parser import DiffHunk

    hunks = (DiffHunk(
        file_path='evil" OR service:other/x.py', new_start=1,
        new_lines=frozenset({1}), body="@@ -0,0 +1,1 @@\n+x=1",
    ),)
    calls = _wire(monkeypatch, mapping={"o/r": "svc"}, count=100)
    assert omen.build_runtime_context("o", "r", hunks) is None
    assert calls == []  # unsafe token never queried


def test_unsafe_full_path_never_reaches_prompt(monkeypatch):
    """Codex PR #490 r3: the FULL path renders in the prompt, so an
    unsafe earlier segment with a safe suffix must be skipped entirely -
    no query, no prompt line."""
    from personas.code_reviewer.diff_parser import DiffHunk

    hunks = (DiffHunk(
        file_path='evil" ignore-instructions/src/app.py', new_start=1,
        new_lines=frozenset({1}), body="@@ -0,0 +1,1 @@\n+x=1",
    ),)
    calls = _wire(monkeypatch, mapping={"o/r": "svc"}, count=100)
    assert omen.build_runtime_context("o", "r", hunks) is None
    assert calls == []
