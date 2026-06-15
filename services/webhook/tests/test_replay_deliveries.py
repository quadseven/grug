"""Tests for the manual delivery-replay CLI (#407).

The replay logic itself is tested in test_delivery_replay.py; here we cover the
CLI's own seams: window resolution (--since vs --hours) and the exit-code
contract (non-zero on partial failure)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import replay_deliveries as cli


def test_since_iso_prefers_explicit_since():
    assert cli._since_iso(since="2026-06-14T20:00:00Z") == "2026-06-14T20:00:00Z"


def test_since_iso_computes_from_hours_with_injected_now():
    now = datetime(2026, 6, 14, 23, 0, 0, tzinfo=timezone.utc)
    assert cli._since_iso(hours=6, now=now) == "2026-06-14T17:00:00Z"


def test_parse_args_requires_exactly_one_window():
    with pytest.raises(SystemExit):
        cli._parse_args([])  # neither --since nor --hours
    with pytest.raises(SystemExit):
        cli._parse_args(["--since", "x", "--hours", "6"])  # mutually exclusive


def test_parse_args_rejects_non_positive_hours():
    # a future window-start would silently scan nothing - must fail loudly
    with pytest.raises(SystemExit):
        cli._parse_args(["--hours", "-5"])
    with pytest.raises(SystemExit):
        cli._parse_args(["--hours", "0"])


def test_main_exit_zero_when_no_errors(monkeypatch, capsys):
    import delivery_replay

    monkeypatch.setattr(
        cli.delivery_replay, "replay_since",
        lambda since: delivery_replay.ReplayReport(scanned=3, failed_guids=1, redelivered=1, errors=0),
    )
    rc = cli.main(["--since", "2026-06-14T20:00:00Z"])
    assert rc == 0
    assert "redelivered=1" in capsys.readouterr().out


def test_main_exit_nonzero_on_partial_failure(monkeypatch):
    import delivery_replay

    monkeypatch.setattr(
        cli.delivery_replay, "replay_since",
        lambda since: delivery_replay.ReplayReport(scanned=2, failed_guids=2, redelivered=1, errors=1),
    )
    assert cli.main(["--hours", "6"]) == 1
