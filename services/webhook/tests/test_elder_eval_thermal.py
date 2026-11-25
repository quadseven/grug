"""Thermal pacing for the Elder eval against the Sparks (grug#989).

A review-sized call takes a Spark's hottest sensor up by several degrees on
its own, and the Sparks have cut power under sustained load. The rule on
grug#989: start each case only when every Spark is cool enough, and stop the
run when one is too hot. Each Spark's host thermal guard serves its
reading as JSON (`hottest_c`); thresholds live in `elder_eval.thermal`.
"""

from __future__ import annotations

import pytest

from elder_eval.thermal import ThermalAbort, ThermalPacer, pacer_from_env

A = "http://spark-a.example.com:11439/status"
B = "http://spark-b.example.com:11439/status"


class _Feed:
    """Serves queued readings per URL; the last reading repeats."""

    def __init__(self, readings: dict[str, list[object]]) -> None:
        self.readings = {url: list(values) for url, values in readings.items()}
        self.calls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.calls.append(url)
        queue = self.readings[url]
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(value, Exception):
            raise value
        return {"hottest_c": value}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def time(self) -> float:
        return self.now


def _pacer(feed: _Feed, clock: _Clock, **kw) -> ThermalPacer:
    return ThermalPacer(
        (A, B), read=feed, sleep=clock.sleep, clock=clock.time, **kw
    )


def test_a_cool_fleet_starts_the_case_at_once():
    feed, clock = _Feed({A: [51.3], B: [47.8]}), _Clock()
    _pacer(feed, clock)("case-1")
    assert clock.slept == []
    assert sorted(feed.calls) == [A, B]


def test_the_case_waits_until_every_spark_reads_65_or_below():
    feed, clock = _Feed({A: [70.0, 66.0, 65.0], B: [60.0]}), _Clock()
    _pacer(feed, clock, poll_s=15.0)("case-1")
    assert clock.slept == [15.0, 15.0]


def test_a_spark_above_82_stops_the_run():
    feed, clock = _Feed({A: [50.0], B: [82.1]}), _Clock()
    with pytest.raises(ThermalAbort, match="guard 2 .*82.1"):
        _pacer(feed, clock)("case-1")


def test_a_spark_that_heats_past_82_while_waiting_stops_the_run():
    feed, clock = _Feed({A: [75.0, 79.0, 83.0], B: [50.0]}), _Clock()
    with pytest.raises(ThermalAbort, match="83.0"):
        _pacer(feed, clock)("case-1")


def test_82_exactly_waits_rather_than_stops():
    feed, clock = _Feed({A: [82.0, 64.0], B: [50.0]}), _Clock()
    _pacer(feed, clock, poll_s=15.0)("case-1")
    assert clock.slept == [15.0]


def test_an_unreadable_guard_stops_the_run():
    """No reading is not a cool reading: the run cannot keep its rule blind."""
    feed, clock = _Feed({A: [OSError("refused")], B: [50.0]}), _Clock()
    with pytest.raises(ThermalAbort, match="guard 1 unreadable"):
        _pacer(feed, clock)("case-1")


def test_a_reading_with_no_temperature_stops_the_run():
    feed, clock = _Feed({A: [None], B: [50.0]}), _Clock()
    with pytest.raises(ThermalAbort, match="guard 1 gave no"):
        _pacer(feed, clock)("case-1")


def test_a_fleet_that_never_cools_stops_the_run_at_the_wait_limit():
    feed, clock = _Feed({A: [70.0], B: [50.0]}), _Clock()
    with pytest.raises(ThermalAbort, match="did not cool"):
        _pacer(feed, clock, poll_s=60.0, max_wait_s=300.0)("case-1")
    assert sum(clock.slept) <= 300.0


def test_messages_never_carry_a_guard_url():
    """The URLs are private addresses; a public repo's Actions logs are public."""
    feed, clock = _Feed({A: [OSError("refused")], B: [50.0]}), _Clock()
    with pytest.raises(ThermalAbort) as unreadable:
        _pacer(feed, clock)("case-1")
    feed, clock = _Feed({A: [70.0], B: [50.0]}), _Clock()
    with pytest.raises(ThermalAbort) as never_cooled:
        _pacer(feed, clock, poll_s=60.0, max_wait_s=60.0)("case-1")
    for err in (unreadable.value, never_cooled.value):
        assert "spark-a" not in str(err) and "http" not in str(err)


def test_the_pacer_needs_at_least_one_guard():
    with pytest.raises(ValueError):
        ThermalPacer(())


def test_the_env_names_the_guards_comma_separated():
    pacer = pacer_from_env({"GRUG_BENCH_THERMAL_URLS": f" {A}, {B} ,"})
    assert pacer is not None
    assert pacer.urls == (A, B)


def test_no_env_means_no_pacing():
    assert pacer_from_env({}) is None
    assert pacer_from_env({"GRUG_BENCH_THERMAL_URLS": " "}) is None


# --- wiring: run_eval paces each case; the CLI stops with no baseline -------


def _cases():
    from elder_eval.corpus import build_cases
    from ledger import LedgerRow

    rows = [
        LedgerRow(
            repo="quadseven/grug", pr=pr, reviewer="codex", severity="HIGH",
            finding_class="correctness", finding="synthetic finding",
            verdict="fixed",
        )
        for pr in (1, 2)
    ]
    return build_cases(rows)


def _backend():
    from sast_benchmark.backends import BenchBackend

    return BenchBackend(name="fake", url="http://invalid", model="m", api_key="")


def test_run_eval_asks_the_pacer_before_every_case():
    from elder_eval.runner import run_eval

    paced: list[str] = []

    def failing_fetch(repo, pr, token):
        raise ValueError("no network in tests")

    run_eval(_backend(), _cases(), fetch=failing_fetch, before_case=paced.append)
    assert len(paced) == 2


