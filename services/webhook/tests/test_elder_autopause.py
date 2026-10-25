"""grug#1188: Elder auto-pause under rapid pushes, and the push gate in
Elder's webhook dispatch.

The store is faked in-process here; the real-Postgres semantics of the
push log and pause row live in services/api/tests/test_pg_stores.py.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from personas.code_reviewer import autopause
from personas.code_reviewer import webhook_dispatch as elder_dispatch
from personas.registry import PullRequestContext


class _FakeStore:
    """The five store calls the gate makes, over plain dicts."""

    def __init__(self, *, recent: int = 1, paused: bool = False, claimed=()):
        self.recent = recent
        self.paused = paused
        self.claimed = set(claimed)
        self.pushes: list[dict] = []
        self.pause_calls: list[str] = []
        self.resumed = 0

    def claim_delivery(self, key: str) -> bool:
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def is_elder_paused(self, **_k) -> bool:
        return self.paused

    def record_elder_push(self, **k) -> int:
        self.pushes.append(k)
        return self.recent

    def pause_elder(self, *, head_sha: str, **_k) -> bool:
        self.pause_calls.append(head_sha)
        won = not self.paused
        self.paused = True
        return won

    def resume_elder(self, **_k) -> bool:
        self.resumed += 1
        was = self.paused
        self.paused = False
        return was

    def patches(self):
        return [
            patch(f"adapters.install_store.{name}", getattr(self, name))
            for name in (
                "claim_delivery", "is_elder_paused", "record_elder_push",
                "pause_elder", "resume_elder",
            )
        ]


def _ctx(*, action: str = "synchronize", after: bool = True, head: str = "a" * 40):
    payload = {
        "action": action,
        "pull_request": {"number": 7, "head": {"sha": head}},
        "repository": {"name": "r", "owner": {"login": "o"}},
        "installation": {"id": 9},
    }
    if after:
        payload["before"] = "b" * 40
        payload["after"] = head
    return PullRequestContext(
        installation_id=9, owner="o", repo_name="r", repo_id=7777,
        head_sha=head, pr_number=7, pr_body="", payload=payload,
        delivery_id="d-1", blocking=True,
    )


@pytest.fixture()
def policy(monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_AUTOPAUSE_PUSHES", "3")
    monkeypatch.setenv("GRUG_ELDER_AUTOPAUSE_WINDOW_MINUTES", "10")


def _run(store, ctx, *, note_ok=True, verdicts=None):
    notes: list[str] = []

    def fake_note(_ctx, body):
        if not note_ok:
            raise httpx.ConnectError("down")
        notes.append(body)

    recorded = verdicts if verdicts is not None else []
    cms = store.patches() + [
        patch.object(autopause, "_post_note", fake_note),
        patch.object(autopause, "record_check_verdict",
                     lambda **k: recorded.append(k)),
    ]
    for cm in cms:
        cm.start()
    try:
        return autopause.gate_push(ctx), notes, recorded
    finally:
        for cm in cms:
            cm.stop()


# --- policy ----------------------------------------------------------------


def test_policy_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("GRUG_ELDER_AUTOPAUSE_PUSHES", raising=False)
    monkeypatch.delenv("GRUG_ELDER_AUTOPAUSE_WINDOW_MINUTES", raising=False)
    p = autopause.policy_from_env()
    assert (p.pushes, p.window_minutes) == (5, 10)
    assert p.enabled


def test_policy_zero_pushes_disables(monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_AUTOPAUSE_PUSHES", "0")
    assert not autopause.policy_from_env().enabled


def test_policy_garbage_env_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("GRUG_ELDER_AUTOPAUSE_PUSHES", "lots")
    p = autopause.policy_from_env()
    assert p.pushes == 5
    assert "elder_autopause_env_invalid" in caplog.text


# --- the gate --------------------------------------------------------------


def test_push_under_threshold_reviews_and_counts(policy):
    store = _FakeStore(recent=2)
    decision, notes, rows = _run(store, _ctx())
    assert decision == "review"
    assert len(store.pushes) == 1
    assert store.pushes[0]["window_seconds"] == 600
    assert not notes and not rows and not store.pause_calls


def test_threshold_push_engages_pause_once_with_note_and_activity_row(policy):
    store = _FakeStore(recent=3)
    decision, notes, rows = _run(store, _ctx())
    assert decision == "pause_engaged"
    assert store.pause_calls == ["a" * 40]
    assert len(notes) == 1
    assert "/grug resume" in notes[0] and "/grug review" in notes[0]
    assert autopause.NOTE_MARKER in notes[0]
    assert len(rows) == 1
    row = rows[0]
    assert row["persona_key"] == "code_reviewer"
    assert row["head_sha"] == "a" * 40
    assert row["conclusion"] == "skipped"
    assert row["degraded_reason"] == "auto_paused"
    assert "/grug resume" in row["summary"]


def test_push_while_paused_skips_without_a_second_note(policy):
    store = _FakeStore(recent=9, paused=True)
    decision, notes, rows = _run(store, _ctx(head="c" * 40))
    assert decision == "paused"
    assert not notes
    # still an activity row: this head was not reviewed, and the feed says so
    assert len(rows) == 1 and rows[0]["head_sha"] == "c" * 40


def test_redelivered_push_is_not_counted_twice(policy):
    """Claim per (pr, head_sha): a redelivery of the same push neither
    counts again nor re-decides a pause."""
    store = _FakeStore(recent=2)
    _run(store, _ctx())
    decision, _, _ = _run(store, _ctx())
    assert decision == "review"
    assert len(store.pushes) == 1


def test_claim_key_names_install_repo_pr_and_head(policy):
    store = _FakeStore(recent=1)
    _run(store, _ctx(head="f" * 40))
    assert store.claimed == {f"elder-push:9:o/r#7@{'f' * 40}"}


@pytest.mark.parametrize("action", ["opened", "reopened", "edited", "ready_for_review"])
def test_non_push_actions_never_touch_the_gate(policy, action):
    store = _FakeStore(recent=99, paused=True)
    decision, _, _ = _run(store, _ctx(action=action))
    assert decision == "review"
    assert not store.pushes and not store.claimed


def test_synthetic_rerequest_synchronize_bypasses_pause(policy):
    """The check_run Re-run button dispatches a synthetic `synchronize`
    with no `after`: an explicit operator request, not a push."""
    store = _FakeStore(recent=99, paused=True)
    decision, _, _ = _run(store, _ctx(after=False))
    assert decision == "review"
    assert not store.claimed


def test_disabled_policy_reviews_every_push(monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_AUTOPAUSE_PUSHES", "0")
    store = _FakeStore(recent=99, paused=True)
    decision, _, _ = _run(store, _ctx())
    assert decision == "review"
    assert not store.claimed


def test_store_outage_fails_open_to_review(policy, caplog):
    class _DownStore(_FakeStore):
        def claim_delivery(self, key: str) -> bool:
            raise RuntimeError("db down")

    store = _DownStore()
    decision, _, _ = _run(store, _ctx())
    assert decision == "review"
    assert "elder_autopause_gate_failed" in caplog.text


def test_note_failure_lifts_the_pause_and_reviews(policy, caplog):
    """A pause nobody was told about is a silent stop: if the note does
    not land, undo the pause and review this push."""
    store = _FakeStore(recent=3)
    decision, _, rows = _run(store, _ctx(), note_ok=False)
    assert decision == "review"
    assert store.resumed == 1 and not store.paused
    assert not rows
    assert "elder_autopause_note_failed" in caplog.text


# --- wired into Elder's webhook dispatch -----------------------------------


def test_webhook_dispatch_skips_enqueue_while_paused():
    with patch.object(autopause, "gate_push", return_value="paused"), \
         patch("async_dispatch.enqueue_elder_review") as enq:
        out = elder_dispatch.dispatch_pull_request(_ctx())
    enq.assert_not_called()
    assert out == {"persona": "code_reviewer", "result": "paused"}


def test_webhook_dispatch_reports_pause_engaged():
    with patch.object(autopause, "gate_push", return_value="pause_engaged"), \
         patch("async_dispatch.enqueue_elder_review") as enq:
        out = elder_dispatch.dispatch_pull_request(_ctx())
    enq.assert_not_called()
    assert out == {"persona": "code_reviewer", "result": "pause_engaged"}


def test_webhook_dispatch_enqueues_when_gate_says_review():
    with patch.object(autopause, "gate_push", return_value="review"), \
         patch("async_dispatch.enqueue_elder_review", return_value=True) as enq:
        out = elder_dispatch.dispatch_pull_request(_ctx())
    enq.assert_called_once()
    assert out == {"persona": "code_reviewer", "result": "queued"}
