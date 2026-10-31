"""grug#1225: switched-off LLM backends are never called, and an auth or
quota refusal stops calls to that provider for the rest of the review.

`GRUG_DISABLED_BACKENDS` is the operator's switch; the provider breaker is
the automatic half for a provider nobody has switched off yet."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args
from unittest.mock import patch

import httpx
import llm_client as lc
import pytest
from llm_client import Backend, Hunk, review_diff
from review_evidence import assemble_evidence

_GO_URL = "opencode.ai"
_OR_URL = "openrouter.ai"
_POOLSIDE_URL = "poolside.ai"
_CAVE_URL = "cave.test"
_FREE = "nvidia/nemotron-3-super-120b-a12b:free"
_MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    monkeypatch.setattr(lc, "_opencode_go_blocked_until", 0.0, raising=False)
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    # The OpenRouter :free limiter talks to Postgres; admit every call here.
    monkeypatch.setattr(
        lc, "acquire_free_tier_slot",
        lambda *a, **k: type("Admit", (), {"admitted": True})(),
    )
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    # The production chain: three OpenCode Go models, OpenRouter :free, Poolside.
    monkeypatch.setenv("GRUG_OPENCODE_GO_API_KEY_SSM", "/test/opencode-go")
    monkeypatch.setenv("GRUG_OPENCODE_GO_SECOND_MODEL", "mimo-v2.5")
    monkeypatch.setenv("GRUG_OPENCODE_GO_FALLBACK_MODEL", "longcat-2.5-preview-free")
    monkeypatch.setenv("GRUG_CLOUD_FREE_TIER_MODEL", _FREE)


def _ok(content: str = '{"findings": []}') -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content, "role": "assistant"}}],
        "model": "m",
    })


def _finding_response() -> httpx.Response:
    return _ok(
        '{"findings": [{"path": "src/x.py", "line": 1, "rule": "lost-error", '
        '"severity": "high", "message": "bug"}]}'
    )


def _hunk() -> Hunk:
    return Hunk(path="src/x.py", body="@@ -1 +1 @@\n-foo\n+bar")


class _Recorder:
    """Stands in for `httpx.post`: records every URL, answers per host."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.urls: list[str] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        for host, answer in self.answers.items():
            if host in url:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise httpx.ConnectError(f"no fake for {url}")

    def hosts(self) -> set[str]:
        return {
            h for h in (_GO_URL, _OR_URL, _POOLSIDE_URL, _CAVE_URL)
            if any(h in u for u in self.urls)
        }


# --- the switch: GRUG_DISABLED_BACKENDS -------------------------------------


def test_unset_switch_disables_nothing(monkeypatch) -> None:
    monkeypatch.delenv("GRUG_DISABLED_BACKENDS", raising=False)
    assert [t.backend for t in lc._cloud_chain_tiers()] == [
        Backend.OPENCODE_GO, Backend.OPENCODE_GO, Backend.OPENCODE_GO,
        Backend.OPENROUTER, Backend.POOLSIDE,
    ]
    assert lc.select_backend(1) == Backend.OPENROUTER


