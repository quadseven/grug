"""Tests for check_run_reconciler (grug#947).

The idempotency mechanism under test: before dispatching, list the head
SHA's real check-runs and skip every persona already present. Mocks
httpx and the persona registry - no real GitHub calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import check_run_reconciler as reconciler
from personas.registry import PersonaSpec


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
    )


_CHIEF = _spec("tpm", "Grug - Chief")
_ELDER = _spec("code_reviewer", "Grug - Elder")


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def test_expected_personas_filters_by_enablement_and_event():
    only_pr_event = [_CHIEF, _ELDER]
    with patch.object(reconciler.persona_registry, "REGISTRY", tuple(only_pr_event)), \
         patch(
             "check_run_reconciler.is_persona_enabled",
             side_effect=lambda _iid, _rid, key: key == "tpm",
         ):
        out = reconciler.expected_personas(1, 2)
    assert [spec.key for spec in out] == ["tpm"]


def test_split_missing_present_stuck_separates_missing_from_present():
    """grug#947 acceptance #3/#4 core: a persona whose check-run is
    already on the head SHA lands in `present`, not `missing` - this is
    the mechanism that stops a race with a webhook from double-posting."""
    runs = [{"name": "Grug - Chief", "status": "completed"}, {"name": "Some Other App"}]
    with patch.object(reconciler, "_ALL_CHECK_RUN_NAMES", frozenset({"Grug - Chief", "Grug - Elder"})):
        missing, present, stuck = reconciler._split_missing_present_stuck(runs, [_CHIEF, _ELDER])
    assert [spec.key for spec in missing] == ["code_reviewer"]
    assert present == frozenset({"tpm"})
    assert stuck == []


def test_split_missing_present_stuck_finds_an_old_in_progress_run():
    """grug#887: Elder sat in_progress for 90+ minutes after a mid-review
    503. Past the age bound, it must be flagged for sweeping."""
    runs = [{
        "id": 1, "name": "Grug - Elder", "status": "in_progress",
        "started_at": _iso(minutes_ago=200),
    }]
    with patch.object(reconciler, "_ALL_CHECK_RUN_NAMES", frozenset({"Grug - Elder"})):
        _, _, stuck = reconciler._split_missing_present_stuck(runs, [])
    assert len(stuck) == 1
    assert stuck[0]["id"] == 1


def test_split_missing_present_stuck_leaves_a_fresh_in_progress_run_alone():
    """A review still genuinely working must never be swept - closing it
    would be the false-positive version of the same defect."""
    runs = [{
        "id": 1, "name": "Grug - Elder", "status": "in_progress",
        "started_at": _iso(minutes_ago=5),
    }]
    with patch.object(reconciler, "_ALL_CHECK_RUN_NAMES", frozenset({"Grug - Elder"})):
        _, _, stuck = reconciler._split_missing_present_stuck(runs, [])
    assert stuck == []


def test_split_missing_present_stuck_never_touches_a_third_party_check_run():
    """A same-named-by-coincidence or long-running third-party check-run
    must never be swept - only grug's own registry names are candidates."""
    runs = [{
        "id": 1, "name": "Some Other CI", "status": "in_progress",
        "started_at": _iso(minutes_ago=200),
    }]
    with patch.object(reconciler, "_ALL_CHECK_RUN_NAMES", frozenset({"Grug - Elder"})):
        _, _, stuck = reconciler._split_missing_present_stuck(runs, [])
    assert stuck == []


def test_split_missing_present_stuck_leaves_a_completed_run_alone():
    """Only `status == in_progress` is a candidate - a completed run,
    however old, is not stuck."""
    runs = [{
        "id": 1, "name": "Grug - Elder", "status": "completed",
        "conclusion": "success", "started_at": _iso(minutes_ago=200),
    }]
    with patch.object(reconciler, "_ALL_CHECK_RUN_NAMES", frozenset({"Grug - Elder"})):
        _, _, stuck = reconciler._split_missing_present_stuck(runs, [])
    assert stuck == []


