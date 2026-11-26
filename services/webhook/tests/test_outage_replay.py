"""grug#1869: pull requests whose events were missed in a GitHub outage get
their Elder and Chief checks from the poller, once per head, without anyone
asking.

Simulates the outage at the poller seam, then drives late deliveries through
the real dispatcher and Chief against a fake GitHub Checks API.
"""

from __future__ import annotations

from unittest.mock import call, patch

import check_run_reconciler as reconciler
import dispatcher
import github_app_auth
import httpx
import pytest
from personas import publish_check, registry
from personas.registry import PersonaSpec
from personas.tpm import persona


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
_GITHUB_DELIVERY = "00000000-0000-4000-8000-000000000001"


def _tick(runs_by_sha: dict[str, list[dict]]):
    """One poller pass with the inline dispatcher and durable queue stubbed."""
    prs = [_OPENED_DURING_OUTAGE, _PUSHED_DURING_OUTAGE]
    with (
        patch("check_run_reconciler._list_open_pull_requests", return_value=prs),
        patch("check_run_reconciler.expected_personas", return_value=[_CHIEF, _ELDER]),
        patch(
            "check_run_reconciler.list_check_runs_for_ref",
            side_effect=lambda _t, _o, _r, sha: runs_by_sha.get(sha, []),
        ),
        patch("dispatcher.dispatch") as mock_dispatch,
        patch("rerun.enqueue_rerun") as enqueue_elder,
    ):
        result = reconciler.reconcile_repo("tok", 1, "o", "r", 2)
    return result, mock_dispatch, enqueue_elder


def test_missed_opened_and_pushed_events_are_replayed_once_per_head():
    # Outage: no check-runs exist for either head.
    runs: dict[str, list[dict]] = {}
    (dispatched, swept), mock_dispatch, enqueue_elder = _tick(runs)

    assert (dispatched, swept) == (2, 0)
    assert mock_dispatch.call_count == 2
    by_sha = {
        c.args[1]["pull_request"]["head"]["sha"]: c
        for c in mock_dispatch.call_args_list
    }
    assert set(by_sha) == {"sha-opened", "sha-pushed-new"}
    for sha, dispatch_call in by_sha.items():
        assert dispatch_call.kwargs["skip_personas"] == frozenset({"code_reviewer"})
        assert dispatch_call.kwargs["delivery_id"] == f"reconcile:o/r:{sha}"
    assert enqueue_elder.call_args_list == [
        call(install_id=1, repo="o/r", pr_number=1, persona="elder", dedup_key="reconcile:sha-opened"),
        call(install_id=1, repo="o/r", pr_number=2, persona="elder", dedup_key="reconcile:sha-pushed-new"),
    ]

    # Recovery: the replay posted its runs. The next ticks cause no second
    # review of the same head.
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
        (dispatched, swept), mock_dispatch, enqueue_elder = _tick(runs)
        assert (dispatched, swept) == (0, 0)
        mock_dispatch.assert_not_called()
        enqueue_elder.assert_not_called()


def test_replay_runs_only_the_persona_the_outage_cost():
    runs = {"sha-opened": [{"name": "Grug - Chief", "status": "completed"}]}
    result, mock_dispatch, enqueue_elder = _tick(runs)
    assert result == (2, 0)
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.args[1]["pull_request"]["head"]["sha"] == "sha-pushed-new"
    assert mock_dispatch.call_args.kwargs["skip_personas"] == frozenset({"code_reviewer"})
    assert enqueue_elder.call_args_list == [
        call(install_id=1, repo="o/r", pr_number=1, persona="elder", dedup_key="reconcile:sha-opened"),
        call(install_id=1, repo="o/r", pr_number=2, persona="elder", dedup_key="reconcile:sha-pushed-new"),
    ]


