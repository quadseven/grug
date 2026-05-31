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
    assert out == {"installs": 2, "records": 2, "submitted": 4, "failed_installs": 0}


def test_poller_one_install_failure_does_not_abort_cycle(monkeypatch):
    """A single install's token/GH failure is logged + counted, and the
    cycle continues to the next install (best-effort per install)."""
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
    out = poller_handler.handler({}, None)
    assert out["installs"] == 2
    assert out["failed_installs"] == 1
    assert out["submitted"] == 3   # install 2 still polled despite install 1 failing


def test_poller_skips_installs_with_no_records(monkeypatch):
    """An install with no CommentRecords is skipped — no token fetch, no poll."""
    touched = []
    _wire(
        monkeypatch,
        installs=[7],
        records_for=lambda iid: [],
        retry=lambda iid, fn: touched.append(iid),
        poll=lambda *a, **k: 0,
    )
    out = poller_handler.handler({}, None)
    assert touched == []
    assert out == {"installs": 1, "records": 0, "submitted": 0, "failed_installs": 0}


def test_poller_no_installs_is_a_clean_noop(monkeypatch):
    _wire(
        monkeypatch,
        installs=[],
        records_for=lambda iid: [{"x": 1}],
        retry=lambda iid, fn: fn("tok"),
        poll=lambda *a, **k: 1,
    )
    out = poller_handler.handler({}, None)
    assert out == {"installs": 0, "records": 0, "submitted": 0, "failed_installs": 0}