def test_close_stuck_run_patches_by_id_with_an_honest_failure():
    run = {"id": 42, "name": "Grug - Elder", "head_sha": "abc123", "started_at": _iso(90)}
    with patch("check_run_reconciler.patch_check_run") as mock_patch:
        reconciler._close_stuck_run("tok", "o", "r", run)
    mock_patch.assert_called_once()
    args, _ = mock_patch.call_args
    assert args[:4] == ("tok", "o", "r", 42)
    result = args[4]
    assert result.conclusion == "failure"
    assert result.status == "completed"
    assert "recheck" not in result.summary, (
        "/grug recheck is currently dead (grug#945) - never point an "
        "author at a lever that does not work"
    )


def test_reconcile_repo_skips_pr_with_nothing_missing_or_stuck():
    """No dispatch and no sweep when every expected persona already has a
    completed check-run - the common case on every cron tick when nothing
    is broken."""
    pr = {"number": 5, "head": {"sha": "abc"}, "body": ""}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[_CHIEF]), \
         patch("check_run_reconciler.list_check_runs_for_ref",
               return_value=[{"name": "Grug - Chief", "status": "completed"}]), \
         patch("check_run_reconciler.patch_check_run") as mock_patch, \
         patch("dispatcher.dispatch") as mock_dispatch:
        dispatched, swept = reconciler.reconcile_repo("tok", 1, "o", "r", 2)
    assert (dispatched, swept) == (0, 0)
    mock_dispatch.assert_not_called()
    mock_patch.assert_not_called()


def test_reconcile_repo_dispatches_only_for_the_missing_persona():
    """grug#947 acceptance #2: an absent expected check-run gets created -
    via the normal dispatch path, with the ALREADY-PRESENT persona passed
    as skip_personas so it is not re-run."""
    pr = {"number": 5, "head": {"sha": "abc"}, "body": ""}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[_CHIEF, _ELDER]), \
         patch("check_run_reconciler.list_check_runs_for_ref",
               return_value=[{"name": "Grug - Chief", "status": "completed"}]), \
         patch("dispatcher.dispatch") as mock_dispatch:
        dispatched, swept = reconciler.reconcile_repo("tok", 1, "o", "r", 2)

    assert (dispatched, swept) == (1, 0)
    mock_dispatch.assert_called_once()
    args, kwargs = mock_dispatch.call_args
    assert args[0] == "pull_request"
    assert args[1]["pull_request"]["head"]["sha"] == "abc"
    assert args[1]["action"] == "synchronize"
    assert kwargs["skip_personas"] == frozenset({"tpm"}), (
        "the persona that already has a check-run must be skipped, "
        "not re-dispatched"
    )


def test_reconcile_repo_sweeps_a_stuck_run_and_requeues_it_with_the_missing():
    """A persona can be simultaneously STUCK (sweep it) while another is
    fully absent (dispatch for that one) - the two halves of one pass.

    grug#1275 supersedes the #947 no-retry rule for Elder/Chief: the
    swept Elder is re-queued on the durable rerun lane while the missing
    Chief still gets its inline dispatch. The loop risk #947 cited is
    bounded structurally: the sweep completes the orphaned run (it can
    never be flagged stuck again) and the re-queued review starts a new
    run with a fresh deadline."""
    pr = {"number": 5, "head": {"sha": "abc"}, "body": ""}
    stuck_run = {"id": 9, "name": "Grug - Elder", "status": "in_progress", "started_at": _iso(200)}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[_CHIEF, _ELDER]), \
         patch("check_run_reconciler.list_check_runs_for_ref", return_value=[stuck_run]), \
         patch("check_run_reconciler.patch_check_run") as mock_patch, \
         patch("rerun.enqueue_rerun") as mock_enqueue, \
         patch("dispatcher.dispatch") as mock_dispatch:
        dispatched, swept = reconciler.reconcile_repo("tok", 1, "o", "r", 2)

    assert (dispatched, swept) == (1, 1)
    mock_patch.assert_called_once()
    mock_enqueue.assert_called_once_with(
        install_id=1, repo="o/r", pr_number=5, persona="elder",
        dedup_key="restart-recovery:9",
    )
    mock_dispatch.assert_called_once()
    _, kwargs = mock_dispatch.call_args
    assert kwargs["skip_personas"] == frozenset({"code_reviewer"}), (
        "Elder rides the rerun lane - the inline dispatch is for the "
        "missing Chief only"
    )
    assert kwargs["delivery_id"] == "reconcile:o/r:abc"


