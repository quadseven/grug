"""Tests for personas/code_reviewer/cross_file.py (#468 tracer).

Symbol extraction is pure; the fetcher is exercised against mocked GitHub
code-search + contents responses. The load-bearing contract is FAIL-SAFE:
any error path degrades to {} (today's diff-only review), never raises.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx

from personas.code_reviewer import cross_file
from personas.code_reviewer.diff_parser import parse_diff


_DIFF_CHANGED_SIG = """diff --git a/src/api.py b/src/api.py
--- a/src/api.py
+++ b/src/api.py
@@ -1,4 +1,5 @@
 import os
-def fetch_user(user_id):
+def fetch_user(user_id, *, tenant):
+    validate_tenant(tenant)
     return db.get(user_id)
"""


def test_extract_symbols_finds_changed_def_and_external_call():
    hunks = parse_diff(_DIFF_CHANGED_SIG)
    symbols = cross_file.extract_symbols(hunks)
    # The changed def (callers elsewhere may break) comes FIRST,
    # then the called-but-not-defined-here name (its definition matters).
    assert symbols[0] == "fetch_user"
    assert "validate_tenant" in symbols


def test_extract_symbols_skips_locally_defined_calls():
    """A name both DEFINED and CALLED inside the diff needs no cross-file
    lookup - its definition is already in context."""
    diff = """diff --git a/src/m.py b/src/m.py
--- a/src/m.py
+++ b/src/m.py
@@ -1,2 +1,5 @@
 import os
