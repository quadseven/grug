"""The Elder asks OpenCode Go which of its models cost nothing right now.

OpenCode Go's public model list carries a price per model, and the set of
free models changes often. Elder reads that list at run time, keeps it for a
while, and tries only models whose input and output price are both exactly
zero. Every call to such a model is paced: a bounded number at once, a
ceiling per hour, and a cool-down after a 429 that honours Retry-After. The
HTTP boundary is faked; the filter, the cache and the pacing are real.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import llm_client as lc
import opencode_go_free as og
import pytest
from llm_client import Backend

_FREE = "longcat-2.5-free"
_FREE_NO_SUFFIX = "big-pickle"


def _model(model_id: str, inp=0, out=0, api="chat-completions", **extra) -> dict:
    cost = {"text": {"input": {"price": inp}, "output": {"price": out}}}
    cost.update(extra.pop("cost_extra", {}))
    return {
        "id": model_id, "type": extra.pop("type", "chat"), "cost": cost,
        "api": {api: {"base_url": "https://example.invalid/v1"}}, **extra,
    }


def _payload(*models: dict) -> dict:
    return {"object": "list", "data": list(models)}


# --- the filter: a price that is not exactly zero is never free -----------------


def test_a_model_priced_zero_on_both_sides_is_free() -> None:
    assert og.free_chat_models(_payload(_model("a-free"), _model("b"))) == ["a-free", "b"]


@pytest.mark.parametrize("inp,out", [(0.1, 0), (0, 0.0001), (0.15, 0.6), (-1, 0), (0, -0.5)])
def test_any_non_zero_price_refuses_the_model(inp, out) -> None:
    assert og.free_chat_models(_payload(_model("x", inp, out))) == []


@pytest.mark.parametrize("bad", [None, "0", "free", True, False, float("nan"), float("inf"), [], {}])
def test_a_price_that_is_not_a_plain_zero_number_refuses_the_model(bad) -> None:
    assert og.free_chat_models(_payload(_model("x", bad, 0))) == []
    assert og.free_chat_models(_payload(_model("x", 0, bad))) == []


def test_a_free_looking_name_does_not_make_a_priced_model_free() -> None:
    assert og.free_chat_models(_payload(_model("sneaky-free", 0.2, 0.6))) == []


def test_a_model_without_a_cost_block_is_refused() -> None:
    no_cost = _model("x")
    del no_cost["cost"]
    no_text = _model("y")
    no_text["cost"] = {}
    no_out = _model("z")
    del no_out["cost"]["text"]["output"]
    assert og.free_chat_models(_payload(no_cost, no_text, no_out)) == []


def test_a_charged_cache_price_or_multiplier_refuses_the_model() -> None:
    cached = _model("c")
    cached["cost"]["text"]["input"]["cache"] = {"read": 0, "write": 0.5}
    multiplied = _model("m", cost_extra={"multipliers": [{"factor": 2}]})
    extra_axis = _model("e", cost_extra={"audio": {"input": {"price": 1}}})
    assert og.free_chat_models(_payload(cached, multiplied, extra_axis)) == []


def test_zero_cache_prices_do_not_refuse_a_free_model() -> None:
    free = _model("c")
    free["cost"]["text"]["input"]["cache"] = {"read": 0, "write": 0}
    assert og.free_chat_models(_payload(free)) == ["c"]


def test_only_chat_completions_models_are_kept() -> None:
    kept = _model("chat-one")
    responses = _model("resp-one", api="responses")
    messages = _model("msg-one", api="messages")
    embedding = _model("emb-one", type="embedding")
    assert og.free_chat_models(_payload(responses, messages, embedding, kept)) == ["chat-one"]


def test_a_garbled_payload_yields_nothing_and_never_raises() -> None:
    for bad in (None, [], "x", 3, {}, {"data": None}, {"data": "x"}, {"data": [None, 1, "a", {}]}):
        assert og.free_chat_models(bad) == []
    assert og.free_chat_models(_payload(_model(""), _model(None), _model(5))) == []


def test_duplicate_ids_are_listed_once_in_list_order() -> None:
    assert og.free_chat_models(_payload(_model("b"), _model("a"), _model("b"))) == ["b", "a"]


# --- the cache ------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Fetch:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self) -> httpx.Response:
        self.calls += 1
        item = self.responses[min(self.calls, len(self.responses)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item


def _ok(*models: dict) -> httpx.Response:
    return httpx.Response(200, json=_payload(*models))


def _catalog(fetch, clock, ttl=1800.0, max_stale=3600.0) -> og.FreeModelCatalog:
    return og.FreeModelCatalog(
        fetch, ttl_seconds=ttl, max_stale_seconds=max_stale, clock=clock, rand=lambda: 0.0,
    )


def test_the_list_is_fetched_once_inside_the_ttl_and_again_after_it() -> None:
    clock, fetch = _Clock(), _Fetch(_ok(_model("a-free")), _ok(_model("b-free")))
    cat = _catalog(fetch, clock)
    assert cat.models() == ["a-free"]
    clock.now += 1799
    assert cat.models() == ["a-free"]
    assert fetch.calls == 1
    clock.now += 2
    assert cat.models() == ["b-free"]
    assert fetch.calls == 2


def test_an_empty_free_list_is_an_answer_and_is_cached() -> None:
    clock, fetch = _Clock(), _Fetch(_ok(_model("paid", 1, 2)))
    cat = _catalog(fetch, clock)
    assert cat.models() == []
    assert cat.models() == []
    assert fetch.calls == 1


def test_a_failed_refresh_serves_the_old_list_until_it_is_too_stale() -> None:
    clock = _Clock()
    fetch = _Fetch(_ok(_model("a-free")), httpx.ConnectError("down"))
    cat = _catalog(fetch, clock, ttl=100, max_stale=300)
    assert cat.models() == ["a-free"]
    clock.now += 150
    assert cat.models() == ["a-free"]
    clock.now += 300  # past ttl + max_stale (400)
    assert cat.models() == []


def test_a_failing_fetch_is_not_retried_on_every_call() -> None:
    clock, fetch = _Clock(), _Fetch(httpx.ConnectError("down"))
    cat = _catalog(fetch, clock)
    assert cat.models() == []
    for _ in range(5):
        assert cat.models() == []
    assert fetch.calls == 1
    clock.now += 7200
    cat.models()
    assert fetch.calls == 2


def test_the_failure_backoff_grows_and_is_jittered() -> None:
    clock, fetch = _Clock(), _Fetch(httpx.ConnectError("down"))
    low = og.FreeModelCatalog(fetch, clock=clock, rand=lambda: 0.0)
    high = og.FreeModelCatalog(_Fetch(httpx.ConnectError("down")), clock=clock, rand=lambda: 1.0)
    low.models()
    high.models()
    assert 0 < low.retry_not_before - clock.now < high.retry_not_before - clock.now
    first = low.retry_not_before - clock.now
    clock.now = low.retry_not_before + 1
    low.models()
    assert low.retry_not_before - clock.now > first


def test_a_429_on_the_list_waits_out_retry_after() -> None:
    clock = _Clock()
    fetch = _Fetch(httpx.Response(429, headers={"retry-after": "900"}), _ok(_model("a-free")))
    cat = _catalog(fetch, clock)
    assert cat.models() == []
    clock.now += 899
    assert cat.models() == []
    assert fetch.calls == 1
    clock.now += 2
    assert cat.models() == ["a-free"]
    assert fetch.calls == 2


def test_a_garbled_or_error_answer_to_the_list_never_raises() -> None:
    clock = _Clock()
    for resp in (
        httpx.Response(200, content=b"not json"),
        httpx.Response(500, json={"error": "x"}),
        httpx.Response(200, json=[1, 2]),
    ):
        assert _catalog(_Fetch(resp), clock).models() == []


def test_peek_never_fetches_and_knows_free_from_priced() -> None:
    clock, fetch = _Clock(), _Fetch(_ok(_model("a-free"), _model("b-free", 0.1, 0.1)))
    cat = _catalog(fetch, clock)
    assert cat.verdict("a-free") is None
    assert fetch.calls == 0
    cat.models()
    assert cat.verdict("a-free") is True
    assert cat.verdict("b-free") is False
    assert cat.verdict("never-listed") is None
    clock.now += 10_000
    assert cat.verdict("a-free") is None


# --- the pacing -----------------------------------------------------------------


def _gate(clock, **kw) -> og.FreeCallGate:
    kw.setdefault("hourly_ceiling", 3)
    kw.setdefault("max_concurrent", 2)
    return og.FreeCallGate(clock=clock, rand=lambda: 0.0, **kw)


def test_the_hourly_ceiling_refuses_the_next_call_and_the_window_slides() -> None:
    clock, gate = _Clock(), _gate(_Clock())
    gate = _gate(clock)
    for _ in range(3):
        assert gate.enter("m", wait_seconds=0)
        gate.leave()
    assert not gate.enter("m", wait_seconds=0)
    clock.now += 3601
    assert gate.enter("m", wait_seconds=0)
    gate.leave()


def test_a_refused_call_does_not_use_up_the_ceiling() -> None:
    clock = _Clock()
    gate = _gate(clock, hourly_ceiling=1, max_concurrent=1)
    assert gate.enter("m", wait_seconds=0)
    assert not gate.enter("m", wait_seconds=0)  # no slot
    gate.leave()
    clock.now += 3601
    assert gate.enter("m", wait_seconds=0)


def test_concurrency_is_bounded() -> None:
    gate = _gate(_Clock(), hourly_ceiling=100, max_concurrent=2)
    assert gate.enter("a", wait_seconds=0)
    assert gate.enter("b", wait_seconds=0)
    assert not gate.enter("c", wait_seconds=0)
    gate.leave()
    assert gate.enter("c", wait_seconds=0)


def test_a_429_with_retry_after_cools_that_model_for_that_long() -> None:
    clock, gate = _Clock(), _gate(_Clock())
    gate = _gate(clock)
    gate.note_response("m", httpx.Response(429, headers={"retry-after": "120"}))
    assert gate.cooling("m")
    assert not gate.cooling("other")
    clock.now += 119
    assert gate.cooling("m")
    clock.now += 2
    assert not gate.cooling("m")


def test_a_429_without_retry_after_backs_off_with_doubling_and_jitter() -> None:
    clock = _Clock()
    lo = og.FreeCallGate(clock=clock, rand=lambda: 0.0)
    hi = og.FreeCallGate(clock=clock, rand=lambda: 1.0)
    waits = []
    for gate in (lo, hi):
        gate.note_response("m", httpx.Response(429))
        waits.append(gate.cooldown_until("m") - clock.now)
    assert 0 < waits[0] < waits[1]
    lo.note_response("m", httpx.Response(429))
    assert lo.cooldown_until("m") - clock.now > waits[0]


def test_retry_after_is_capped_and_a_junk_header_falls_back_to_backoff() -> None:
    clock, gate = _Clock(), _gate(_Clock())
    gate = _gate(clock)
    gate.note_response("big", httpx.Response(429, headers={"retry-after": "99999999"}))
    assert gate.cooldown_until("big") - clock.now <= og.MAX_COOLDOWN_SECONDS * 1.3
    for junk in ("soon", "-5", "nan", "inf", ""):
        gate.note_response("j", httpx.Response(429, headers={"retry-after": junk}))
        assert 0 < gate.cooldown_until("j") - clock.now <= og.MAX_COOLDOWN_SECONDS * 1.3
        gate.note_response("j", httpx.Response(200))


def test_a_good_answer_clears_the_cool_down_streak() -> None:
    clock, gate = _Clock(), _gate(_Clock())
    gate = _gate(clock)
    gate.note_response("m", httpx.Response(429))
    first = gate.cooldown_until("m") - clock.now
    gate.note_response("m", httpx.Response(200))
    assert not gate.cooling("m")
    gate.note_response("m", httpx.Response(429))
    assert gate.cooldown_until("m") - clock.now == first


# --- Elder's chain --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_opencode_go_free_catalog", None, raising=False)
    monkeypatch.setattr(lc, "_opencode_go_free_gate", None, raising=False)
    monkeypatch.setenv("GRUG_OPENCODE_GO_API_KEY_SSM", "/test/opencode-go")
    monkeypatch.setenv("GRUG_OPENCODE_GO_FALLBACK_MODEL", "beam-free")
    monkeypatch.delenv("GRUG_OPENCODE_GO_SECOND_MODEL", raising=False)
    monkeypatch.delenv("GRUG_CLOUD_FREE_TIER_MODEL", raising=False)
    monkeypatch.delenv("GRUG_CLOUD_FREE_POOL", raising=False)
    monkeypatch.delenv("GRUG_OPENCODE_GO_FREE_DISCOVERY", raising=False)
    monkeypatch.delenv("GRUG_OPENCODE_GO_FREE_MAX_TIERS", raising=False)
    monkeypatch.delenv("GRUG_DISABLED_BACKENDS", raising=False)


def _listed(monkeypatch, *models: dict, enabled: bool = True) -> _Fetch:
    fetch = _Fetch(_ok(*models, _model(lc._OPENCODE_GO_MODEL, 1, 1)))
    if enabled:
        monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_DISCOVERY", "1")
    monkeypatch.setattr(lc, "_opencode_go_free_models_fetch", fetch, raising=False)
    return fetch


def _chain() -> list[str]:
    return [t.model for t in lc._cloud_chain_tiers()]


def test_discovery_is_off_unless_asked_for_and_never_touches_the_network(monkeypatch) -> None:
    fetch = _listed(monkeypatch, _model(_FREE), enabled=False)
    assert _FREE not in _chain()
    assert fetch.calls == 0


def test_configured_and_discovered_free_models_run_before_paid_go(monkeypatch) -> None:
    _listed(monkeypatch, _model("beam-free"), _model(_FREE), _model(_FREE_NO_SUFFIX))
    chain = _chain()
    assert chain[:4] == ["beam-free", _FREE, _FREE_NO_SUFFIX, lc._OPENCODE_GO_MODEL]
    assert chain.count("beam-free") == 1


def test_a_priced_model_is_never_a_tier(monkeypatch) -> None:
    _listed(
        monkeypatch,
        _model("cheap-one", 0.1, 0.2), _model("sneaky-free", 0, 0.01), _model(_FREE),
    )
    chain = _chain()
    assert _FREE in chain
    assert "cheap-one" not in chain and "sneaky-free" not in chain


def test_an_empty_list_leaves_the_paid_chain_as_it_was(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_OPENCODE_GO_SECOND_MODEL", "second-paid")
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_DISCOVERY", "1")
    monkeypatch.setattr(lc, "_opencode_go_free_models_fetch", _Fetch(_ok()))
    assert _chain() == [lc._OPENCODE_GO_MODEL, "second-paid", "poolside/laguna-s-2.1"]


def test_a_configured_model_delisted_without_a_free_suffix_is_skipped(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_OPENCODE_GO_FALLBACK_MODEL", _FREE_NO_SUFFIX)
    _listed(monkeypatch, _model("beam-free"))
    assert _chain() == ["beam-free", lc._OPENCODE_GO_MODEL, "poolside/laguna-s-2.1"]


def test_a_dead_list_endpoint_preserves_paid_go(monkeypatch) -> None:
    before = _chain()
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_DISCOVERY", "1")
    monkeypatch.setattr(
        lc, "_opencode_go_free_models_fetch", _Fetch(httpx.ConnectError("down")), raising=False,
    )
    assert _chain() == [model for model in before if model != "beam-free"]


def test_the_number_of_discovered_tiers_is_bounded(monkeypatch) -> None:
    _listed(monkeypatch, *[_model(f"m{i}-free") for i in range(6)])
    assert len([m for m in _chain() if m.startswith("m")]) == 2
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_MAX_TIERS", "4")
    assert len([m for m in _chain() if m.startswith("m")]) == 4
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_MAX_TIERS", "0")
    assert [m for m in _chain() if m.startswith("m")] == []


def test_a_cooling_model_is_skipped_and_the_next_free_one_takes_its_place(monkeypatch) -> None:
    _listed(monkeypatch, _model("a-free"), _model("b-free"), _model("c-free"))
    assert [m for m in _chain() if m[1:] == "-free" and m[0] in "abc"] == ["a-free", "b-free"]
    lc._opencode_go_free_gate_get().note_response(
        "a-free", httpx.Response(429, headers={"retry-after": "600"}),
    )
    assert [m for m in _chain() if m[1:] == "-free" and m[0] in "abc"] == ["b-free", "c-free"]


def test_a_discovered_tier_is_a_chat_tier_with_a_bounded_output(monkeypatch) -> None:
    _listed(monkeypatch, _model(_FREE))
    tier = next(t for t in lc._cloud_chain_tiers() if t.model == _FREE)
    assert tier.backend == Backend.OPENCODE_GO
    assert tier.wire == "chat"
    assert tier.extra_body["max_tokens"] == lc._CLOUD_CHAIN_MAX_TOKENS
    assert tier.retry_attempts == 1


def test_the_catalogue_overrules_the_name_when_it_lists_a_price(monkeypatch) -> None:
    _listed(monkeypatch, _model("beam-free", 0.3, 0.9), _model(_FREE))
    lc._opencode_go_free_catalog_get().models()
    assert lc._opencode_go_model_is_free(_FREE) is True
    assert lc._opencode_go_model_is_free("beam-free") is False  # priced now: counts as paid
    assert lc._opencode_go_model_is_free("unlisted-free") is True  # no data: the old name rule


def test_a_free_model_without_the_suffix_counts_as_free_for_the_cap(monkeypatch) -> None:
    _listed(monkeypatch, _model(_FREE_NO_SUFFIX))
    lc._opencode_go_free_catalog_get().models()
    assert lc._opencode_go_model_is_free(_FREE_NO_SUFFIX) is True


def test_private_code_still_reaches_only_models_with_verified_terms(monkeypatch) -> None:
    _listed(monkeypatch, _model(_FREE))
    tier = next(t for t in lc._cloud_chain_tiers() if t.model == _FREE)
    assert _FREE not in lc._OPENCODE_GO_PRIVATE_SAFE_MODELS
    with patch.object(httpx, "post") as post, pytest.raises(lc.BackendDisabledError):
        lc._call_backend(tier, [{"role": "user", "content": "x"}])
    post.assert_not_called()


def _public(monkeypatch) -> None:
    monkeypatch.setattr(lc, "_spark_data_class_header", lambda: "public")


def _ok_review() -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": json.dumps({"findings": []})}}],
        "model": "m",
    })


def test_a_429_on_a_free_model_starts_its_cool_down_from_retry_after(monkeypatch) -> None:
    _listed(monkeypatch, _model(_FREE))
    _public(monkeypatch)
    tier = next(t for t in lc._cloud_chain_tiers() if t.model == _FREE)
    limited = httpx.Response(429, headers={"retry-after": "300"}, json={"error": {"message": "slow"}})
    with patch.object(httpx, "post", return_value=limited):
        resp = lc._call_backend(tier, [{"role": "user", "content": "x"}])
    assert resp.status_code == 429
    assert lc._opencode_go_free_gate_get().cooling(_FREE)
    assert _FREE not in _chain()


def test_the_hourly_ceiling_stops_calls_before_the_network(monkeypatch) -> None:
    _listed(monkeypatch, _model(_FREE))
    _public(monkeypatch)
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_HOURLY_CEILING", "2")
    tier = next(t for t in lc._cloud_chain_tiers() if t.model == _FREE)
    with patch.object(httpx, "post", return_value=_ok_review()) as post:
        for _ in range(2):
            lc._call_backend(tier, [{"role": "user", "content": "x"}])
        with pytest.raises(httpx.RequestError):
            lc._call_backend(tier, [{"role": "user", "content": "x"}])
    assert post.call_count == 2
    assert _FREE not in _chain()


def test_the_ceiling_is_released_slot_by_slot_when_a_call_fails(monkeypatch) -> None:
    _listed(monkeypatch, _model(_FREE))
    _public(monkeypatch)
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_CONCURRENCY", "1")
    tier = next(t for t in lc._cloud_chain_tiers() if t.model == _FREE)
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("boom")):
        for _ in range(3):
            with pytest.raises(httpx.ConnectError):
                lc._call_backend(tier, [{"role": "user", "content": "x"}])


def test_an_empty_answer_from_a_discovered_free_model_is_still_not_a_second_opinion() -> None:
    """The rule in `_empty_answers_agree` is unchanged: a free Go model's empty
    answer never confirms another empty answer (grug#1601 holds the question)."""
    assert lc._empty_confirms_nothing((Backend.OPENCODE_GO, _FREE))
    assert not lc._empty_answers_agree(
        (Backend.OPENCODE_GO, lc._OPENCODE_GO_MODEL), (Backend.OPENCODE_GO, _FREE), None,
    )


def test_every_elder_deployment_turns_discovery_on() -> None:
    k8s = Path(__file__).resolve().parents[3] / "k8s"
    for name in ("consumer-deployment.yaml", "webhook-deployment.yaml", "poller-cronjob.yaml"):
        text = (k8s / name).read_text(encoding="utf-8")
        assert "GRUG_OPENCODE_GO_FREE_DISCOVERY" in text, name