def test_reconcile_repo_sweeps_a_stuck_run_and_still_skips_it_as_present():
    """A persona can be simultaneously STUCK (sweep it) while another is
    fully absent (dispatch for that one) - the two halves of one pass.

    Deliberate: a swept persona outside Elder/Chief is NOT re-dispatched
    in the same cycle (grug#1275 re-queues only those two). `present`
    means "a check-run with this name exists", regardless of its status -
    a closed-by-sweep failure is a real, visible verdict the author can
    act on (push a new commit), not a silence the reconciler needs to
    paper over."""
    guard = _spec("guard", "Grug - Guard")
    pr = {"number": 5, "head": {"sha": "abc"}, "body": ""}
    stuck_run = {"id": 9, "name": "Grug - Guard", "status": "in_progress", "started_at": _iso(200)}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[_CHIEF, guard]), \
         patch("check_run_reconciler.list_check_runs_for_ref", return_value=[stuck_run]), \
         patch("check_run_reconciler.patch_check_run") as mock_patch, \
         patch("dispatcher.dispatch") as mock_dispatch:
        dispatched, swept = reconciler.reconcile_repo("tok", 1, "o", "r", 2)

    assert swept == 1
    mock_patch.assert_called_once()
    assert dispatched == 1, "tpm is fully absent and must still be dispatched"
    _, kwargs = mock_dispatch.call_args
    assert kwargs["skip_personas"] == frozenset({"guard"}), (
        "guard already has a check-run (now being swept) - it "
        "counts as present and must not also be dispatched"
    )


def test_reconcile_repo_one_pr_failure_does_not_abort_the_rest():
    good_pr = {"number": 1, "head": {"sha": "aaa"}, "body": ""}
    bad_pr = {"number": 2, "head": {"sha": "bbb"}, "body": ""}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[bad_pr, good_pr]), \
         patch(
             "check_run_reconciler.list_check_runs_for_ref",
             side_effect=[RuntimeError("boom"), []],
         ), \
         patch("check_run_reconciler.expected_personas", return_value=[_CHIEF]), \
         patch("dispatcher.dispatch") as mock_dispatch:
        dispatched, swept = reconciler.reconcile_repo("tok", 1, "o", "r", 2)

    assert dispatched == 1, "the good PR must still be reconciled despite the bad one erroring"
    mock_dispatch.assert_called_once()


def test_reconcile_installs_skips_installs_with_no_opted_in_repos():
    with patch("check_run_reconciler.list_check_run_reconcile_repos", return_value=[]), \
         patch("check_run_reconciler.with_install_token_retry") as mock_retry:
        total, failed = reconciler.reconcile_installs([1, 2])
    assert (total, failed) == (0, 0)
    mock_retry.assert_not_called()


def test_reconcile_installs_sums_dispatched_and_swept_across_repos():
    with patch("check_run_reconciler.list_check_run_reconcile_repos",
               return_value=[{"id": 9, "full_name": "o/r"}]), \
         patch("check_run_reconciler.with_install_token_retry", return_value=[(2, 1)]):
        total, failed = reconciler.reconcile_installs([1])
    assert (total, failed) == (3, 0)


def test_reconcile_installs_one_install_failure_does_not_abort_the_cron():
    with patch(
        "check_run_reconciler.list_check_run_reconcile_repos",
        side_effect=[RuntimeError("boom"), [{"id": 9, "full_name": "o/r"}]],
    ), patch("check_run_reconciler.with_install_token_retry", return_value=[(1, 0)]):
        total, failed = reconciler.reconcile_installs([1, 2])
    assert total == 1
    assert failed == 1


def test_reconcile_installs_emits_dense_stuck_count_metric_when_nothing_stuck():
    """grug#948: the stuck-check-run alert signal must be DENSE - a clean
    pass (nothing swept) still emits 0.0, not silence, so a healthy
    stretch is distinguishable from no data at all."""
    import observability  # type: ignore

    emitted: list[tuple[str, float]] = []
    with patch("check_run_reconciler.list_check_run_reconcile_repos",
               return_value=[{"id": 9, "full_name": "o/r"}]), \
         patch("check_run_reconciler.with_install_token_retry", return_value=[(1, 0)]), \
         patch.object(observability, "emit_gauge",
                      lambda metric, value, **_kw: emitted.append((metric, value))):
        reconciler.reconcile_installs([1])
    assert emitted == [("grug.check_run.stuck_count", 0.0)]


