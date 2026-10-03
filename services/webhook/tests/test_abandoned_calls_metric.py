"""Tests for the abandoned-LLM-call in-flight metric (#638).

grug#637 added mid-flight review cancellation: when a newer commit lands,
the waiter stops waiting on an in-flight backend call and returns
immediately. The background call is abandoned, not killed - it keeps
running to its own natural conclusion and still competes for the
backend's (e.g. spark-gateway's) single generation slot. Nothing tracked
how many abandoned calls were in flight, so a pileup under rapid PR
churn was invisible.

These tests pin the metric's contract: the count increments when the
waiting side abandons a still-running call, decrements when that call's
real request finishes, and never counts a call that finished before the
abandon (or was never abandoned at all).
"""

from __future__ import annotations

import threading
import time

import httpx
import llm_client as lc
import pytest
from llm_client import Backend


@pytest.fixture()
def tracker(monkeypatch):
    """A fresh tracker wired as the module's active one, with gauge
    emission captured instead of sent."""
    emitted: list[tuple[float, str]] = []
    monkeypatch.setattr(
        lc,
        "_emit_abandoned_in_flight_gauge",
        lambda count, backend: emitted.append((count, backend)),
    )
    t = lc._AbandonedCallTracker()
    monkeypatch.setattr(lc, "_abandoned_calls", t)
    t.emitted = emitted  # type: ignore[attr-defined]
    return t


def _state() -> dict:
    return {"abandoned": False, "finished": False}


def test_abandon_before_finish_counts_then_drains(tracker) -> None:
    """The core contract: abandon a running call -> 1 in flight; its real
    request finishes -> back to 0. The gauge is emitted on both
    transitions so a dashboard never has to infer the count."""
    state = _state()
    tracker.note_abandoned(state, Backend.CAVE.value)
    assert tracker.in_flight == 1
    assert (1.0, Backend.CAVE.value) in tracker.emitted
    tracker.call_finished(state, Backend.CAVE.value)
    assert tracker.in_flight == 0
    assert (0.0, Backend.CAVE.value) in tracker.emitted


def test_abandon_after_finish_counts_nothing(tracker) -> None:
    """If the background call already finished, the abandon raced a
    completed request - it was never in flight as abandoned, so the
    count must not move and no gauge may fire."""
    state = _state()
    tracker.call_finished(state, Backend.CAVE.value)
    tracker.note_abandoned(state, Backend.CAVE.value)
    assert tracker.in_flight == 0
    assert tracker.emitted == []


def test_unabandoned_finish_counts_nothing(tracker) -> None:
    """The common path - no cancellation, call completes, waiter consumed
    the result. The tracker must stay silent: no count, no emission."""
    state = _state()
    tracker.call_finished(state, Backend.CAVE.value)
    assert tracker.in_flight == 0
    assert tracker.emitted == []


def test_concurrent_abandons_track_independently(tracker) -> None:
    """Three abandoned calls in flight, draining one at a time - the
    gauge walks 3 -> 2 -> 1 -> 0."""
    states = [_state() for _ in range(3)]
    for s in states:
        tracker.note_abandoned(s, Backend.CAVE.value)
    assert tracker.in_flight == 3
    for i, s in enumerate(states):
        tracker.call_finished(s, Backend.CAVE.value)
        assert tracker.in_flight == 2 - i
    assert (0.0, Backend.CAVE.value) in tracker.emitted


def test_cancellable_path_wires_abandon_to_tracker(tracker, monkeypatch) -> None:
    """End to end through `_post_with_retries_cancellable`: a blocked
    backend call is abandoned via cancel_event (count -> 1, gauge fires),
    then the blocked request is released and the count drains to 0."""
    release = threading.Event()

    def _blocking_post(*args, **kwargs):
        assert release.wait(10), "background call never released"
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(lc, "_post_with_retries", _blocking_post)
    config = lc.BackendConfig(
        backend=Backend.CAVE,
        url="http://127.0.0.1:9/v1/chat/completions",
        model="test-model",
        key_loader=lambda: "test-key",
        timeout_seconds=5.0,
        retry_attempts=1,
    )
    cancel_event = threading.Event()
    outcome: dict = {}

    def _run() -> None:
        try:
            lc._post_with_retries_cancellable(config, {}, {}, 1, cancel_event)
        except Exception as e:  # noqa: BLE001 - recording, the assert below checks it
            outcome["exc"] = e

    waiter = threading.Thread(target=_run, daemon=True)
    waiter.start()
    time.sleep(0.5)  # let the background _do_call start and block
    cancel_event.set()
    waiter.join(5)
    assert isinstance(outcome.get("exc"), httpx.RequestError)
    assert tracker.in_flight == 1, "abandoned call not counted"
    assert (1.0, Backend.CAVE.value) in tracker.emitted

    release.set()
    deadline = time.monotonic() + 5
    while tracker.in_flight != 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert tracker.in_flight == 0, "finished abandoned call never drained"
    assert (0.0, Backend.CAVE.value) in tracker.emitted


def test_cancellable_path_no_abandon_no_metric(tracker, monkeypatch) -> None:
    """No cancellation: the call completes normally and the tracker
    stays silent, exactly like the pre-#638 code path."""
    monkeypatch.setattr(
        lc,
        "_post_with_retries",
        lambda *args, **kwargs: httpx.Response(200, json={"ok": True}),
    )
    config = lc.BackendConfig(
        backend=Backend.CAVE,
        url="http://127.0.0.1:9/v1/chat/completions",
        model="test-model",
        key_loader=lambda: "test-key",
        timeout_seconds=5.0,
        retry_attempts=1,
    )
    resp = lc._post_with_retries_cancellable(
        config,
        {},
        {},
        1,
        threading.Event(),
    )
    assert resp.status_code == 200
    assert tracker.in_flight == 0
    assert tracker.emitted == []
