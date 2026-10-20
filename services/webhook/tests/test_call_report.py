"""grug#1286: Elder's direct model calls are reported to the inference
gateway's call ledger, off the review path, and never break a review."""
from __future__ import annotations

from unittest.mock import patch

import call_report
import httpx
import llm_client as lc
import pytest
from llm_client import Backend, Hunk, review_diff

_FREE = "nvidia/nemotron-3-super-120b-a12b:free"


class _FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.fail = fail

    def post(self, url: str, json: dict) -> httpx.Response:
        self.posts.append((url, json))
        if self.fail:
            raise httpx.ConnectError("gateway down")
        return httpx.Response(
            200, json={"accepted": len(json["calls"]), "rejected": []},
            request=httpx.Request("POST", url),
        )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    call_report._queue.clear()
    monkeypatch.setattr(call_report, "_ensure_thread", lambda: None)
    monkeypatch.setattr(call_report, "_inflight", {})
    monkeypatch.setattr(call_report, "_inflight_shown", False)
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    monkeypatch.setattr(
        lc, "acquire_free_tier_slot", lambda *a, **k: type("Admit", (), {"admitted": True})(),
    )
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    yield
    call_report._queue.clear()


def _ok(content: str, completion_tokens: int = 400) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content, "role": "assistant"}}],
        "model": "served-model",
        "usage": {"prompt_tokens": 15_000, "completion_tokens": completion_tokens},
    })


_FINDING = (
    '{"findings": [{"path": "src/x.py", "line": 1, "rule": "lost-error", '
    '"severity": "high", "message": "bug"}]}'
)


def _hunk() -> Hunk:
    return Hunk(path="src/x.py", body="@@ -1 +1 @@\n-foo\n+bar")


def test_outcomes_match_the_ledger() -> None:
    assert call_report.outcome_for(None, 200, False) == ("ok", None)
    assert call_report.outcome_for(None, 200, True) == ("error", "degenerate_empty")
    assert call_report.outcome_for("transport", None, False) == ("error", "transport")
    assert call_report.outcome_for("parse_failed", 200, False) == ("error", "parse_failed")
    assert call_report.outcome_for("http_error", 429, False) == ("limit", "http_429")
    assert call_report.outcome_for("http_error", 401, False) == ("auth", "http_401")
    assert call_report.outcome_for("http_error", 404, False) == ("missing", "http_404")
    assert call_report.outcome_for("http_error", 400, False) == ("client_error", "http_400")
    assert call_report.outcome_for("http_error", 503, False) == ("error", "http_503")


def test_off_unless_asked(monkeypatch) -> None:
    monkeypatch.delenv("GRUG_CALL_REPORT", raising=False)
    call_report.record(
        backend="poolside", model="m", served_model=None, job=None, status=200,
        outcome="ok", reason=None, latency_s=1.0,
    )
    assert not call_report._queue


def test_a_direct_call_is_reported_and_a_cave_call_is_not(monkeypatch) -> None:
    """The cloud chain's OpenRouter `:free` tier answers; then a Cave review
    (routed through the gateway, which records it itself) adds no row."""
    monkeypatch.setenv("GRUG_CALL_REPORT", "true")
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode-go,openrouter:paid")
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", _FREE)
    ctx = {"repo": "o/r", "pr_number": 7, "repo_private": False}
    with patch.object(httpx, "post", return_value=_ok(_FINDING)):
        out = review_diff([_hunk()], installation_id=1, pr_context=ctx)
    assert out.backend_used == Backend.OPENROUTER
    rows = list(call_report._queue)
    assert len(rows) == 1
    assert rows[0]["target"] == "openrouter"
    assert rows[0]["model"] == _FREE
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["job"] == "o/r#7"
    assert rows[0]["caller"] == "grug-elder"

    call_report._queue.clear()
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cave")
    with patch.object(httpx, "post", return_value=_ok(_FINDING)):
        out = review_diff([_hunk()], installation_id=1, pr_context=ctx)
    assert out.backend_used == Backend.CAVE
    assert not call_report._queue