def test_reconcile_installs_emits_stuck_count_metric_summed_across_repos():
    """The metric feeds the same detection `reconcile_repo` already does
    (never a second implementation of it) - it just sums what came back."""
    import observability  # type: ignore

    emitted: list[tuple[str, float]] = []
    with patch("check_run_reconciler.list_check_run_reconcile_repos",
               return_value=[{"id": 9, "full_name": "o/r"}, {"id": 10, "full_name": "o/r2"}]), \
         patch("check_run_reconciler.with_install_token_retry", return_value=[(0, 2), (1, 1)]), \
         patch.object(observability, "emit_gauge",
                      lambda metric, value, **_kw: emitted.append((metric, value))):
        reconciler.reconcile_installs([1])
    assert emitted == [("grug.check_run.stuck_count", 3.0)]


def test_reconcile_repo_requeues_a_stuck_elder_once_after_sweeping_it():
    """grug#1275: a stuck Elder run past the review deadline is swept AND
    re-queued exactly once in the same pass, on the durable rerun lane (the
    `/grug improve` path, run by the consumers - not a thread inside the
    poller CronJob). The SQS dedup key is the orphaned run's id. No PR
    comment is posted, and the swept run says a fresh review is coming."""
    pr = {"number": 5, "head": {"sha": "abc"}, "body": ""}
    stuck_run = {"id": 9, "name": "Grug - Elder", "status": "in_progress", "started_at": _iso(200)}
    chief_run = {"name": "Grug - Chief", "status": "completed"}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[_CHIEF, _ELDER]), \
         patch("check_run_reconciler.list_check_runs_for_ref", return_value=[stuck_run, chief_run]), \
         patch("check_run_reconciler.patch_check_run") as mock_patch, \
         patch("rerun.enqueue_rerun") as mock_enqueue, \
         patch("dispatcher.dispatch") as mock_dispatch:
        dispatched, swept = reconciler.reconcile_repo("tok", 1, "o", "r", 2)
    assert (dispatched, swept) == (1, 1)
    mock_patch.assert_called_once()
    result = mock_patch.call_args.args[4]
    assert result.conclusion == "failure"
    assert "re-queued a fresh review" in result.summary
    mock_enqueue.assert_called_once()
    assert mock_enqueue.call_args.kwargs["persona"] == "elder"
    assert mock_enqueue.call_args.kwargs["dedup_key"] == "restart-recovery:9"
    mock_dispatch.assert_not_called()


def test_reconcile_repo_requeues_a_stuck_chief_inline_keyed_by_its_run():
    """grug#1275: Chief has no rerun lane, so a stuck Chief is re-queued
    through the inline dispatch - not skipped as present - under a delivery
    id keyed by the orphaned run, never the per-head reconcile key."""
    pr = {"number": 5, "head": {"sha": "abc"}, "body": ""}
    stuck_run = {"id": 21, "name": "Grug - Chief", "status": "in_progress", "started_at": _iso(200)}
    elder_run = {"name": "Grug - Elder", "status": "completed"}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[_CHIEF, _ELDER]), \
         patch("check_run_reconciler.list_check_runs_for_ref", return_value=[stuck_run, elder_run]), \
         patch("check_run_reconciler.patch_check_run"), \
         patch("rerun.enqueue_rerun") as mock_enqueue, \
         patch("dispatcher.dispatch") as mock_dispatch:
        assert reconciler.reconcile_repo("tok", 1, "o", "r", 2) == (1, 1)
    mock_enqueue.assert_not_called()
    mock_dispatch.assert_called_once()
    _, kwargs = mock_dispatch.call_args
    assert kwargs["skip_personas"] == frozenset({"code_reviewer"})
    assert kwargs["delivery_id"] == "restart-recovery:o/r:21"


