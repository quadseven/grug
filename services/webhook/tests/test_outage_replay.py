"""grug#1869: pull requests whose events were missed in a GitHub outage get
their Elder and Chief checks from the poller, once per head, without anyone
asking.

Simulates the outage at the poller seam: the webhook deliveries for a newly
opened PR and for a push to another PR never produced check-runs. GitHub
and the dispatcher are fakes; `check_run_reconciler` runs for real.
"""

from __future__ import annotations

from unittest.mock import patch

from personas.registry import PersonaSpec

import check_run_reconciler as reconciler


def _spec(key: str, check_run_name: str) -> PersonaSpec:
    return PersonaSpec(
        key=key,
        canonical=key,
        check_run_name=check_run_name,
        enabled_flag=f"{key}_enabled",
        enabled_default=True,
        blocking_flag=None,
        blocking_default=False,
        dispatch_style="inline",
        missing_repo_policy="enabled",
        events=("pull_request",),
        dispatch_module=f"personas.{key}.webhook_dispatch",
        posts_pr_check_run=True,
    )


_CHIEF = _spec("tpm", "Grug - Chief")
_ELDER = _spec("code_reviewer", "Grug - Elder")

_OPENED_DURING_OUTAGE = {"number": 1, "head": {"sha": "sha-opened"}, "body": ""}
_PUSHED_DURING_OUTAGE = {"number": 2, "head": {"sha": "sha-pushed-new"}, "body": ""}


def _tick(runs_by_sha: dict[str, list[dict]]):
    """One poller pass; returns (result, dispatch mock)."""
    prs = [_OPENED_DURING_OUTAGE, _PUSHED_DURING_OUTAGE]
    with (
        patch("check_run_reconciler._list_open_pull_requests", return_value=prs),
        patch("check_run_reconciler.expected_personas", return_value=[_CHIEF, _ELDER]),
        patch(
            "check_run_reconciler.list_check_runs_for_ref",
            side_effect=lambda _t, _o, _r, sha: runs_by_sha.get(sha, []),
        ),
        patch("dispatcher.dispatch") as mock_dispatch,
    ):
        result = reconciler.reconcile_repo("tok", 1, "o", "r", 2)
    return result, mock_dispatch


def test_missed_opened_and_pushed_events_are_replayed_once_per_head():
    # Outage: no check-runs exist for either head.
    runs: dict[str, list[dict]] = {}
    (dispatched, swept), mock_dispatch = _tick(runs)

    assert (dispatched, swept) == (2, 0)
    assert mock_dispatch.call_count == 2
    by_sha = {
        c.args[1]["pull_request"]["head"]["sha"]: c
        for c in mock_dispatch.call_args_list
    }
    assert set(by_sha) == {"sha-opened", "sha-pushed-new"}
    for sha, call in by_sha.items():
        assert call.kwargs["skip_personas"] == frozenset(), (
            "both Elder and Chief must run"
        )
        assert call.kwargs["delivery_id"] == f"reconcile:o/r:{sha}"

    # Recovery: the replay posted its runs. The next ticks, and a late
    # redelivery of the original events (which finds the runs in place),
    # cause no second review of the same head.
    runs.update(
        {
            sha: [
                {"name": "Grug - Chief", "status": "completed"},
                {"name": "Grug - Elder", "status": "completed"},
            ]
            for sha in by_sha
        }
    )
    for _ in range(2):
        (dispatched, swept), mock_dispatch = _tick(runs)
        assert (dispatched, swept) == (0, 0)
        mock_dispatch.assert_not_called()


def test_replay_runs_only_the_persona_the_outage_cost():
    runs = {"sha-opened": [{"name": "Grug - Chief", "status": "completed"}]}
    _, mock_dispatch = _tick(runs)
    calls = {
        c.args[1]["pull_request"]["head"]["sha"]: c
        for c in mock_dispatch.call_args_list
    }
    assert calls["sha-opened"].kwargs["skip_personas"] == frozenset({"tpm"})
    assert calls["sha-pushed-new"].kwargs["skip_personas"] == frozenset()
