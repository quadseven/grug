"""Tests for missed-delivery replay (#407).

The deep, load-bearing logic is `guids_needing_replay` - the idempotency
rule that an event whose delivery eventually SUCCEEDED is never replayed
(acceptance #4), keyed on the shared `guid`. The HTTP wrappers are tested
with an injected fake `http` module (no real GitHub), matching the
github_app_auth test style.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import delivery_replay as dr


def _d(id_, guid, status_code, delivered_at, event="pull_request", redelivery=False):
    return dr.Delivery(
        id=id_,
        guid=guid,
        status_code=status_code,
        delivered_at=delivered_at,
        event=event,
        redelivery=redelivery,
    )


# --- _is_success boundaries -------------------------------------------------


@pytest.mark.parametrize(
    "code,ok",
    [(200, True), (204, True), (302, True), (399, True), (400, False),
     (500, False), (0, False), (-1, False)],
)
def test_is_success_uses_2xx_3xx_range(code, ok):
    assert dr._is_success(code) is ok


# --- guids_needing_replay (PURE idempotency core) ---------------------------


def test_failed_only_guid_is_selected():
    ds = [_d(1, "g1", 500, "2026-06-14T20:00:00Z")]
    assert dr.guids_needing_replay(ds) == [1]


def test_succeeded_guid_is_skipped():
    ds = [_d(1, "g1", 200, "2026-06-14T20:00:00Z")]
    assert dr.guids_needing_replay(ds) == []


def test_guid_with_later_success_is_skipped_idempotent():
    # Original failed, a redelivery later succeeded -> the event WAS delivered,
    # so it must NOT be replayed again (acceptance #4, no duplicate check).
    ds = [
        _d(1, "g1", 500, "2026-06-14T20:00:00Z", redelivery=False),
        _d(2, "g1", 200, "2026-06-14T20:05:00Z", redelivery=True),
    ]
    assert dr.guids_needing_replay(ds) == []


def test_all_failed_guid_picks_latest_attempt_id():
    ds = [
        _d(1, "g1", 500, "2026-06-14T20:00:00Z"),
        _d(2, "g1", 502, "2026-06-14T20:05:00Z", redelivery=True),
    ]
    assert dr.guids_needing_replay(ds) == [2]


def test_mixed_guids_only_unsucceeded_returned():
    ds = [
        _d(1, "ok", 200, "2026-06-14T20:00:00Z"),
        _d(2, "bad", 503, "2026-06-14T20:01:00Z"),
        _d(3, "conn", 0, "2026-06-14T20:02:00Z"),
    ]
    assert sorted(dr.guids_needing_replay(ds)) == [2, 3]


# --- list_deliveries_since (paginates, stops at window, parses) -------------


class _Resp:
    def __init__(self, payload, links=None):
        self._payload = payload
        self.links = links or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_list_paginates_until_since_boundary():
    page1 = [
        {"id": 10, "guid": "g10", "status_code": 500, "delivered_at": "2026-06-14T21:00:00Z", "event": "pull_request", "redelivery": False},
        {"id": 9, "guid": "g9", "status_code": 200, "delivered_at": "2026-06-14T20:30:00Z", "event": "push", "redelivery": False},
    ]
    page2 = [
        {"id": 8, "guid": "g8", "status_code": 500, "delivered_at": "2026-06-14T19:00:00Z", "event": "pull_request", "redelivery": False},
    ]
    calls = []

    def _get(url, headers=None, params=None, timeout=None):
        calls.append(params or {})
        if "cursor" not in (params or {}):
            return _Resp(page1, links={"next": {"url": "https://api.github.com/app/hook/deliveries?cursor=CUR2"}})
        return _Resp(page2)

    http = SimpleNamespace(get=_get)
    out = dr.list_deliveries_since(
        "2026-06-14T20:00:00Z", http=http, jwt_token="jwt"
    )
    # g10 + g9 kept (>= since); g8 at 19:00 is < since -> stop, excluded.
    assert [d.id for d in out] == [10, 9]
    # second page WAS fetched with the cursor from the Link header
    assert any(c.get("cursor") == "CUR2" for c in calls)


def test_list_sends_app_jwt_bearer():
    captured = {}

    def _get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp([])

    http = SimpleNamespace(get=_get)
    dr.list_deliveries_since("2026-06-14T20:00:00Z", http=http, jwt_token="jwt-xyz")
    assert captured["url"].endswith("/app/hook/deliveries")
    assert captured["headers"]["Authorization"] == "Bearer jwt-xyz"


# --- redeliver --------------------------------------------------------------


def test_redeliver_posts_attempts_endpoint_with_bearer():
    captured = {}

    def _post(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp({})

    http = SimpleNamespace(post=_post)
    dr.redeliver(42, http=http, jwt_token="jwt-xyz")
    assert captured["url"].endswith("/app/hook/deliveries/42/attempts")
    assert captured["headers"]["Authorization"] == "Bearer jwt-xyz"


# --- replay_since (orchestrator, best-effort) -------------------------------


def test_replay_since_redelivers_only_failed_guids(monkeypatch):
    monkeypatch.setattr(dr, "get_app_jwt", lambda: "jwt")
    deliveries = [
        {"id": 1, "guid": "ok", "status_code": 200, "delivered_at": "2026-06-14T20:00:00Z", "event": "pull_request", "redelivery": False},
        {"id": 2, "guid": "bad", "status_code": 500, "delivered_at": "2026-06-14T20:01:00Z", "event": "pull_request", "redelivery": False},
    ]
    posted = []

    def _get(url, headers=None, params=None, timeout=None):
        return _Resp(deliveries)

    def _post(url, headers=None, timeout=None):
        posted.append(url)
        return _Resp({})

    http = SimpleNamespace(get=_get, post=_post)
    report = dr.replay_since("2026-06-14T20:00:00Z", http=http)
    assert report.scanned == 2
    assert report.failed_guids == 1
    assert report.redelivered == 1
    assert report.errors == 0
    assert posted == ["https://api.github.com/app/hook/deliveries/2/attempts"]


def test_replay_since_is_best_effort_on_redeliver_error(monkeypatch):
    monkeypatch.setattr(dr, "get_app_jwt", lambda: "jwt")
    deliveries = [
        {"id": 2, "guid": "a", "status_code": 500, "delivered_at": "2026-06-14T20:01:00Z", "event": "pull_request", "redelivery": False},
        {"id": 3, "guid": "b", "status_code": 500, "delivered_at": "2026-06-14T20:02:00Z", "event": "pull_request", "redelivery": False},
    ]
    attempts = []

    def _get(url, headers=None, params=None, timeout=None):
        return _Resp(deliveries)

    def _post(url, headers=None, timeout=None):
        attempts.append(url)
        if url.endswith("/2/attempts"):
            raise RuntimeError("boom")
        return _Resp({})

    http = SimpleNamespace(get=_get, post=_post)
    report = dr.replay_since("2026-06-14T20:00:00Z", http=http)
    # both attempted despite the first raising; 1 ok, 1 error
    assert len(attempts) == 2
    assert report.redelivered == 1
    assert report.errors == 1


# --- audit hardening: systemic failure, fractional time, malformed rows ------


def test_replay_since_escalates_when_all_redelivers_fail(monkeypatch, caplog):
    """Audit H1: events to replay but EVERY redeliver fails (broken App-JWT) =
    recovery is dead -> log.error (not a pile of warnings that read as one
    blip), so a status:error monitor fires."""
    import logging

    monkeypatch.setattr(dr, "get_app_jwt", lambda: "jwt")
    deliveries = [
        {"id": 2, "guid": "a", "status_code": 500, "delivered_at": "2026-06-14T20:01:00Z", "event": "pull_request", "redelivery": False},
    ]

    def _get(url, headers=None, params=None, timeout=None):
        return _Resp(deliveries)

    def _post(url, headers=None, timeout=None):
        raise RuntimeError("401")

    http = SimpleNamespace(get=_get, post=_post)
    with caplog.at_level(logging.INFO):
        report = dr.replay_since("2026-06-14T20:00:00Z", http=http)
    assert report.failed_guids == 1 and report.redelivered == 0
    errs = [r for r in caplog.records if r.msg == "delivery_replay_all_redelivers_failed"]
    assert errs and errs[0].levelno == logging.ERROR
    assert not any(r.msg == "delivery_replay_done" for r in caplog.records)


def test_list_window_boundary_uses_instants_not_string_compare():
    """Audit M3: GitHub delivered_at has FRACTIONAL seconds; a delivery at
    20:00:00.500Z is AFTER since=20:00:00Z and must be KEPT (lexicographic
    compare wrongly excludes it because '.' < 'Z')."""
    page = [
        {"id": 5, "guid": "g5", "status_code": 500, "delivered_at": "2026-06-14T20:00:00.500Z", "event": "pull_request", "redelivery": False},
    ]
    http = SimpleNamespace(get=lambda *a, **k: _Resp(page))
    out = dr.list_deliveries_since("2026-06-14T20:00:00Z", http=http, jwt_token="jwt")
    assert [d.id for d in out] == [5]


def test_list_skips_malformed_row_without_aborting_window():
    """Audit M1: one row missing delivered_at must be skipped + logged, not
    abort the whole window (best-effort contract)."""
    page = [
        {"id": 1, "guid": "g1", "status_code": 500, "event": "pull_request", "redelivery": False},  # no delivered_at
        {"id": 2, "guid": "g2", "status_code": 500, "delivered_at": "2026-06-14T21:00:00Z", "event": "pull_request", "redelivery": False},
    ]
    http = SimpleNamespace(get=lambda *a, **k: _Resp(page))
    out = dr.list_deliveries_since("2026-06-14T20:00:00Z", http=http, jwt_token="jwt")
    assert [d.id for d in out] == [2]  # malformed row 1 skipped, row 2 kept


def test_list_logs_truncation_when_page_budget_exhausted(monkeypatch, caplog):
    """Audit M4: a window bigger than the page budget must log a truncation
    warning, not silently under-recover."""
    import logging

    monkeypatch.setattr(dr, "_MAX_PAGES", 2)
    full = [
        {"id": 9, "guid": "g9", "status_code": 500, "delivered_at": "2026-06-14T21:00:00Z", "event": "pull_request", "redelivery": False},
    ]
    # every page returns a next cursor -> loop exhausts the (patched) 2-page budget
    http = SimpleNamespace(
        get=lambda *a, **k: _Resp(full, links={"next": {"url": "https://api.github.com/app/hook/deliveries?cursor=X"}})
    )
    with caplog.at_level(logging.WARNING):
        dr.list_deliveries_since("2026-06-14T20:00:00Z", http=http, jwt_token="jwt")
    assert any(r.msg == "delivery_replay_window_truncated" for r in caplog.records)