def test_reconcile_repo_a_failed_elder_requeue_still_sweeps_honestly():
    """A rerun-queue send failure must not abort the pass or leave the run
    hanging: the run is still swept, but as a plain failure that asks for a
    push - it never promises a re-review that was not queued."""
    pr = {"number": 5, "head": {"sha": "abc"}, "body": ""}
    stuck_run = {"id": 9, "name": "Grug - Elder", "status": "in_progress", "started_at": _iso(200)}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[_ELDER]), \
         patch("check_run_reconciler.list_check_runs_for_ref", return_value=[stuck_run]), \
         patch("check_run_reconciler.patch_check_run") as mock_patch, \
         patch("rerun.enqueue_rerun", side_effect=RuntimeError("no queue")), \
         patch("dispatcher.dispatch") as mock_dispatch:
        assert reconciler.reconcile_repo("tok", 1, "o", "r", 2) == (0, 1)
    mock_patch.assert_called_once()
    summary = mock_patch.call_args.args[4].summary
    assert "Push a new commit" in summary
    assert "re-queued" not in summary
    mock_dispatch.assert_not_called()


def test_reconcile_repo_does_not_requeue_a_stuck_non_elder_chief_run():
    """grug#1275 scope: only Elder/Chief stuck runs are re-queued. Any
    other persona's stuck run is still swept to a failure, not retried."""
    guard = _spec("guard", "Grug - Guard")
    pr = {"number": 5, "head": {"sha": "abc"}, "body": ""}
    stuck_run = {"id": 11, "name": "Grug - Guard", "status": "in_progress", "started_at": _iso(200)}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[guard]), \
         patch("check_run_reconciler.list_check_runs_for_ref", return_value=[stuck_run]), \
         patch("check_run_reconciler.patch_check_run") as mock_patch, \
         patch("rerun.enqueue_rerun") as mock_enqueue, \
         patch("dispatcher.dispatch") as mock_dispatch:
        dispatched, swept = reconciler.reconcile_repo("tok", 1, "o", "r", 2)
    assert (dispatched, swept) == (0, 1)
    assert "Push a new commit" in mock_patch.call_args.args[4].summary
    mock_dispatch.assert_not_called()
    mock_enqueue.assert_not_called()


def test_restart_recovery_flow_requeues_once_and_then_leaves_the_new_run_alone():
    """grug#1275 acceptance drill, at the poller seam: a review was started
    (in_progress Elder run), the consumers rolled mid-review and the run
    was orphaned past its deadline. Pass 1 sweeps it and re-queues once;
    pass 2 sees the swept run completed and the re-queued review's FRESH
    in_progress run - and does nothing. The PR gets its Elder conclusion
    from the re-queued run with no manual command and no second requeue."""
    pr = {"number": 5, "head": {"sha": "abc"}, "body": ""}
    orphaned = {"id": 9, "name": "Grug - Elder", "status": "in_progress", "started_at": _iso(200)}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[_ELDER]), \
         patch("check_run_reconciler.list_check_runs_for_ref", return_value=[orphaned]), \
         patch("check_run_reconciler.patch_check_run"), \
         patch("rerun.enqueue_rerun") as mock_enqueue, \
         patch("dispatcher.dispatch") as mock_dispatch:
        assert reconciler.reconcile_repo("tok", 1, "o", "r", 2) == (1, 1)
    assert mock_enqueue.call_count == 1
    mock_dispatch.assert_not_called()

    swept = {"id": 9, "name": "Grug - Elder", "status": "completed", "conclusion": "failure", "started_at": _iso(200)}
    fresh = {"id": 10, "name": "Grug - Elder", "status": "in_progress", "started_at": _iso(1)}
    with patch("check_run_reconciler._list_open_pull_requests", return_value=[pr]), \
         patch("check_run_reconciler.expected_personas", return_value=[_ELDER]), \
         patch("check_run_reconciler.list_check_runs_for_ref", return_value=[swept, fresh]), \
         patch("check_run_reconciler.patch_check_run") as mock_patch2, \
         patch("rerun.enqueue_rerun") as mock_enqueue2, \
         patch("dispatcher.dispatch") as mock_dispatch2:
        assert reconciler.reconcile_repo("tok", 1, "o", "r", 2) == (0, 0)
    mock_enqueue2.assert_not_called()
    mock_dispatch2.assert_not_called()
    mock_patch2.assert_not_called()
