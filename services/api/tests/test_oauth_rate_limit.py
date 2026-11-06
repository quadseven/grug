"""Per-client-IP rate limit on /auth/github/login and /auth/github/callback (#1680).

The routes run through a real FastAPI app so the tests prove the limit is
wired onto the routes, not just that a helper exists. The slot store is
replaced by an in-memory fake that keeps one counter per name and minute
bucket, the same windowing the Postgres store uses. One test at the end
runs the same flow against the real store and skips without
GRUG_TEST_DATABASE_URL, as the other store-backed tests do.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from adapters.pg_rate_limit_store import ReservationResult

_LOGIN = "/api/v1/auth/github/login"
_CALLBACK = "/api/v1/auth/github/callback"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class _FakeSlots:
    """Per-name, per-minute counter with the store's admission rule."""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, int], int] = defaultdict(int)
        self.names: list[str] = []

    def __call__(self, *, name, minute_limit, day_limit, now=None):
        assert now is not None, "the limiter must pass its clock to the store"
        self.names.append(name)
        key = (name, int(now.timestamp() // 60))
        if self.counts[key] >= minute_limit:
            return ReservationResult(admitted=False, minute_count=self.counts[key], day_count=0)
        self.counts[key] += 1
        return ReservationResult(admitted=True, minute_count=self.counts[key], day_count=0)


@pytest.fixture
def oauth(monkeypatch):
    monkeypatch.setenv("GRUG_DOMAIN", "grug.lol")
    import auth.github_oauth as mod

    monkeypatch.setattr(mod, "_state_secret", lambda: "test-secret-v1")
    monkeypatch.setattr(mod, "_client_id", lambda: "Iv1.testclientid")
    monkeypatch.setattr(mod, "_client_secret", lambda: "test-client-secret")
    slots = _FakeSlots()
    monkeypatch.setattr(mod, "try_reserve_slot", slots)
    # 12:00:15 UTC: 45 seconds remain in the minute window.
    clock = _Clock(datetime(2026, 1, 1, 12, 0, 15, tzinfo=timezone.utc))
    monkeypatch.setattr(mod, "_now", clock)
    app = FastAPI()
    app.include_router(mod.router)
    client = TestClient(app)
    return mod, client, slots, clock


def _login(client, ip="203.0.113.7"):
    return client.get(_LOGIN, headers={"X-Grug-Client-IP": ip}, follow_redirects=False)


def _callback(client, ip="203.0.113.7"):
    return client.get(
        _CALLBACK, params={"code": "junk", "state": "forged"},
        headers={"X-Grug-Client-IP": ip}, follow_redirects=False,
    )


def test_default_limits_match_the_ticket(oauth):
    mod, *_ = oauth
    assert mod._LOGIN_PER_MINUTE == 20
    assert mod._CALLBACK_PER_MINUTE == 10


def test_login_admits_up_to_the_limit_then_answers_429_with_retry_after(oauth):
    mod, client, _slots, _clock = oauth
    for _ in range(mod._LOGIN_PER_MINUTE):
        assert _login(client).status_code == 302
    resp = _login(client)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "45"


def test_callback_counts_junk_codes_before_the_state_check(oauth):
    """A forged state is a 400 inside the limit and a 429 past it, so the
    check runs before the CSRF state check."""
    mod, client, slots, _clock = oauth
    for _ in range(mod._CALLBACK_PER_MINUTE):
        assert _callback(client).status_code == 400
    resp = _callback(client)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "45"
    assert set(slots.names) == {"oauth_callback:203.0.113.7"}


def test_login_and_callback_count_separately(oauth):
    mod, client, _slots, _clock = oauth
    for _ in range(mod._CALLBACK_PER_MINUTE):
        _callback(client)
    assert _callback(client).status_code == 429
    assert _login(client).status_code == 302


def test_limit_resets_in_the_next_minute_window(oauth):
    mod, client, _slots, clock = oauth
    for _ in range(mod._LOGIN_PER_MINUTE):
        _login(client)
    assert _login(client).status_code == 429
    clock.now = datetime(2026, 1, 1, 12, 1, 0, tzinfo=timezone.utc)
    assert _login(client).status_code == 302


def test_each_ip_has_its_own_budget(oauth):
    mod, client, _slots, _clock = oauth
    for _ in range(mod._LOGIN_PER_MINUTE):
        _login(client, ip="203.0.113.7")
    assert _login(client, ip="203.0.113.7").status_code == 429
    assert _login(client, ip="198.51.100.9").status_code == 302


def test_limits_are_read_from_settings(oauth, monkeypatch):
    mod, client, _slots, _clock = oauth
    monkeypatch.setattr(mod, "_LOGIN_PER_MINUTE", 2)
    assert _login(client).status_code == 302
    assert _login(client).status_code == 302
    assert _login(client).status_code == 429


@pytest.mark.parametrize("fault", [psycopg.OperationalError("down"), OSError("reset")])
def test_store_fault_fails_open_and_logs_a_warning(oauth, monkeypatch, caplog, fault):
    mod, client, _slots, _clock = oauth

    def _broken(**_kwargs):
        raise fault

    monkeypatch.setattr(mod, "try_reserve_slot", _broken)
    with caplog.at_level(logging.WARNING, logger="grug.api.auth"):
        login = _login(client)
        callback = _callback(client)
    assert login.status_code == 302
    assert callback.status_code == 400  # reached the state check
    warnings = [r for r in caplog.records if r.getMessage() == "oauth_rate_limit_store_unavailable"]
    assert len(warnings) == 2
    assert {r.levelno for r in warnings} == {logging.WARNING}


def test_ip_comes_from_the_worker_set_header(oauth):
    _mod, client, slots, _clock = oauth
    _login(client, ip="198.51.100.20")
    assert slots.names == ["oauth_login:198.51.100.20"]


def test_cf_connecting_ip_does_not_choose_the_bucket(oauth):
    """On the Worker's cross-zone subrequest CF-Connecting-IP is the
    Worker's own address, and a client can send any value; neither may
    move the request to another bucket."""
    _mod, client, slots, _clock = oauth
    client.get(_LOGIN, headers={"CF-Connecting-IP": "198.51.100.66"}, follow_redirects=False)
    client.get(
        _LOGIN, headers={"CF-Connecting-IP": "198.51.100.66", "X-Grug-Client-IP": "203.0.113.7"},
        follow_redirects=False,
    )
    assert slots.names == ["oauth_login:testclient", "oauth_login:203.0.113.7"]


def test_ipv6_clients_share_a_bucket_per_64(oauth):
    _mod, client, slots, _clock = oauth
    _login(client, ip="2001:db8:1:2::1")
    _login(client, ip="2001:db8:1:2:ffff::9")
    assert slots.names == ["oauth_login:2001:db8:1:2::/64"] * 2


def test_unparseable_header_falls_back_to_the_socket_peer(oauth):
    _mod, client, slots, _clock = oauth
    _login(client, ip="not-an-ip")
    assert slots.names == ["oauth_login:testclient"]


def test_rate_limited_log_line_carries_no_ip(oauth, monkeypatch, caplog):
    mod, client, _slots, _clock = oauth
    monkeypatch.setattr(mod, "_LOGIN_PER_MINUTE", 0)
    with caplog.at_level(logging.INFO, logger="grug.api.auth"):
        assert _login(client).status_code == 429
    records = [r for r in caplog.records if r.getMessage() == "oauth_rate_limited"]
    assert len(records) == 1
    assert "203.0.113.7" not in str(records[0].__dict__)


def test_ip_falls_back_to_the_socket_peer(oauth):
    _mod, client, slots, _clock = oauth
    client.get(_LOGIN, follow_redirects=False)
    # TestClient reports its peer as host "testclient".
    assert slots.names == ["oauth_login:testclient"]


def test_retry_after_is_at_least_one_second(oauth, monkeypatch):
    mod, client, _slots, clock = oauth
    clock.now = datetime(2026, 1, 1, 12, 0, 59, 999000, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "_LOGIN_PER_MINUTE", 0)
    resp = _login(client)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "1"


def test_limit_trips_against_the_real_store(pg_store, monkeypatch):
    monkeypatch.setenv("GRUG_DOMAIN", "grug.lol")
    import auth.github_oauth as mod

    monkeypatch.setattr(mod, "_state_secret", lambda: "test-secret-v1")
    monkeypatch.setattr(mod, "_client_id", lambda: "Iv1.testclientid")
    monkeypatch.setattr(mod, "_CALLBACK_PER_MINUTE", 3)
    app = FastAPI()
    app.include_router(mod.router)
    client = TestClient(app)
    responses = [_callback(client) for _ in range(4)]
    assert [r.status_code for r in responses] == [400, 400, 400, 429]
    assert 1 <= int(responses[-1].headers["Retry-After"]) <= 60