+def helper(x):
+    return x
+def main():
+    return helper(1)
"""
    symbols = cross_file.extract_symbols(parse_diff(diff))
    # helper is defined in-diff -> not a cross-file CALL target; it still
    # appears once as a DEF (its callers elsewhere may exist).
    assert symbols.count("helper") == 1


def test_extract_symbols_ignores_builtins_and_caps():
    diff_lines = ["diff --git a/src/big.py b/src/big.py",
                  "--- a/src/big.py", "+++ b/src/big.py", "@@ -0,0 +1,30 @@"]
    # 20 distinct external calls + builtin noise
    for i in range(20):
        diff_lines.append(f"+    external_call_{i}(print(len(str(x))))")
    symbols = cross_file.extract_symbols(parse_diff("\n".join(diff_lines) + "\n"))
    assert len(symbols) <= cross_file._MAX_SYMBOLS
    assert "print" not in symbols and "len" not in symbols and "str" not in symbols


def test_extract_symbols_empty_diff():
    assert cross_file.extract_symbols(()) == ()


def _search_response(paths):
    return httpx.Response(
        200,
        json={"items": [{"path": p} for p in paths]},
        request=httpx.Request("GET", "https://api.github.com/search/code"),
    )


def _raw_response(text):
    return httpx.Response(
        200, text=text,
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/contents/x"),
    )


def test_fetch_cross_file_context_returns_unchanged_files():
    """Search discovers paths (default-branch index); content fetches at
    head_sha; changed paths are excluded (already in #336 context)."""
    def fake_get(url, **kw):
        if "/search/code" in url:
            return _search_response(["src/caller.py", "src/api.py"])
        return _raw_response("def caller():\n    fetch_user(1)\n")

    with patch.object(cross_file.httpx, "get", side_effect=fake_get):
        out = cross_file.fetch_cross_file_context(
            "tok", "o", "r", ("fetch_user",),
            head_sha="abc123", exclude_paths=frozenset({"src/api.py"}),
        )
    assert list(out) == ["src/caller.py"]  # changed file excluded
    assert "fetch_user(1)" in out["src/caller.py"]


def test_fetch_cross_file_context_fail_safe_on_search_error():
    def fake_get(url, **kw):
        raise httpx.ConnectTimeout("search down", request=None)

    with patch.object(cross_file.httpx, "get", side_effect=fake_get):
        out = cross_file.fetch_cross_file_context(
            "tok", "o", "r", ("fetch_user",),
            head_sha="abc123", exclude_paths=frozenset(),
        )
    assert out == {}


def test_fetch_cross_file_context_partial_on_content_error():
    """A per-file content failure skips THAT file only (same degrade
    contract as #336's _fetch_file_contents)."""
    calls = {"n": 0}

    def fake_get(url, **kw):
        if "/search/code" in url:
            return _search_response(["src/a.py", "src/b.py"])
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("blip", request=None)
        return _raw_response("content-b")

    with patch.object(cross_file.httpx, "get", side_effect=fake_get):
        out = cross_file.fetch_cross_file_context(
            "tok", "o", "r", ("sym",),
            head_sha="abc123", exclude_paths=frozenset(),
        )
    assert list(out) == ["src/b.py"]


def test_fetch_cross_file_context_respects_budgets():
    """At most _MAX_SYMBOLS search calls and _MAX_FILES content files;
    oversized files are dropped."""
    search_calls = {"n": 0}

    def fake_get(url, **kw):
        if "/search/code" in url:
            search_calls["n"] += 1
            return _search_response([f"src/f{search_calls['n']}_{i}.py" for i in range(10)])
        return _raw_response("x" * (cross_file._MAX_FILE_BYTES + 1))

    many_symbols = tuple(f"sym{i}" for i in range(20))
    with patch.object(cross_file.httpx, "get", side_effect=fake_get):
        out = cross_file.fetch_cross_file_context(
            "tok", "o", "r", many_symbols,
            head_sha="abc123", exclude_paths=frozenset(),
        )
    assert search_calls["n"] <= cross_file._MAX_SYMBOLS
    assert out == {}  # every file over the byte cap -> dropped


def test_fetch_no_symbols_no_calls():
    with patch.object(cross_file.httpx, "get", side_effect=AssertionError("no call expected")):
        assert cross_file.fetch_cross_file_context(
            "tok", "o", "r", (), head_sha="a", exclude_paths=frozenset(),
        ) == {}


# ── integration: dispatch + prompt rendering ──────────────────────────


def test_build_messages_renders_cross_file_block():
    from llm_client import Hunk, _build_messages

    msgs = _build_messages(
        [Hunk(path="src/api.py", body="+def f(x, *, t):")],
        "v1",
        None,
        {"src/caller.py": "def g():\n    f(1)\n"},
    )
    user = msgs[1]["content"]
    assert "src/caller.py (UNCHANGED — cross-file context)" in user
    assert "do not flag lines in it" in user
    assert "1: def g():" in user


def test_build_messages_byte_identical_when_no_cross_file():
    from llm_client import Hunk, _build_messages

    hunks = [Hunk(path="src/api.py", body="+x = 1")]
    without = _build_messages(hunks, "v1")
    with_empty = _build_messages(hunks, "v1", None, {})
    with_none = _build_messages(hunks, "v1", None, None)
    assert without == with_empty == with_none


def test_build_messages_skips_changed_paths_and_oversized():
    from llm_client import _MAX_FILE_CONTEXT_LINES, Hunk, _build_messages

    huge = "\n".join("x" for _ in range(_MAX_FILE_CONTEXT_LINES + 1))
    msgs = _build_messages(
        [Hunk(path="src/api.py", body="+x = 1")],
        "v1",
        None,
        {"src/api.py": "already changed", "src/big.py": huge},
    )
    user = msgs[1]["content"]
    # The changed path renders as a hunk, never doubled as cross-file ctx;
    # the oversized file is dropped by the line budget.
    assert "cross-file context" not in user


def test_prompt_has_caller_not_updated_rule():
    from code_review_prompt import RULES

    names = {r.name for r in RULES}
    assert "caller-not-updated" in names


def test_dispatch_threads_cross_file_context_to_review(monkeypatch):
    """The dispatch fetches cross-file context and passes it into
    review_diff; a fetch failure degrades to {} without breaking the
    review (fail-safe acceptance criterion)."""
    from unittest.mock import MagicMock

    from llm_client import Backend, LlmReviewResponse
    from personas.code_reviewer import dispatch as cr_dispatch

    captured: dict = {}

    def fake_review_diff(hunks, installation_id, pr_context=None,
                         file_contents=None, cross_file_contents=None):
        captured["xf"] = cross_file_contents
        return LlmReviewResponse(kind="reviewed", findings=(), backend_used=Backend.POOLSIDE)

    monkeypatch.setattr(cr_dispatch, "review_diff", fake_review_diff)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(cr_dispatch, "record_check_verdict", lambda **kw: None)
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda iid, fn: fn("tok"),
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_pr_diff",
        lambda token, o, r, n: "diff --git a/src/api.py b/src/api.py\n"
        "--- a/src/api.py\n+++ b/src/api.py\n@@ -1,1 +1,2 @@\n context\n"
        "+def fetch_user(user_id, *, tenant):\n",
    )
    monkeypatch.setattr(cr_dispatch, "_fetch_file_contents", lambda *a: {})
    monkeypatch.setattr(
        cr_dispatch, "fetch_cross_file_context",
        lambda token, o, r, syms, head_sha, exclude_paths: {"src/caller.py": "fetch_user(1)"},
    )

    payload = {
        "action": "opened",
        "pull_request": {"number": 7, "head": {"sha": "abc"}, "body": ""},
        "repository": {"name": "r", "owner": {"login": "o"}},
        "installation": {"id": 1},
    }
    out = cr_dispatch.dispatch_code_review(payload, blocking=False)
    assert out["result"] == "pass"
    assert captured["xf"] == {"src/caller.py": "fetch_user(1)"}


