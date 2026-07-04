"""Tests for poller_handler.handler — the scheduled reaction-poll Lambda
entry point (#247b). Mocks install_store / auth / reactions; no DDB or
network. Webhook-only (the poller ships in the webhook image)."""
from __future__ import annotations

import poller_handler


def _wire(monkeypatch, *, installs, records_for, retry, poll):
    monkeypatch.setattr(poller_handler, "list_allowlisted_installs", lambda: installs)
    monkeypatch.setattr(poller_handler, "list_comment_records", records_for)
    monkeypatch.setattr(poller_handler, "with_install_token_retry", retry)
    monkeypatch.setattr(poller_handler, "poll_and_annotate", poll)
    # #407: stub the auto-replay to a no-op so reaction-poll tests don't hit
    # GitHub and their exact-result assertions stay about the reaction poll.
    monkeypatch.setattr(poller_handler, "_replay_missed_deliveries", lambda: {})


def test_poller_polls_each_allowlisted_install(monkeypatch):
    """One poll_and_annotate per allowlisted install with records; the
    token thunk resolves to the retry-supplied token; summary sums verdicts."""
    polled = []

    def _poll(records, *, install_id, fetch_token):
        polled.append(install_id)
        assert fetch_token() == "tok"   # thunk yields the retry's token
        return 2
    _wire(
        monkeypatch,
        installs=[11, 22],
        records_for=lambda iid: [{"comment_id": iid}],
        retry=lambda iid, fn: fn("tok"),
        poll=_poll,
    )
    out = poller_handler.handler({}, None)
    assert polled == [11, 22]
    assert out == {"installs": 2, "records": 2, "submitted": 4, "failed_installs": 0, "pulse_nudges": 0, "pulse_failed_installs": 0}


def test_poller_one_install_failure_does_not_abort_cycle(monkeypatch, caplog):
    """A single install's token/GH failure is logged + counted, and the
    cycle continues to the next install (best-effort per install). A PARTIAL
    failure must NOT escalate to error (else a status:error monitor false-
    fires every time one of many installs hiccups)."""
    import logging as _logging

    def _retry(iid, fn):
        if iid == 1:
            raise RuntimeError("install 1 token fetch failed")
        return fn("tok")
    _wire(
        monkeypatch,
        installs=[1, 2],
        records_for=lambda iid: [{"comment_id": iid}],
        retry=_retry,
        poll=lambda records, *, install_id, fetch_token: 3,
    )
    with caplog.at_level(_logging.INFO):
        out = poller_handler.handler({}, None)
    assert out["installs"] == 2
    assert out["failed_installs"] == 1
    assert out["submitted"] == 3   # install 2 still polled despite install 1 failing
    assert out["records"] == 2     # both installs' records counted as attempted
    # partial failure → cycle-complete at INFO, NOT the all-failed error.
    cycle = [r for r in caplog.records if r.msg == "reaction_poll_cycle_complete"]
    assert cycle and cycle[0].levelno == _logging.INFO
    assert not any(r.levelno >= _logging.ERROR for r in caplog.records)


def test_poller_records_listing_failure_is_best_effort(monkeypatch):
    """A CommentRecord LISTING failure (DDB error) for one install must be
    caught too — it's inside the per-install try — so the cron counts it
    failed and continues to the next install (codex BLOCK regression)."""
    def _records(iid):
        if iid == 1:
            raise RuntimeError("DDB list failure")
        return [{"comment_id": iid}]
    _wire(
        monkeypatch,
        installs=[1, 2],
        records_for=_records,
        retry=lambda iid, fn: fn("tok"),
        poll=lambda records, *, install_id, fetch_token: 5,
    )
    out = poller_handler.handler({}, None)
    assert out["installs"] == 2
    assert out["failed_installs"] == 1   # install 1 listing failed
    assert out["submitted"] == 5         # install 2 still polled


def test_poller_skips_installs_with_no_records(monkeypatch):
    """An install with no CommentRecords skips the REACTIONS poll, and
    with no pulse-enabled repos (store-driven targeting, #472/PR #489)
    the Pulse pass costs no token either - a fully idle install makes
    zero GitHub calls."""
    touched = []
    monkeypatch.setattr(
        "adapters.install_store.list_pulse_enabled_repos", lambda iid: [],
    )
    _wire(
        monkeypatch,
        installs=[7],
        records_for=lambda iid: [],
        retry=lambda iid, fn: touched.append(iid),
        poll=lambda *a, **k: 0,
    )
    out = poller_handler.handler({}, None)
    assert touched == []
    assert out == {"installs": 1, "records": 0, "submitted": 0, "failed_installs": 0, "pulse_nudges": 0, "pulse_failed_installs": 0}


