"""The reaction poll must not scale with every comment grug ever posted.

2026-10-05: the scheduled poll re-fetched the reactions on ALL ~980 comment
records (30-day TTL) every 15 minutes, about 1,000 GitHub calls per run and
4,100 an hour, 70% of the installation's 6,200/hour budget before any review
traffic. A burst of deploys (each smoke job ran a full copy) pushed the hour
past the limit, GitHub answered 403 to every read and write until the window
reset, and Chief could not publish. Old comments are re-checked far less
often; the schedule is stateless (spread by comment id).
"""

from __future__ import annotations

import httpx

import poller_handler
from personas.code_reviewer import reaction_cadence as rc

DAY = 86400
NOW = 1_800_000_000.0


def _rec(comment_id: int, age_days: float | None, at: float = NOW) -> dict:
    """A record that is `age_days` old at time `at`."""
    rec = {"comment_id": comment_id, "repo": "o/r", "pr_number": 1}
    if age_days is not None:
        rec["ttl"] = int(at - age_days * DAY + rc.RECORD_TTL_DAYS * DAY)
    return rec


def _due_count(age_days: float, ticks: int) -> int:
    """Runs (out of `ticks`) on which a record held at `age_days` is due."""
    n = 0
    for t in range(ticks):
        now = NOW + t * rc.TICK_SECONDS
        if rc.reaction_poll_due(_rec(7, age_days, now), now):
            n += 1
    return n


def test_a_fresh_comment_is_polled_every_run():
    assert _due_count(0.2, 96) == 96


def test_polling_backs_off_with_age():
    # polls per day (96 runs): fresh > 1-3d > 3-14d > 14-30d
    day1, day2, week, month = (_due_count(a, 96) for a in (0.5, 2, 8, 20))
    assert day1 == 96
    assert day2 == 24   # hourly
    assert week == 4    # every 6 hours
    assert month == 1   # daily


def test_unknown_age_is_polled_every_run():
    assert rc.reaction_poll_due(_rec(7, None), NOW)


def test_the_schedule_spreads_across_runs_not_all_at_once():
    per_tick = []
    for t in range(96):
        now = NOW + t * rc.TICK_SECONDS
        per_tick.append(sum(
            rc.reaction_poll_due(_rec(cid, 20, now), now) for cid in range(1000, 1960)
        ))
    assert max(per_tick) < 30          # ~10 expected, never a stampede
    assert sum(per_tick) == 960        # each of the 960 polled exactly once a day


def test_the_scan_cost_of_a_full_month_of_records_is_small():
    # ~33 comments a day for 30 days (the live shape: 983 records)
    total = 0
    for t in range(96):
        now = NOW + t * rc.TICK_SECONDS
        total += sum(
            rc.reaction_poll_due(_rec(i, (i % 30) + 0.5, now), now) for i in range(990)
        )
    per_run = total / 96
    assert per_run < 120               # was 990 every run


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "u"), response=httpx.Response(self.status_code))


def _wire_pass(monkeypatch, records, polled, *, rate=None):
    monkeypatch.setattr(poller_handler, "list_comment_records", lambda iid: records)
    monkeypatch.setattr(poller_handler, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(
        poller_handler, "poll_and_annotate",
        lambda recs, **kw: polled.extend(r["comment_id"] for r in recs) or 0,
    )
    monkeypatch.setattr(poller_handler, "_github_core_budget", lambda token: rate)
    monkeypatch.setattr(poller_handler, "_now_epoch", lambda: NOW)


def test_the_poll_pass_only_polls_records_that_are_due(monkeypatch):
    recs = [_rec(1, 0.1), _rec(2, 25)]   # fresh: due; 25d old: due once a day
    polled: list[int] = []
    _wire_pass(monkeypatch, recs, polled, rate=(6000, 6200))
    tick = int(NOW // rc.TICK_SECONDS)
    expect = [r["comment_id"] for r in recs if rc.reaction_poll_due(r, NOW)]
    poller_handler._reaction_poll_pass([111])
    assert polled == expect and 1 in polled
    assert len(polled) <= 2 and tick >= 0


def test_a_low_budget_skips_the_whole_poll_and_says_so(monkeypatch, caplog):
    """Chief and Elder are why the budget exists; the reaction poll is
    calibration data and yields first."""
    polled: list[int] = []
    _wire_pass(monkeypatch, [_rec(1, 0.1)], polled, rate=(1000, 6200))
    with caplog.at_level("WARNING"):
        out = poller_handler._reaction_poll_pass([111])
    assert polled == [] and out[0] == 0
    ev = [r for r in caplog.records if r.getMessage() == "reaction_poll_skipped_low_budget"]
    assert ev and ev[0].remaining == 1000 and ev[0].limit == 6200


def test_an_unreadable_budget_never_blocks_the_poll(monkeypatch):
    polled: list[int] = []
    _wire_pass(monkeypatch, [_rec(1, 0.1)], polled, rate=None)
    poller_handler._reaction_poll_pass([111])
    assert polled == [1]


def test_core_budget_reads_the_rate_limit_endpoint(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        return _Resp({"resources": {"core": {"remaining": 4321, "limit": 6200}}})

    monkeypatch.setattr(poller_handler.httpx, "get", fake_get)
    assert poller_handler._github_core_budget("tok") == (4321, 6200)
    assert seen["url"].endswith("/rate_limit")
    monkeypatch.setattr(poller_handler.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert poller_handler._github_core_budget("tok") is None