def test_dispatch_cross_file_failure_degrades_to_diff_only(monkeypatch):
    from llm_client import Backend, LlmReviewResponse
    from personas.code_reviewer import dispatch as cr_dispatch

    captured: dict = {}

    def fake_review_diff(hunks, installation_id, pr_context=None,
                         file_contents=None, cross_file_contents=None):
        captured["xf"] = cross_file_contents
        return LlmReviewResponse(kind="reviewed", findings=(), backend_used=Backend.POOLSIDE)

    monkeypatch.setattr(cr_dispatch, "review_diff", fake_review_diff)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(cr_dispatch, "record_check_verdict", lambda **kw: None)
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda iid, fn: fn("tok"),
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_pr_diff",
        lambda token, o, r, n: "diff --git a/src/api.py b/src/api.py\n"
        "--- a/src/api.py\n+++ b/src/api.py\n@@ -1,1 +1,2 @@\n context\n"
        "+def fetch_user(user_id, *, tenant):\n",
    )
    monkeypatch.setattr(cr_dispatch, "_fetch_file_contents", lambda *a: {})

    def _boom(*a, **kw):
        raise RuntimeError("code-search exploded")

    monkeypatch.setattr(cr_dispatch, "fetch_cross_file_context", _boom)

    payload = {
        "action": "opened",
        "pull_request": {"number": 7, "head": {"sha": "abc"}, "body": ""},
        "repository": {"name": "r", "owner": {"login": "o"}},
        "installation": {"id": 1},
    }
    out = cr_dispatch.dispatch_code_review(payload, blocking=False)
    assert out["result"] == "pass"          # review still ran
    assert captured["xf"] == {}             # degraded to diff-only


def test_fetch_stops_at_global_wall_clock_budget(monkeypatch):
    """Codex peer-review HIGH (PR #480): per-call timeouts alone allow
    ~100s of slow-but-not-failing responses. Simulate a clock advancing
    5s per HTTP call: the 8s global budget must stop the phase after ~2
    calls and degrade to what was collected - never run all 10."""
    clock = {"t": 100.0}

    def fake_monotonic():
        return clock["t"]

    http_calls = {"n": 0}

    def fake_get(url, **kw):
        http_calls["n"] += 1
        clock["t"] += 5.0  # each call is slow but under the per-call timeout
        if "/search/code" in url:
            return _search_response([f"src/hit_{http_calls['n']}_{i}.py" for i in range(5)])
        return _raw_response("content")

    monkeypatch.setattr(cross_file.time, "monotonic", fake_monotonic)
    with patch.object(cross_file.httpx, "get", side_effect=fake_get):
        out = cross_file.fetch_cross_file_context(
            "tok", "o", "r", tuple(f"sym{i}" for i in range(5)),
            head_sha="abc", exclude_paths=frozenset(),
        )
    # Budget 8s / 5s per call -> at most 2 calls started before the
    # deadline tripped, and the function returned (partial or empty)
    # instead of burning the full 10-call worst case.
    assert http_calls["n"] <= 2
    assert isinstance(out, dict)


def test_per_call_timeout_clamped_to_remaining_budget(monkeypatch):
    """Codex round 2 (PR #480): a request started near the deadline must
    receive only the REMAINING budget as its timeout, never the full
    per-call default."""
    clock = {"t": 100.0}
    monkeypatch.setattr(cross_file.time, "monotonic", lambda: clock["t"])

    seen_timeouts: list[float] = []

    def fake_get(url, **kw):
        seen_timeouts.append(kw["timeout"])
        clock["t"] += 6.0  # slow call: 6s of the 8s budget gone
        if "/search/code" in url:
            return _search_response(["src/a.py"])
        return _raw_response("content")

    with patch.object(cross_file.httpx, "get", side_effect=fake_get):
        cross_file.fetch_cross_file_context(
            "tok", "o", "r", ("s1", "s2"),
            head_sha="abc", exclude_paths=frozenset(),
        )
    # First call gets min(10, 8) = 8; every later call gets the remaining
    # ~2s, never the full 10s default.
    assert seen_timeouts[0] <= cross_file._TOTAL_BUDGET_SECONDS
    assert all(t < cross_file._SEARCH_TIMEOUT for t in seen_timeouts[1:])
