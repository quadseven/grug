"""Chief self-heal pass: a PR head with no `Grug - Chief` check-run is
re-run through the same function `/grug recheck` uses."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import chief_self_heal as heal
import poller_handler


def _iso(**delta) -> str:
    ts = datetime.now(timezone.utc) - timedelta(**delta)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr(number: int, *, minutes_ago: float = 30, sha: str | None = None) -> dict:
    return {
        "number": number,
        "body": f"body {number}",
        "updated_at": _iso(minutes=minutes_ago),
        "head": {"sha": sha or f"sha{number:03d}xxxx"},
    }


@pytest.fixture
def world(monkeypatch):
    """One install, one Chief-enabled repo; knobs and call recorders."""
    w = type("W", (), {})()
    w.prs = []
    w.runs_by_sha = {}
    w.rechecks = []
    w.check_lists = []
    w.recheck_result = "pass"
    w.recheck_raises = None
    w.enabled = True

    monkeypatch.setattr(heal, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(
        "github_rulesets_client.list_installation_repos",
        lambda token: [{"id": 7, "full_name": "o/r"}],
    )
    monkeypatch.setattr(heal, "is_persona_enabled", lambda i, r, k: w.enabled and k == "tpm")
    monkeypatch.setattr(heal, "_list_recent_open_prs", lambda t, o, r: list(w.prs))

    def _list_runs(token, owner, repo, sha):
        w.check_lists.append(sha)
        return w.runs_by_sha.get(sha, [])

    monkeypatch.setattr(heal, "list_check_runs_for_ref", _list_runs)

    def _recheck(**kw):
        w.rechecks.append(kw)
        if w.recheck_raises:
            raise w.recheck_raises
        return object(), {"persona": "tpm", "result": w.recheck_result}

    monkeypatch.setattr("dispatcher.run_chief_recheck", _recheck)
    w.metrics = []
    monkeypatch.setattr(
        "observability.emit_count",
        lambda metric, value=1, tags=None: w.metrics.append((metric, tags)),
    )
    return w


def test_missing_chief_is_republished_via_recheck_path(world, caplog):
    world.prs = [_pr(1)]
    with caplog.at_level(logging.INFO):
        published, failed = heal.self_heal_installs([99])
    assert (published, failed) == (1, 0)
    assert world.rechecks == [{
        "installation_id": 99, "owner": "o", "repo": "r",
        "head_sha": "sha001xxxx", "pr_number": 1, "pr_body": "body 1",
    }]
    rec = next(r for r in caplog.records if r.getMessage() == "chief_self_heal_published")
    assert rec.repo == "o/r" and rec.pr == 1
    assert world.metrics == [("grug.chief.self_heal", {"outcome": "published"})]


def test_pr_with_chief_is_skipped(world):
    world.prs = [_pr(1)]
    world.runs_by_sha["sha001xxxx"] = [{"name": "Grug - Chief", "status": "in_progress"}]
    assert heal.self_heal_installs([99]) == (0, 0)
    assert world.rechecks == []


def test_legacy_chief_name_counts_as_present(world):
    world.prs = [_pr(1)]
    world.runs_by_sha["sha001xxxx"] = [{"name": "Grug - Definition of Ready"}]
    assert heal.self_heal_installs([99]) == (0, 0)
    assert world.rechecks == []


def test_other_checks_do_not_count_as_chief(world):
    world.prs = [_pr(1)]
    world.runs_by_sha["sha001xxxx"] = [{"name": "Grug - Elder"}, {"name": "ci"}]
    assert heal.self_heal_installs([99]) == (1, 0)


def test_recently_updated_pr_is_skipped_without_any_check_list(world):
    world.prs = [_pr(1, minutes_ago=0.5)]
    assert heal.self_heal_installs([99]) == (0, 0)
    assert world.check_lists == [] and world.rechecks == []


def test_pr_outside_lookback_stops_the_scan(world):
    world.prs = [_pr(1), _pr(2, minutes_ago=60 * 24 * 10), _pr(3, minutes_ago=60 * 24 * 11)]
    assert heal.self_heal_installs([99]) == (1, 0)
    assert world.check_lists == ["sha001xxxx"]


def test_repo_with_chief_disabled_is_not_scanned(world):
    world.prs = [_pr(1)]
    world.enabled = False
    assert heal.self_heal_installs([99]) == (0, 0)
    assert world.check_lists == []


def test_publish_cap_is_respected(world, monkeypatch):
    monkeypatch.setattr(heal, "_MAX_ATTEMPTS", 3)
    world.prs = [_pr(n) for n in range(1, 11)]
    assert heal.self_heal_installs([99]) == (3, 0)
    assert len(world.rechecks) == 3


def test_failed_attempts_count_toward_the_cap(world, monkeypatch):
    monkeypatch.setattr(heal, "_MAX_ATTEMPTS", 2)
    world.prs = [_pr(n) for n in range(1, 6)]
    world.recheck_result = "publish_failed"
    assert heal.self_heal_installs([99]) == (0, 2)
    assert len(world.rechecks) == 2


def test_check_list_cap_is_respected(world, monkeypatch):
    monkeypatch.setattr(heal, "_MAX_CHECK_LISTS", 2)
    world.prs = [_pr(n) for n in range(1, 6)]
    for pr in world.prs:
        world.runs_by_sha[pr["head"]["sha"]] = [{"name": "Grug - Chief"}]
    heal.self_heal_installs([99])
    assert len(world.check_lists) == 2


def test_deadline_stops_the_pass(world, monkeypatch):
    monkeypatch.setattr(heal, "_DEADLINE_S", -1.0)
    world.prs = [_pr(1)]
    assert heal.self_heal_installs([99]) == (0, 0)
    assert world.check_lists == []


def test_recheck_exception_is_contained_and_logged_with_kind(world, caplog):
    world.prs = [_pr(1), _pr(2)]
    world.recheck_raises = httpx.ConnectError("boom")
    with caplog.at_level(logging.WARNING):
        published, failed = heal.self_heal_installs([99])
    assert (published, failed) == (0, 2)
    rec = next(r for r in caplog.records if r.getMessage() == "chief_self_heal_failed")
    assert rec.kind == "ConnectError" and rec.repo == "o/r" and rec.pr in (1, 2)
    assert world.metrics[0] == ("grug.chief.self_heal", {"outcome": "failed"})


def test_publish_failed_sentinel_counts_as_failed(world):
    world.prs = [_pr(1)]
    world.recheck_result = "publish_failed"
    assert heal.self_heal_installs([99]) == (0, 1)


def test_install_level_failure_never_raises(world, monkeypatch):
    def _boom(iid, fn):
        raise RuntimeError("token exchange down")

    monkeypatch.setattr(heal, "with_install_token_retry", _boom)
    assert heal.self_heal_installs([1, 2]) == (0, 2)


def test_pr_listing_failure_is_contained_per_repo(world, monkeypatch):
    def _boom(t, o, r):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(heal, "_list_recent_open_prs", _boom)
    assert heal.self_heal_installs([99]) == (0, 1)


def test_poller_handler_reports_self_heal_counts(monkeypatch):
    monkeypatch.setattr(poller_handler, "_prove_roles_anywhere_identity", lambda: None)
    monkeypatch.setattr(poller_handler, "list_allowlisted_installs", lambda: [])
    monkeypatch.setattr(poller_handler, "_replay_missed_deliveries", lambda: {})
    monkeypatch.setenv("GRUG_ELDER_CANARY", "off")
    monkeypatch.setattr(heal, "self_heal_installs", lambda installs: (4, 1))
    out = poller_handler.handler({}, None)
    assert out["chief_self_heal_published"] == 4
    assert out["chief_self_heal_failed"] == 1


def test_budget_exhausted_by_the_check_list_skips_the_recheck(world, monkeypatch):
    """The check-run list can consume the last of the deadline; Chief must
    not start a re-publish after it."""
    world.prs = [_pr(1)]
    runs = []
    real_run = heal._Run
    monkeypatch.setattr(heal, "_Run", lambda deadline: runs.append(real_run(deadline)) or runs[-1])

    def _list_then_expire(token, owner, repo, sha):
        world.check_lists.append(sha)
        runs[0].deadline = 0.0  # budget gone while the listing was in flight
        return []

    monkeypatch.setattr(heal, "list_check_runs_for_ref", _list_then_expire)
    assert heal.self_heal_installs([99]) == (0, 0)
    assert world.check_lists == ["sha001xxxx"] and world.rechecks == []
