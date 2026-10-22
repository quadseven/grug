"""grug#1286: a bench or breaker one pod opens holds in every pod, and after
a restart, and the shared store never fails or delays a review."""
from __future__ import annotations

import threading
import time

import httpx
import llm_client as lc
import pytest
import shared_benches
from llm_client import Backend, BackendConfig

_MODEL = "laguna-s-2.1"
_REAL_ENSURE_THREAD = shared_benches._ensure_thread
_POOLSIDE = BackendConfig(
    backend=Backend.POOLSIDE, url="https://poolside.test/v1/chat/completions",
    model=_MODEL, key_loader=lambda: "k",
)


class _Table:
    """grug_kv rows for PK, shared by every simulated pod."""

    def __init__(self) -> None:
        self.rows: dict[str, float] = {}
        self.down = False

    def write(self, sk: str, until: float) -> None:
        self._check()
        self.rows[sk] = max(self.rows.get(sk, 0.0), until)

    def delete(self, sk: str) -> None:
        self._check()
        self.rows.pop(sk, None)

    def read(self) -> list[tuple[str, float]]:
        self._check()
        return list(self.rows.items())

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("store down")


@pytest.fixture
def table(monkeypatch):
    t = _Table()
    monkeypatch.setenv("GRUG_SHARED_BENCHES", "true")
    monkeypatch.setenv("GRUG_DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(shared_benches, "_write", t.write)
    monkeypatch.setattr(shared_benches, "_delete", t.delete)
    monkeypatch.setattr(shared_benches, "_read_live", t.read)
    monkeypatch.setattr(shared_benches, "_ensure_thread", lambda: None)
    monkeypatch.setattr(shared_benches, "_snapshot", {})
    shared_benches._queue.clear()
    monkeypatch.setattr(lc, "_bench_state", {})
    monkeypatch.setattr(lc, "_slow_bench_state", {})
    monkeypatch.setattr(lc, "_provider_blocked_until", {})
    monkeypatch.setattr(lc, "_opencode_go_blocked_until", 0.0)
    yield t
    shared_benches._queue.clear()


def _other_pod(monkeypatch) -> None:
    """A different process, or this one after a restart: no local state, and
    a snapshot read fresh from the table."""
    monkeypatch.setattr(lc, "_bench_state", {})
    monkeypatch.setattr(lc, "_slow_bench_state", {})
    monkeypatch.setattr(lc, "_provider_blocked_until", {})
    monkeypatch.setattr(lc, "_opencode_go_blocked_until", 0.0)
    monkeypatch.setattr(shared_benches, "_snapshot", {})
    shared_benches.sync()


def _resp(status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status, json=body or {"error": {"message": "model not found"}},
        headers=headers, request=httpx.Request("POST", _POOLSIDE.url),
    )


def _bench_poolside() -> None:
    for _ in range(lc._BENCH_FAILURE_THRESHOLD):
        lc._note_model_response(_POOLSIDE, _resp(404))


def test_a_model_benched_in_one_pod_is_skipped_by_another_and_after_a_restart(table, monkeypatch):
    _bench_poolside()
    shared_benches.sync()
    assert "bench#poolside#laguna-s-2.1" in table.rows

    _other_pod(monkeypatch)
    assert lc._bench_state == {}, "the other pod starts with no local bench"
    assert lc._model_is_benched("poolside", _MODEL)
    assert not lc._tier_is_usable(_POOLSIDE, [])


def test_a_success_clears_the_shared_bench_for_pods_that_did_not_open_it(table, monkeypatch):
    _bench_poolside()
    shared_benches.sync()
    _other_pod(monkeypatch)

    lc._note_model_response(_POOLSIDE, _resp(200, {"choices": []}))
    shared_benches.sync()
    assert table.rows == {}
    _other_pod(monkeypatch)
    assert not lc._model_is_benched("poolside", _MODEL)


def test_a_slow_bench_is_shared(table, monkeypatch):
    timeout = httpx.ReadTimeout("slow", request=httpx.Request("POST", _POOLSIDE.url))
    for _ in range(lc._SLOW_BENCH_FAILURE_THRESHOLD):
        lc._note_model_timeout(Backend.POOLSIDE, _MODEL, timeout)
    shared_benches.sync()

    _other_pod(monkeypatch)
    assert lc._model_is_slow_benched("poolside", _MODEL)
    assert lc._slow_bench_expiry(_POOLSIDE) > 0


def test_a_provider_breaker_is_shared(table, monkeypatch):
    lc._note_provider_response(_POOLSIDE, _resp(401, {"error": {"message": "bad key"}}))
    shared_benches.sync()

    _other_pod(monkeypatch)
    assert not lc._provider_available(Backend.POOLSIDE, _MODEL)
    with pytest.raises(lc.BackendDisabledError):
        lc._refuse_unusable_backend(_POOLSIDE)


def test_the_opencode_go_usage_breaker_is_shared(table, monkeypatch):
    go = BackendConfig(
        backend=Backend.OPENCODE_GO, url="https://go.test", model="deepseek-v4.1-flash",
        key_loader=lambda: "k",
    )
    limit = _resp(
        429, {"error": {"type": "GoUsageLimitError", "message": "usage limit"}},
        headers={"retry-after": "7200"},
    )
    lc._note_opencode_go_response(limit)
    shared_benches.sync()

    _other_pod(monkeypatch)
    assert not lc._opencode_go_available()
    with pytest.raises(lc.OpencodeGoCircuitOpenError):
        lc._refuse_unusable_backend(go)


def test_a_store_that_is_down_never_breaks_or_blocks_a_review(table, monkeypatch):
    table.down = True
    _bench_poolside()  # publishing never raises
    assert lc._model_is_benched("poolside", _MODEL), "the local bench still holds"
    shared_benches.sync()  # logged, never raised

    calls = []
    monkeypatch.setattr(shared_benches, "_read_live", lambda: calls.append(1) or [])
    assert lc._tier_is_usable(
        BackendConfig(backend=Backend.OPENROUTER, url="u", model="m:free", key_loader=lambda: "k"),
        [],
    )
    assert calls == [], "a reader touched the store on the review path"


def test_nothing_is_shared_unless_switched_on(table, monkeypatch):
    monkeypatch.delenv("GRUG_SHARED_BENCHES")
    _bench_poolside()
    shared_benches.sync()
    assert table.rows == {}
    assert len(shared_benches._queue) == 0


def test_a_clear_made_while_the_store_is_down_is_applied_when_it_is_back(table, monkeypatch):
    _bench_poolside()
    shared_benches.sync()
    _other_pod(monkeypatch)
    table.down = True
    lc._note_model_response(_POOLSIDE, _resp(200, {"choices": []}))
    shared_benches.sync()
    table.down = False
    shared_benches.sync()
    assert table.rows == {}, "the dropped clear let the bench come back"


def test_an_entry_shared_between_write_and_reread_is_not_lost(table, monkeypatch):
    real_read = table.read

    def read_after_a_trip():
        _bench_poolside()  # another thread trips a bench mid-sync
        return real_read()

    monkeypatch.setattr(shared_benches, "_read_live", read_after_a_trip)
    shared_benches.sync()
    assert shared_benches.blocked_until("bench", "poolside", _MODEL) > 0
    monkeypatch.setattr(shared_benches, "_read_live", real_read)
    shared_benches.sync()
    assert "bench#poolside#laguna-s-2.1" in table.rows


def test_a_short_lived_run_shares_its_bench_at_exit(table, monkeypatch):
    _bench_poolside()
    assert table.rows == {}
    shared_benches._flush_at_exit()
    assert "bench#poolside#laguna-s-2.1" in table.rows


def _fresh_process(monkeypatch, read) -> None:
    """A process that just started: the real thread start, no snapshot yet."""
    monkeypatch.setattr(shared_benches, "_ensure_thread", _REAL_ENSURE_THREAD)
    monkeypatch.setattr(shared_benches, "_read_live", read)
    monkeypatch.setattr(shared_benches, "_thread", None)
    monkeypatch.setattr(shared_benches, "_first_sync", threading.Event())
    monkeypatch.setattr(shared_benches, "_first_load_deadline", 0.0)
    monkeypatch.setattr(shared_benches, "REFRESH_INTERVAL_S", 3600.0)


def test_a_fresh_process_reads_the_table_before_its_first_answer(table, monkeypatch):
    table.rows["bench#poolside#laguna-s-2.1"] = time.time() + 600

    def slow_read():
        time.sleep(0.1)  # a real store round trip, slower than the first lookup
        return table.read()

    _fresh_process(monkeypatch, slow_read)
    assert shared_benches.blocked_until("bench", "poolside", _MODEL) > time.time()


def test_a_store_that_never_answers_delays_a_process_once_and_briefly(table, monkeypatch):
    hang = threading.Event()
    _fresh_process(monkeypatch, lambda: hang.wait(30) and [])
    monkeypatch.setattr(shared_benches, "FIRST_LOAD_WAIT_S", 0.2)
    started = time.monotonic()
    try:
        for _ in range(5):
            assert shared_benches.blocked_until("bench", "poolside", _MODEL) == 0.0
        assert time.monotonic() - started < 1.0, "every read waited, not just the first"
    finally:
        hang.set()


def test_an_unknown_kind_is_a_programming_error(table):
    with pytest.raises(ValueError):
        shared_benches.publish("bnech", "poolside", _MODEL, 1.0)