def test_a_thermal_abort_ends_run_eval_before_the_next_case():
    from elder_eval.runner import run_eval

    fetched: list[int] = []

    def fetch(repo, pr, token):
        fetched.append(pr)
        raise ValueError("no network in tests")

    calls = iter([None])

    def pacer(case_id):
        if next(calls, "hot") == "hot":
            raise ThermalAbort("too hot")

    with pytest.raises(ThermalAbort):
        run_eval(_backend(), _cases(), fetch=fetch, before_case=pacer)
    assert len(fetched) == 1


def test_main_stops_with_exit_4_and_writes_no_baseline(monkeypatch, capsys):
    from elder_eval import __main__ as cli

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GRUG_BENCH_THERMAL_URLS", A)

    def run(*_a, **kw):
        assert isinstance(kw.get("before_case"), ThermalPacer)
        raise ThermalAbort("spark-a reads 84.6 C")

    monkeypatch.setattr(cli, "_run_requested_mode", run)
    monkeypatch.setattr(
        cli, "_record_report",
        lambda *_a: pytest.fail("an aborted run must not record"),
    )

    assert cli.main(["--record"]) == 4
    assert "84.6" in capsys.readouterr().err


def test_an_ab_arm_is_paced_and_its_abort_records_nothing(monkeypatch, capsys):
    from elder_eval import __main__ as cli
    from types import SimpleNamespace

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GRUG_BENCH_THERMAL_URLS", A)
    cases = _cases()
    monkeypatch.setattr(cli, "_load_corpus", lambda _a: ([], cases, cases))
    monkeypatch.setattr(
        cli, "_run_requested_mode",
        lambda *_a, **_k: (_backend(), "fake", {}),
    )
    monkeypatch.setattr(cli, "score", lambda *_a: SimpleNamespace(all_errored=False))

    def arms(*_a, before_case=None):
        assert isinstance(before_case, ThermalPacer)
        raise ThermalAbort("spark-b reads 83.0 C")

    monkeypatch.setattr(cli, "_run_ab_modes", arms)
    monkeypatch.setattr(cli, "_print_report", lambda *_a: None)
    monkeypatch.setattr(
        cli, "_record_report",
        lambda *_a: pytest.fail("an aborted run must not record"),
    )

    assert cli.main(["--record"]) == 4
    assert "83.0" in capsys.readouterr().err



def _cave():
    from sast_benchmark.backends import BenchBackend

    return BenchBackend(name="cave", url="http://invalid", model="m", api_key="")


def _args(**kw):
    from types import SimpleNamespace

    base = dict(published=False, production=False, backend=None,
                ab_practices=False, ab_few_shot=False)
    return SimpleNamespace(**{**base, **kw})


def test_the_cave_backend_refuses_to_run_unpaced(monkeypatch, capsys):
    from elder_eval import __main__ as cli

    monkeypatch.setattr(
        "sast_benchmark.backends.configured_backends", lambda: [_cave()]
    )
    monkeypatch.setattr(
        cli, "run_eval", lambda *_a, **_k: pytest.fail("must not replay unpaced")
    )
    assert cli._run_requested_mode(None, _args(), _cases(), "t") == (None, "", None)
    assert "GRUG_BENCH_THERMAL_URLS" in capsys.readouterr().err


def test_a_cloud_backend_is_never_paced(monkeypatch):
    from elder_eval import __main__ as cli

    seen = {}
    monkeypatch.setattr(
        "sast_benchmark.backends.configured_backends", lambda: [_backend()]
    )
    monkeypatch.setattr(
        cli, "run_eval", lambda *_a, **kw: seen.update(kw) or {}
    )
    cli._run_requested_mode(None, _args(), _cases(), "t", before_case=print)
    assert seen["before_case"] is None


def test_the_production_replay_is_paced(monkeypatch):
    from elder_eval.runner import run_production_eval

    paced: list[str] = []

    def failing_fetch(repo, pr, token):
        raise ValueError("no network in tests")

    run_production_eval(_cases(), fetch=failing_fetch, before_case=paced.append)
    assert len(paced) == 2


def test_a_spark_hot_after_the_last_case_voids_the_run(monkeypatch, capsys):
    from types import SimpleNamespace

    from elder_eval import __main__ as cli

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GRUG_BENCH_THERMAL_URLS", A)
    cases = _cases()
    monkeypatch.setattr(cli, "_load_corpus", lambda _a: ([], cases, cases))
    readings = iter([50.0, 50.0, 84.6])
    monkeypatch.setattr(
        "elder_eval.thermal.read_guard",
        lambda _url: {"hottest_c": next(readings)},
    )

    def run(_parser, _args, cases, _token, before_case=None):
        for case in cases:
            before_case(case.case_id)
        return _cave(), "cave", {}

    monkeypatch.setattr(cli, "_run_requested_mode", run)
    monkeypatch.setattr(cli, "score", lambda *_a: SimpleNamespace(all_errored=False))
    monkeypatch.setattr(cli, "_print_report", lambda *_a: None)
    monkeypatch.setattr(cli, "_run_ab_modes", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cli, "_record_report",
        lambda *_a: pytest.fail("a run that ended hot must not record"),
    )

    assert cli.main(["--record"]) == 4
    assert "after the last case" in capsys.readouterr().err


def test_a_real_guard_read_logs_no_url(caplog):
    """The CLI logs at INFO into public Actions logs; a guard read must not
    put the private address there."""
    import json
    import logging
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Guard(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"hottest_c": 50.0}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Guard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/status"
    try:
        with caplog.at_level(logging.DEBUG):
            ThermalPacer((url,))("case-1")
    finally:
        server.shutdown()
    assert str(server.server_port) not in caplog.text