# --- #407: auto-replay wiring -----------------------------------------------


def test_replay_missed_deliveries_maps_report(monkeypatch):
    """_replay_missed_deliveries calls delivery_replay.replay_since (with a
    window-derived since) and maps the report into replay_* summary keys."""
    import delivery_replay

    captured = {}

    def _fake(since):
        captured["since"] = since
        return delivery_replay.ReplayReport(
            scanned=5, failed_guids=2, redelivered=2, errors=0
        )

    monkeypatch.setattr(poller_handler.delivery_replay, "replay_since", _fake)
    out = poller_handler._replay_missed_deliveries()
    assert out == {
        "replay_scanned": 5,
        "replay_failed_guids": 2,
        "replay_redelivered": 2,
        "replay_errors": 0,
    }
    assert captured["since"].endswith("Z")  # an ISO-8601 UTC instant was passed


def test_handler_merges_replay_counts(monkeypatch):
    """The cron summary carries the replay counts so an operator/DD sees that
    auto-recovery ran each tick."""
    _wire(
        monkeypatch,
        installs=[],
        records_for=lambda iid: [],
        retry=lambda iid, fn: 0,
        poll=lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        poller_handler, "_replay_missed_deliveries",
        lambda: {"replay_scanned": 9, "replay_redelivered": 3, "replay_errors": 0},
    )
    out = poller_handler.handler({}, None)
    assert out["replay_redelivered"] == 3
    assert out["replay_scanned"] == 9


def test_handler_replay_failure_does_not_abort_cron(monkeypatch, caplog):
    """A replay blow-up (GitHub down, JWT error) must NOT abort the reaction
    poll - it's logged and surfaced as replay_error, results otherwise intact."""
    import logging as _logging

    _wire(
        monkeypatch,
        installs=[1],
        records_for=lambda iid: [{"comment_id": iid}],
        retry=lambda iid, fn: fn("tok"),
        poll=lambda records, *, install_id, fetch_token: 1,
    )

    def _boom():
        raise RuntimeError("github down")

    monkeypatch.setattr(poller_handler, "_replay_missed_deliveries", _boom)
    with caplog.at_level(_logging.WARNING):
        out = poller_handler.handler({}, None)
    assert out["submitted"] == 1  # reaction poll still completed
    assert out["replay_error"] == "RuntimeError"
    assert any(r.msg == "delivery_replay_failed" for r in caplog.records)


def test_poller_all_installs_fail_logs_error(monkeypatch, caplog):
    """A SYSTEMIC failure (every install errors — auth/config drift) must
    escalate to log.error, not hide as info — else a status:error monitor
    never fires and it looks like a healthy idle cycle."""
    import logging as _logging

    def _retry(iid, fn):
        raise RuntimeError("systemic token failure")
    _wire(
        monkeypatch,
        installs=[1, 2],
        records_for=lambda iid: [{"comment_id": iid}],
        retry=_retry,
        poll=lambda *a, **k: 0,
    )
    with caplog.at_level(_logging.WARNING):
        out = poller_handler.handler({}, None)
    assert out == {"installs": 2, "records": 2, "submitted": 0, "failed_installs": 2, "pulse_nudges": 0, "pulse_failed_installs": 0}
    errs = [r for r in caplog.records if r.msg == "reaction_poll_all_installs_failed"]
    assert errs and errs[0].levelno == _logging.ERROR
    # a partial failure (not ALL) must NOT escalate to error
    assert not any(r.msg == "reaction_poll_cycle_complete" and r.levelno >= _logging.ERROR
                   for r in caplog.records)


def test_poller_no_installs_is_a_clean_noop(monkeypatch):
    _wire(
        monkeypatch,
        installs=[],
        records_for=lambda iid: [{"x": 1}],
        retry=lambda iid, fn: fn("tok"),
        poll=lambda *a, **k: 1,
    )
    out = poller_handler.handler({}, None)
    assert out == {"installs": 0, "records": 0, "submitted": 0, "failed_installs": 0, "pulse_nudges": 0, "pulse_failed_installs": 0}
