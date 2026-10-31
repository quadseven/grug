"""Grug slows non-urgent work before it reaches the GitHub limit."""
from __future__ import annotations

import logging

import github_budget
import poller_handler
import pytest


@pytest.fixture
def budget(monkeypatch):
    """Report a scripted budget and capture alerts claimed per window."""
    state = {"budget": (10000, 10000, 5000), "claims": set()}

    def _claim(install_id, reset):
        fresh = (install_id, reset) not in state["claims"]
        state["claims"].add((install_id, reset))
        return fresh

    monkeypatch.setattr(
        github_budget, "with_install_token_retry", lambda _iid, _fn: state["budget"],
    )
    monkeypatch.setattr("adapters.install_store.claim_budget_alert", _claim)
    monkeypatch.setattr(github_budget, "emit_gauge", lambda *a, **k: None)
    return state


@pytest.mark.parametrize(("remaining", "low"), [(4001, False), (4000, False), (3999, True)])
def test_non_urgent_work_waits_below_forty_percent(budget, remaining, low):
    budget["budget"] = (remaining, 10000, 5000)
    assert github_budget.budget_is_low(1) is low


def test_an_unreadable_budget_defers_nothing(monkeypatch):
    def _boom(*_a):
        raise RuntimeError("no token")

    monkeypatch.setattr(github_budget, "with_install_token_retry", _boom)
    assert github_budget.budget_is_low(1) is False
    monkeypatch.setattr(github_budget, "with_install_token_retry", lambda *_a: None)
    assert github_budget.budget_is_low(1) is False


def test_the_alert_fires_once_per_window(budget, caplog):
    budget["budget"] = (1000, 10000, 5000)
    with caplog.at_level(logging.WARNING, logger="grug.github_budget"):
        github_budget.budget_is_low(1)
        github_budget.budget_is_low(1)
        budget["budget"] = (1000, 10000, 8600)
        github_budget.budget_is_low(1)
    assert [r.msg for r in caplog.records].count("github_budget_low") == 2


def test_no_alert_above_twenty_percent(budget, caplog):
    budget["budget"] = (2000, 10000, 5000)
    with caplog.at_level(logging.WARNING, logger="grug.github_budget"):
        github_budget.budget_is_low(1)
    assert "github_budget_low" not in [r.msg for r in caplog.records]


def test_a_failed_alert_claim_still_answers(budget, monkeypatch):
    def _boom(*_a):
        raise OSError("store down")

    monkeypatch.setattr("adapters.install_store.claim_budget_alert", _boom)
    budget["budget"] = (100, 10000, 5000)
    assert github_budget.budget_is_low(1) is True


def _github_log(status: str) -> None:
    logging.getLogger("httpx").info(
        'HTTP Request: GET https://api.github.com/repos/o/r/pulls/1 "HTTP/1.1 %s"', status,
    )


def test_a_review_counts_charged_calls_only(monkeypatch, caplog):
    gauges: list[float] = []
    monkeypatch.setattr(github_budget, "emit_gauge", lambda _m, v, *a: gauges.append(v))
    httpx_log = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_log, "level", logging.INFO)
    with caplog.at_level(logging.INFO, logger="grug.github_budget"):
        with github_budget.count_review_calls("o/r", 1):
            _github_log("200 OK")
            _github_log("404 Not Found")
            _github_log("304 Not Modified")
            httpx_log.info('HTTP Request: POST http://cave.test/v1 "HTTP/1.1 200 OK"')
        _github_log("200 OK")
    assert gauges == [2.0]
    line = next(r for r in caplog.records if r.msg == "elder_review_github_calls")
    assert line.calls == 2


def test_a_low_budget_holds_reports_and_keeps_repairs(monkeypatch):
    """Scheduled reports wait. Chief self-heal and the check reconciler must
    still see the installation, or a review stays stuck for the whole hour."""
    seen: dict[str, list[int]] = {}

    def _pass(_installs, lister, *_a, **_k):
        seen[lister.__name__] = list(_installs)
        return 0, 0

    monkeypatch.setattr(poller_handler, "_prove_roles_anywhere_identity", lambda: None)
    monkeypatch.setattr(poller_handler, "list_allowlisted_installs", lambda: [1, 2])
    monkeypatch.setattr(poller_handler, "_reaction_poll_pass", lambda _i: (0, 0, 0))
    monkeypatch.setattr(poller_handler, "budget_is_low", lambda iid: iid == 1)
    monkeypatch.setattr(poller_handler, "_run_repo_scoped_pass", _pass)
    monkeypatch.setattr(
        poller_handler, "_enforcement_reemission_pass",
        lambda i: seen.__setitem__("enforcement", list(i)) or (0, 0),
    )
    monkeypatch.setattr(poller_handler, "_deep_publish_retry_pass", lambda: (0, 0))
    monkeypatch.setattr(poller_handler, "_install_reconciliation_pass", lambda: (0, 0, 0))
    monkeypatch.setattr(poller_handler, "_replay_missed_deliveries", lambda: {})
    monkeypatch.setattr(poller_handler, "_elder_canary_due", lambda: False)
    monkeypatch.setattr(
        "check_run_reconciler.reconcile_installs",
        lambda i: seen.__setitem__("reconcile", list(i)) or (0, 0),
    )
    monkeypatch.setattr(
        "chief_self_heal.self_heal_installs",
        lambda i: seen.__setitem__("self_heal", list(i)) or (0, 0),
    )
    poller_handler.handler({}, None)
    assert seen.pop("reconcile") == [1, 2]
    assert seen.pop("self_heal") == [1, 2]
    assert seen.pop("enforcement") == [1, 2], "ruleset detection never waits"
    assert seen and all(installs == [2] for installs in seen.values()), seen