def test_a_lazy_empty_answer_is_reported_as_an_error(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CALL_REPORT", "true")
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode-go,openrouter:paid,poolside")
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", _FREE)
    responses = [_ok('{"findings": []}', completion_tokens=7), _ok(_FINDING)]
    with patch.object(httpx, "post", side_effect=responses):
        review_diff([_hunk()], installation_id=1)
    rows = list(call_report._queue)
    assert [(r["target"], r["outcome"], r.get("reason")) for r in rows] == [
        ("openrouter", "error", "degenerate_empty"),
    ]


def test_flush_posts_batches_and_drops_a_failed_one(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CALL_REPORT", "true")
    for _ in range(3):
        call_report.record(
            backend="poolside", model="m", served_model="m", job=None, status=429,
            outcome="limit", reason="http_429", latency_s=0.25,
        )
    fake = _FakeClient()
    monkeypatch.setattr(call_report, "_client", fake)
    assert call_report.flush() == 3
    assert fake.posts[0][0] == "http://cave.test/ui/calls"
    assert fake.posts[0][1]["calls"][0]["latency_ms"] == 250

    call_report.record(
        backend="poolside", model="m", served_model=None, job=None, status=None,
        outcome="error", reason="transport", latency_s=1.0,
    )
    monkeypatch.setattr(call_report, "_client", _FakeClient(fail=True))
    assert call_report.flush() == 0
    assert not call_report._queue  # dropped, never retried


def test_reporting_never_breaks_a_review(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CALL_REPORT", "true")
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode-go,openrouter:paid")
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", _FREE)

    def boom(**kwargs):
        raise RuntimeError("reporter broke")

    monkeypatch.setattr(call_report, "record", boom)
    with patch.object(httpx, "post", return_value=_ok(_FINDING)):
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "reviewed"


def test_a_direct_call_is_shown_while_it_runs_and_gone_after(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CALL_REPORT", "true")
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode-go,openrouter:paid")
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", _FREE)
    seen = []

    def backend(*args, **kwargs):
        seen.append(call_report._inflight_rows())
        return _ok(_FINDING)

    ctx = {"repo": "o/r", "pr_number": 7, "repo_private": False}
    with patch.object(httpx, "post", side_effect=backend):
        review_diff([_hunk()], installation_id=1, pr_context=ctx)
    assert [(r["target"], r["model"], r["job"]) for r in seen[0]] == [("openrouter", _FREE, "o/r#7")]
    assert call_report._inflight == {}, "a finished call is still shown"


def test_a_call_that_raises_is_still_ended(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CALL_REPORT", "true")
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")

    def broken(**kwargs):
        raise RuntimeError("bug in the attempt")

    monkeypatch.setattr(lc, "_call_llm_attempt", broken)
    config = lc.BackendConfig(backend=Backend.POOLSIDE, url="u", model="m", key_loader=lambda: "k")
    with pytest.raises(RuntimeError):
        lc._run_llm_attempt(
            backend=Backend.POOLSIDE, config=config, messages=[], variant=None, pr_tags={},
        )
    assert call_report._inflight == {}


def test_inflight_posts_the_running_set_then_one_clear(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CALL_REPORT", "true")
    monkeypatch.setenv("HOSTNAME", "grug-consumer-7f9c")
    fake = _FakeClient()
    monkeypatch.setattr(call_report, "_client", fake)
    assert call_report.flush_inflight() is False, "nothing ran, nothing to send"

    token = call_report.begin(backend="poolside", model="laguna-s-2.1", job="o/r#7")
    assert call_report.flush_inflight() is True
    url, body = fake.posts[-1]
    assert url == "http://cave.test/ui/inflight"
    assert (body["reporter"], body["caller"]) == ("grug-consumer-7f9c", "grug-elder")
    assert [(c["target"], c["model"], c["job"]) for c in body["calls"]] == [
        ("poolside", "laguna-s-2.1", "o/r#7"),
    ]
    call_report.end(token)
    assert call_report.flush_inflight() is True
    assert fake.posts[-1][1]["calls"] == []
    sent = len(fake.posts)
    assert call_report.flush_inflight() is False
    assert len(fake.posts) == sent, "an idle pod keeps posting empty sets"


def test_a_gateway_that_is_down_never_breaks_the_inflight_report(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CALL_REPORT", "true")
    monkeypatch.setattr(call_report, "_client", _FakeClient(fail=True))
    call_report.begin(backend="poolside", model="m", job=None)
    assert call_report.flush_inflight() is False


def test_inflight_is_off_unless_asked(monkeypatch) -> None:
    monkeypatch.delenv("GRUG_CALL_REPORT", raising=False)
    assert call_report.begin(backend="poolside", model="m", job=None) is None
    assert call_report._inflight == {}