def test_disabled_backends_leave_the_cloud_chain(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode-go, OpenRouter")
    assert [t.backend for t in lc._cloud_chain_tiers()] == [Backend.POOLSIDE]


def test_a_pool_entry_disables_only_that_pool(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "openrouter:paid,opencode-go:free")
    assert [(t.backend, t.model) for t in lc._cloud_chain_tiers()] == [
        (Backend.OPENCODE_GO, lc._OPENCODE_GO_MODEL),
        (Backend.OPENCODE_GO, "mimo-v2.5"),
        (Backend.OPENROUTER, _FREE),
        (Backend.POOLSIDE, lc._POOLSIDE_MODEL),
    ]
    assert lc._usable_saas_fallback_backends() == (Backend.POOLSIDE,)


def _shipped_env(name: str) -> str:
    """The value every k8s manifest ships for `name`."""
    root = Path(__file__).resolve().parents[3] / "k8s"
    found = {
        m.group(1)
        for manifest in ("consumer-deployment.yaml", "webhook-deployment.yaml", "poller-cronjob.yaml")
        for m in re.finditer(
            r'name: ' + name + r', value: "([^"]*)"', (root / manifest).read_text()
        )
    }
    assert len(found) == 1, found
    return found.pop()


def test_the_shipped_chain_is_free_cloud_tiers_only_then_the_cave(monkeypatch) -> None:
    """grug#1269: the operator wants the free cloud models used first.
    OpenRouter's free pool leads, then Poolside, then the gateway's free
    provider pool, and only then the Cave. Nothing paid can be called:
    OpenCode Go is off and OpenRouter's paid pool is off."""
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", _shipped_env("GRUG_DISABLED_BACKENDS"))
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", _shipped_env("GRUG_CLOUD_FREE_POOL"))
    assert [(t.backend, t.model) for t in lc._cloud_chain_tiers()] == [
        (Backend.OPENROUTER, _FREE),
        (Backend.POOLSIDE, lc._POOLSIDE_MODEL),
        (Backend.FREE_POOL, "free-pool"),
    ]
    assert not lc._backend_enabled(Backend.OPENCODE_GO, "longcat-2.5-preview-free")
    assert not lc._backend_enabled(Backend.OPENROUTER, "anthropic/claude-opus-4.7")
    assert lc._backend_enabled(Backend.OPENROUTER, _FREE)


def test_the_shipped_deep_escalation_is_a_mode_the_client_accepts(monkeypatch, caplog) -> None:
    """grug#1487: GRUG_DEEP_ESCALATION ships as on_demand while the Sparks
    are away. An unknown value falls back to auto with a warning, which
    would silently undo the switch, so the shipped value must be a mode
    `_deep_escalation_mode` takes as-is."""
    root = Path(__file__).resolve().parents[3] / "k8s"
    per_manifest = {
        manifest: re.findall(
            r'name: GRUG_DEEP_ESCALATION, value: "?([^"}\s]*)"?\}',
            (root / manifest).read_text(),
        )
        for manifest in ("consumer-deployment.yaml", "webhook-deployment.yaml")
    }
    assert all(len(v) == 1 for v in per_manifest.values()), per_manifest
    assert len({v[0] for v in per_manifest.values()}) == 1, per_manifest
    shipped = _shipped_env("GRUG_DEEP_ESCALATION")
    assert shipped in get_args(lc.DeepEscalationMode)
    monkeypatch.setenv("GRUG_DEEP_ESCALATION", shipped)
    with caplog.at_level("WARNING"):
        assert lc._deep_escalation_mode() == shipped
    assert not [r for r in caplog.records if r.getMessage() == "deep_escalation_mode_invalid"]


def test_the_free_pool_tier_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("GRUG_CLOUD_FREE_POOL", raising=False)
    assert lc._free_pool_chain_config() is None
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", "true")
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL")
    assert lc._free_pool_chain_config() is None


def test_the_free_pool_sends_a_private_repo_only_to_private_safe_providers(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", "true")
    public = {"repo": "o/r", "pr_number": 1, "repo_private": False}
    with lc._spark_job_scope(public):
        assert lc._free_pool_chain_config().url == "http://cave.test/free-public/v1/chat/completions"
    for ctx in ({"repo": "o/r", "pr_number": 1, "repo_private": True}, {}, None):
        with lc._spark_job_scope(ctx):
            assert lc._free_pool_chain_config().url == "http://cave.test/free/v1/chat/completions"


def test_the_free_pool_answers_before_any_spark_is_asked(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode-go,openrouter:paid")
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", "true")
    fake = _Recorder({
        _OR_URL: httpx.ConnectTimeout("openrouter down"),
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        "cave.test/free/": _finding_response(),
    })
    with patch.object(httpx, "post", side_effect=fake):
        out = review_diff(assemble_evidence([_hunk()]), installation_id=1)
    assert out.kind == "reviewed"
    assert out.backend_used == Backend.FREE_POOL
    assert [u for u in fake.urls if "cave.test" in u] == [
        "http://cave.test/free/v1/chat/completions"
    ]


def _lazy_empty() -> httpx.Response:
    """`{"findings": []}` in 7 tokens for a 15k-token cohort, as OpenRouter's
    `:free` model and the free pool's gemma4 answered on 2026-10-19."""
    body = {
        "choices": [{"message": {"content": '{"findings": []}', "role": "assistant"}}],
        "model": "m",
        "usage": {"prompt_tokens": 15_000, "completion_tokens": 7},
    }
    return httpx.Response(200, json=body)


def test_free_tier_empties_never_pass_a_pr_the_sparks_review_it(monkeypatch) -> None:
    """Live 2026-10-19, the shipped order: OpenRouter `:free` answered empty
    first and Poolside empty second, and the old check (second voter only)
    called it consensus, so PRs passed clean without a Spark. Neither the
    `:free` model nor the free pool may vote; the Cave reviews it."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode-go,openrouter:paid")
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", "true")
    fake = _Recorder({
        _OR_URL: _lazy_empty(),
        _POOLSIDE_URL: _lazy_empty(),
        "cave.test/free/": _lazy_empty(),
        _CAVE_URL: _finding_response(),
    })
    with patch.object(httpx, "post", side_effect=fake):
        out = review_diff(assemble_evidence([_hunk()]), installation_id=1)
    assert out.kind == "reviewed"
    assert out.backend_used == Backend.CAVE
    assert len(out.findings) == 1
    assert fake.urls[-1] == "http://cave.test/v1/chat/completions"


def test_the_free_vote_rule_is_symmetric() -> None:
    free = (Backend.OPENROUTER, _FREE)
    pool = (Backend.FREE_POOL, "free-pool")
    poolside = (Backend.POOLSIDE, lc._POOLSIDE_MODEL)
    go = (Backend.OPENCODE_GO, "deepseek-v4.1-flash")
    for a, b in ((free, poolside), (poolside, free), (pool, poolside), (poolside, pool)):
        assert not lc._empty_answers_agree(a, b, None), (a, b)
    assert lc._empty_answers_agree(poolside, go, None)


def test_cloud_review_never_calls_a_disabled_backend(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode-go,openrouter")
    fake = _Recorder({_POOLSIDE_URL: _finding_response()})
    with patch.object(httpx, "post", side_effect=fake):
        out = review_diff(assemble_evidence([_hunk()]), installation_id=1)
    assert out.kind == "reviewed"
    assert out.backend_used == Backend.POOLSIDE
    assert fake.hosts() == {_POOLSIDE_URL}
    assert len(fake.urls) == 1


def test_saas_overload_fallback_never_calls_a_disabled_backend(monkeypatch) -> None:
    """Cave down, Poolside down: OpenRouter would be the last try, but it is
    switched off, so the review ends without calling it."""
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "openrouter")
    fake = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OR_URL: _finding_response(),
    })
    with patch.object(httpx, "post", side_effect=fake):
        out = review_diff(assemble_evidence([_hunk()]), installation_id=1)
    assert out.kind == "all_failed"
    assert _OR_URL not in fake.hosts()
    assert _POOLSIDE_URL in fake.hosts()


def test_judge_moves_off_a_disabled_backend(monkeypatch) -> None:
    """Odd installs pick OpenRouter for the judge; switched off, Poolside
    answers instead."""
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "openrouter")
    assert lc.select_backend(1) == Backend.POOLSIDE
    verdicts = _ok('{"verdicts": [{"index": 0, "is_real_bug": true, "reasoning": "r"}]}')
    fake = _Recorder({_POOLSIDE_URL: verdicts})
    findings = [{"rule_name": "r", "file": "src/x.py", "line": 1, "message": "m"}]
    with patch.object(httpx, "post", side_effect=fake):
        out = lc.judge_findings(findings, assemble_evidence([_hunk()]), installation_id=1)
    assert len(out) == 1
    assert fake.hosts() == {_POOLSIDE_URL}


def test_judge_with_every_backend_disabled_makes_no_call(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "openrouter,poolside")
    findings = [{"rule_name": "r", "file": "src/x.py", "line": 1, "message": "m"}]
    with patch.object(httpx, "post") as post:
        assert lc.judge_findings(findings, assemble_evidence([_hunk()]), installation_id=1) == ()
    post.assert_not_called()


def test_interactive_order_leaves_out_disabled_backends(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode-go,openrouter")
    assert lc._interactive_backend_order(1) == (Backend.POOLSIDE,)
    assert lc._interactive_backend_order(2) == (Backend.POOLSIDE,)
    payload = json.dumps({"summary": "s", "file_summaries": {}, "effort": "trivial"})
    fake = _Recorder({_POOLSIDE_URL: _ok(payload)})
    with patch.object(httpx, "post", side_effect=fake):
        assert lc.summarize_pr("diff --git a/x b/x\n", ["x"], installation_id=1) is not None
    assert fake.hosts() == {_POOLSIDE_URL}


def test_call_backend_refuses_a_disabled_backend_without_a_request(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "openrouter:free")
    free = lc.replace(lc._BACKEND_CONFIGS[Backend.OPENROUTER], model=_FREE)
    with patch.object(httpx, "post", return_value=_ok()) as post:
        with pytest.raises(lc.BackendDisabledError):
            lc._call_backend(free, _MESSAGES)
        post.assert_not_called()
        # The paid pool is still on.
        lc._call_backend(lc._BACKEND_CONFIGS[Backend.OPENROUTER], _MESSAGES)
    assert post.call_count == 1


def test_a_typo_in_the_switch_is_logged(monkeypatch, caplog) -> None:
    monkeypatch.setattr(lc, "_warned_disabled_entries", set())
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "opencode_go,openrouter:cheap")
    with caplog.at_level("WARNING"):
        lc._backend_enabled(Backend.OPENCODE_GO, "m")
    warned = sorted(
        r.entry for r in caplog.records
        if r.getMessage() == "grug_disabled_backends_unknown_entry"
    )
    assert warned == ["opencode_go", "openrouter:cheap"]
    assert lc._backend_enabled(Backend.OPENCODE_GO, "m")


# --- the provider breaker ---------------------------------------------------


def _err(status: int, message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}}, headers=headers)


def _go(model: str) -> lc.BackendConfig:
    return lc.replace(lc._opencode_go_chain_config(), model=model)


def _or(model: str) -> lc.BackendConfig:
    return lc.replace(lc._BACKEND_CONFIGS[Backend.OPENROUTER], model=model, retry_attempts=1)


def test_a_401_stops_every_call_to_that_provider(monkeypatch) -> None:
    """The expired OpenCode Go key: one 401 on the primary, and neither the
    second paid model nor the free one is called again."""
    with patch.object(httpx, "post", return_value=_err(401, "API key expired")) as post:
        lc._call_backend(_go(lc._OPENCODE_GO_MODEL), _MESSAGES)
        for model in ("mimo-v2.5", "longcat-2.5-preview-free"):
            with pytest.raises(lc.BackendDisabledError):
                lc._call_backend(_go(model), _MESSAGES)
    assert post.call_count == 1
    assert all(t.backend != Backend.OPENCODE_GO for t in lc._cloud_chain_tiers())
    # Other providers are untouched.
    assert [t.backend for t in lc._cloud_chain_tiers()] == [
        Backend.OPENROUTER, Backend.POOLSIDE,
    ]


def test_a_spent_paid_limit_blocks_only_the_paid_pool() -> None:
    """OpenRouter's key has a $0 limit: paid models refuse, free ones answer."""
    refusal = _err(403, "Key limit exceeded (total limit)")
    with patch.object(httpx, "post", return_value=refusal) as post:
        lc._call_backend(_or("anthropic/claude-haiku-4.5"), _MESSAGES)
        with pytest.raises(lc.BackendDisabledError):
            lc._call_backend(_or("anthropic/claude-haiku-4.5"), _MESSAGES)
    assert post.call_count == 1
    assert lc._provider_available(Backend.OPENROUTER, _FREE)
    assert lc._usable_saas_fallback_backends() == (Backend.POOLSIDE,)
    assert lc.select_backend(1) == Backend.POOLSIDE


def test_a_402_trips_the_breaker() -> None:
    with patch.object(httpx, "post", return_value=_err(402, "Insufficient credits")):
        lc._call_backend(_or("anthropic/claude-haiku-4.5"), _MESSAGES)
    assert not lc._provider_available(Backend.OPENROUTER, "anthropic/claude-haiku-4.5")


def test_the_free_per_day_cap_blocks_the_free_tier_and_is_not_retried() -> None:
    cap = _err(429, "Rate limit exceeded: free-models-per-day. Add 10 credits")
    cfg = lc.replace(_or(_FREE), retry_attempts=3)
    with patch.object(httpx, "post", return_value=cap) as post:
        lc._call_backend(cfg, _MESSAGES)
        assert post.call_count == 1  # no backoff retries on a spent cap
        with pytest.raises(lc.BackendDisabledError):
            lc._call_backend(cfg, _MESSAGES)
    assert post.call_count == 1
    assert [t.backend for t in lc._cloud_chain_tiers()] == [
        Backend.OPENCODE_GO, Backend.OPENCODE_GO, Backend.OPENCODE_GO, Backend.POOLSIDE,
    ]
    assert lc._provider_available(Backend.OPENROUTER, "anthropic/claude-haiku-4.5")


@pytest.mark.parametrize("resp", [
    # Poolside's short-lived throttle (live 2026-10-17, no Retry-After).
    _err(429, "quota exceeded, try again shortly"),
    _err(429, "Rate limit exceeded: free-models-per-min."),
    _err(429, "slow down"),
    _err(500, "boom"),
    _err(503, "unavailable"),
    _err(403, "forbidden"),
    _err(404, "model not found"),
])
def test_overload_and_other_errors_never_trip_the_breaker(resp) -> None:
    with patch.object(httpx, "post", return_value=resp):
        lc._call_backend(_or(_FREE), _MESSAGES)
        lc._call_backend(_or(_FREE), _MESSAGES)
    assert lc._provider_available(Backend.OPENROUTER, _FREE)


def test_a_plain_429_is_still_retried() -> None:
    cfg = lc.replace(_or(_FREE), retry_attempts=3)
    with patch.object(httpx, "post", return_value=_err(429, "slow down")) as post:
        lc._call_backend(cfg, _MESSAGES)
    assert post.call_count == 3


def test_the_breaker_honours_retry_after_then_closes(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(lc.time, "time", lambda: clock[0])
    with patch.object(httpx, "post", return_value=_err(401, "bad key", {"retry-after": "120"})):
        lc._call_backend(_or(_FREE), _MESSAGES)
    assert not lc._provider_available(Backend.OPENROUTER, _FREE)
    clock[0] += 119
    assert not lc._provider_available(Backend.OPENROUTER, _FREE)
    clock[0] += 2
    assert lc._provider_available(Backend.OPENROUTER, _FREE)


def test_without_retry_after_the_breaker_holds_an_hour(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(lc.time, "time", lambda: clock[0])
    with patch.object(httpx, "post", return_value=_err(401, "bad key")):
        lc._call_backend(_go(lc._OPENCODE_GO_MODEL), _MESSAGES)
    clock[0] += 3599
    assert not lc._provider_available(Backend.OPENCODE_GO, lc._OPENCODE_GO_MODEL)
    clock[0] += 2
    assert lc._provider_available(Backend.OPENCODE_GO, lc._OPENCODE_GO_MODEL)


def test_the_cave_never_trips_the_breaker() -> None:
    cave = lc._cave_review_config(Backend.CAVE)
    assert cave is not None
    with patch.object(httpx, "post", return_value=_err(401, "nope")) as post:
        lc._call_backend(cave, _MESSAGES)
        lc._call_backend(cave, _MESSAGES)
    assert post.call_count == 2


def test_a_review_skips_a_provider_after_its_first_auth_refusal(monkeypatch) -> None:
    """End to end, the 2026-10-17 shape: OpenCode Go's key expired and the
    OpenRouter free cap is spent. The first review pays one refused call to
    each; the next review goes straight to Poolside."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    fake = _Recorder({
        _GO_URL: _err(401, "API key expired"),
        _OR_URL: _err(429, "Rate limit exceeded: free-models-per-day"),
        _POOLSIDE_URL: _finding_response(),
    })
    with patch.object(httpx, "post", side_effect=fake):
        first = review_diff(assemble_evidence([_hunk()]), installation_id=1)
    assert first.backend_used == Backend.POOLSIDE
    assert sum(_GO_URL in u for u in fake.urls) == 1
    assert sum(_OR_URL in u for u in fake.urls) == 1
    fake.urls.clear()
    with patch.object(httpx, "post", side_effect=fake):
        second = review_diff(assemble_evidence([_hunk()]), installation_id=1)
    assert second.backend_used == Backend.POOLSIDE
    assert fake.hosts() == {_POOLSIDE_URL}


def test_a_poolside_usage_limit_429_keeps_poolside_in_the_chain() -> None:
    """The live regression: one `429 usage limit exceeded` from Poolside
    blocked the only working cloud tier for an hour. It is throttling: the
    call is retried by the backoff loop and Poolside stays in the chain."""
    cfg = lc.replace(lc._BACKEND_CONFIGS[Backend.POOLSIDE], retry_attempts=2)
    with patch.object(
        httpx, "post", side_effect=[_err(429, "usage limit exceeded"), _ok()],
    ) as post:
        assert lc._call_backend(cfg, _MESSAGES).status_code == 200
    assert post.call_count == 2
    assert lc._provider_available(Backend.POOLSIDE, lc._POOLSIDE_MODEL)
    assert Backend.POOLSIDE in [t.backend for t in lc._cloud_chain_tiers()]


def _poolside_call(resp: httpx.Response) -> None:
    cfg = lc.replace(lc._BACKEND_CONFIGS[Backend.POOLSIDE], retry_attempts=1)
    with patch.object(httpx, "post", return_value=resp):
        lc._call_backend(cfg, _MESSAGES)


def test_repeated_limit_refusals_skip_poolside_then_it_is_asked_again(monkeypatch) -> None:
    """The second refusal in five minutes holds Poolside for five minutes
    (#1546), so the review reaches a tier that can answer, and Poolside
    comes back on its own."""
    now = 1_000_000.0
    monkeypatch.setattr(lc.time, "time", lambda: now)
    limited = _err(429, "usage limit exceeded")
    _poolside_call(limited)
    assert lc._provider_available(Backend.POOLSIDE, lc._POOLSIDE_MODEL)
    _poolside_call(limited)
    assert not lc._provider_available(Backend.POOLSIDE, lc._POOLSIDE_MODEL)
    assert Backend.POOLSIDE not in lc._usable_saas_fallback_backends()
    assert Backend.POOLSIDE not in [t.backend for t in lc._cloud_chain_tiers()]
    assert lc._provider_available(Backend.POOLSIDE, lc._POOLSIDE_MODEL, now + 301)


def test_repeated_capacity_refusals_skip_the_free_pool(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", "true")
    pool = lc._free_pool_chain_config()
    limited = _err(429, "free pool capacity is reserved for higher-priority callers: openrouter (daily)")
    with patch.object(httpx, "post", return_value=limited):
        lc._call_backend(pool, _MESSAGES)
        lc._call_backend(pool, _MESSAGES)
    assert lc._free_provider_fallback_configs()[0].backend != Backend.FREE_POOL
    assert Backend.FREE_POOL not in [t.backend for t in lc._cloud_chain_tiers()]


def test_a_success_between_limit_refusals_starts_the_count_over() -> None:
    limited = _err(429, "usage limit exceeded")
    _poolside_call(limited)
    _poolside_call(_ok())
    _poolside_call(limited)
    assert lc._provider_available(Backend.POOLSIDE, lc._POOLSIDE_MODEL)


def test_limit_refusals_spread_past_the_window_never_hold(monkeypatch) -> None:
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(lc.time, "time", lambda: now["t"])
    limited = _err(429, "usage limit exceeded")
    _poolside_call(limited)
    now["t"] += 301
    _poolside_call(limited)
    assert lc._provider_available(Backend.POOLSIDE, lc._POOLSIDE_MODEL)


def test_opencode_go_monthly_usage_limit_still_trips_the_breaker() -> None:
    body = {"error": {"type": "GoUsageLimitError", "message": "Go usage limit exceeded"}}
    with patch.object(httpx, "post", return_value=httpx.Response(429, json=body)):
        lc._call_backend(_go("longcat-2.5-preview-free"), _MESSAGES)
    assert not lc._provider_available(Backend.OPENCODE_GO, "longcat-2.5-preview-free")