@pytest.fixture
def chief_replay(monkeypatch):
    runs = []
    payload = reconciler._synthetic_pull_request_payload(1, "o", "r", 2, _OPENED_DURING_OUTAGE)

    def post_check(_token, _owner, _repo, result, *, external_id):
        runs.append(
            {
                "app": {"id": 77},
                "external_id": external_id,
                "name": result.name,
                "head_sha": result.head_sha,
                "status": result.status,
                "conclusion": result.conclusion,
            }
        )

    def list_runs(url, **_kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"total_count": len(runs), "check_runs": runs},
        )

    def with_token(_id, fn):
        return fn("fake-token")

    monkeypatch.setattr(registry, "REGISTRY", (_CHIEF,))
    monkeypatch.setattr(dispatcher, "is_install_allowlisted", lambda _id: True)
    monkeypatch.setattr(dispatcher, "is_persona_enabled", lambda *_args: True)
    monkeypatch.setattr(dispatcher, "_ensure_enforcement_on_pr", lambda *_args: None)
    monkeypatch.setattr(reconciler, "expected_personas", lambda *_args: [_CHIEF])
    monkeypatch.setattr(
        reconciler, "_list_open_pull_requests", lambda *_args: [_OPENED_DURING_OUTAGE]
    )
    monkeypatch.setattr(
        "adapters.install_store.get_repo_config",
        lambda *_args: {"check_run_reconcile_enabled": True},
    )
    monkeypatch.setattr(github_app_auth, "get_install_token", lambda _id: "fake-token")
    monkeypatch.setattr(github_app_auth, "get_app_id", lambda: "77")
    monkeypatch.setattr(github_app_auth, "with_install_token_retry", with_token)
    monkeypatch.setattr(publish_check, "with_install_token_retry", with_token)
    monkeypatch.setattr(publish_check, "record_check_verdict", lambda **_kwargs: None)
    monkeypatch.setattr(publish_check, "post_check_run", post_check)
    monkeypatch.setattr("github_checks_client.httpx.get", list_runs)
    monkeypatch.setattr(
        persona,
        "evaluate_pull_request",
        lambda *_args, **_kwargs: persona.TpmEvaluation(True, (), "success"),
    )
    monkeypatch.setattr(
        "personas.tpm.ticket_compliance_run.run_ticket_compliance",
        lambda *_args, **_kwargs: {},
    )
    return payload, runs


@pytest.mark.parametrize("action", ["opened", "synchronize"])
def test_late_github_delivery_reuses_the_chief_check_left_by_poller(chief_replay, action):
    payload, runs = chief_replay
    assert reconciler.reconcile_repo("fake-token", 1, "o", "r", 2) == (1, 0)
    assert len(runs) == 1
    assert reconciler.reconcile_repo("fake-token", 1, "o", "r", 2) == (0, 0)

    outcome = dispatcher.dispatch(
        "pull_request",
        {**payload, "action": action},
        delivery_id=_GITHUB_DELIVERY,
    )

    assert len(runs) == 1
    assert outcome["personas"] == [{"persona": "tpm", "result": "already_checked"}]


@pytest.mark.parametrize(
    "unrelated_verdict",
    [
        {"app": {"id": 78}},
        {"head_sha": "sha-older"},
        {"external_id": "grug-tpm:o/r#2:sha-opened"},
        {"status": "in_progress", "conclusion": None},
        {"conclusion": "cancelled"},
    ],
)
def test_unrelated_or_unfinished_check_does_not_suppress_chief(chief_replay, unrelated_verdict):
    payload, runs = chief_replay
    assert reconciler.reconcile_repo("fake-token", 1, "o", "r", 2) == (1, 0)
    runs[0].update(unrelated_verdict)

    dispatcher.dispatch("pull_request", payload, delivery_id=_GITHUB_DELIVERY)

    assert len(runs) == 2


@pytest.mark.parametrize(
    ("action", "delivery_id"),
    [("edited", _GITHUB_DELIVERY), ("synchronize", "")],
)
def test_chief_can_check_same_head_again_on_edit_or_rerequest(chief_replay, action, delivery_id):
    payload, runs = chief_replay
    assert reconciler.reconcile_repo("fake-token", 1, "o", "r", 2) == (1, 0)

    dispatcher.dispatch("pull_request", {**payload, "action": action}, delivery_id=delivery_id)

    assert len(runs) == 2


def test_unreadable_checks_allow_chief_to_publish(chief_replay, monkeypatch, caplog):
    payload, runs = chief_replay

    def timeout(*_args, **_kwargs):
        raise httpx.ReadTimeout("test timeout")

    monkeypatch.setattr("github_checks_client.httpx.get", timeout)
    dispatcher.dispatch("pull_request", payload, delivery_id=_GITHUB_DELIVERY)

    assert len(runs) == 1
    assert "tpm_existing_verdict_read_failed" in caplog.messages


def test_late_github_delivery_recovers_a_failed_poller_publish(chief_replay):
    payload, runs = chief_replay
    with patch.object(publish_check, "post_check_run", side_effect=RuntimeError("test publish failure")):
        reconciler.reconcile_repo("fake-token", 1, "o", "r", 2)
    assert runs == []

    dispatcher.dispatch("pull_request", payload, delivery_id=_GITHUB_DELIVERY)

    assert len(runs) == 1


def test_reconciliation_opt_out_skips_the_additional_check_lookup(chief_replay, monkeypatch):
    payload, runs = chief_replay
    monkeypatch.setattr("adapters.install_store.get_repo_config", lambda *_args: {})
    with patch("github_checks_client.httpx.get") as lookup:
        dispatcher.dispatch("pull_request", payload, delivery_id=_GITHUB_DELIVERY)

    lookup.assert_not_called()
    assert len(runs) == 1
