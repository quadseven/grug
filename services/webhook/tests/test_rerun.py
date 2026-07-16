"""Tests for the re-run consumer (#305, ADR-0004).

External behavior: a job (keyed by repo "owner/name") re-fetches the PR's
current head and dispatches the named persona; an unsupported persona is
skipped (not retried); an infra failure RAISES so the ESM retries → DLQ.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import ANY, MagicMock, patch

import httpx
import pytest

import rerun


def _job(**over) -> str:
    d = {"schema_version": 1, "install_id": 11, "repo": "myorg/myrepo", "pr_number": 7, "persona": "elder"}
    d.update(over)
    return json.dumps(d)


def _event(*bodies) -> dict:
    return {"Records": [{"eventSource": "aws:sqs", "body": b} for b in bodies]}


def _pr_data(
    head_sha="abc123",
    repo_id=222,
    *,
    base_sha="base-sha",
    title="Improve review depth",
    body="Closes #6. Preserve old fallback behavior.",
    state="open",
    draft=False,
):
    return {
        "number": 7,
        "title": title,
        "body": body,
        "state": state,
        "draft": draft,
        "user": {"login": "evan"},
        "head": {"sha": head_sha},
        "base": {"sha": base_sha, "repo": {"id": repo_id}},
    }


def _pr_response(head_sha="abc123", repo_id=222):
    pr = MagicMock(spec=httpx.Response)
    pr.raise_for_status = MagicMock()
    pr.json = MagicMock(return_value=_pr_data(head_sha=head_sha, repo_id=repo_id))
    return pr


def _patch_hot_claims(
    monkeypatch,
    *,
    status="acquired",
    release_result=True,
    complete_result=True,
):
    acquire = MagicMock(return_value=status)
    complete = MagicMock(return_value=complete_result)
    release = MagicMock(return_value=release_result)
    monkeypatch.setattr("adapters.install_store.acquire_review_claim", acquire)
    monkeypatch.setattr("adapters.install_store.complete_review_claim", complete)
    monkeypatch.setattr("adapters.install_store.release_review_claim", release)
    return acquire, complete, release


def test_review_claim_heartbeat_renews_until_stopped(monkeypatch):
    renewed = threading.Event()

    def renew(**kwargs):
        renewed.set()
        return True

    monkeypatch.setattr("adapters.install_store.renew_review_claim", renew)
    monkeypatch.setattr(rerun, "_REVIEW_CLAIM_HEARTBEAT_SECONDS", 0.001)
    heartbeat = rerun._start_review_claim_heartbeat({
        "install_id": 11,
        "repo": "myorg/myrepo",
        "pr_number": 7,
        "persona": "code_reviewer",
        "head_sha": "snapshot",
        "owner_token": "worker",
    })

    assert renewed.wait(1.0)
    assert rerun._stop_review_claim_heartbeat(heartbeat) is True


def test_review_claim_heartbeat_surfaces_lost_ownership(monkeypatch):
    attempted = threading.Event()

    def renew(**kwargs):
        attempted.set()
        return False

    monkeypatch.setattr("adapters.install_store.renew_review_claim", renew)
    monkeypatch.setattr(rerun, "_REVIEW_CLAIM_HEARTBEAT_SECONDS", 0.001)
    heartbeat = rerun._start_review_claim_heartbeat({
        "install_id": 11,
        "repo": "myorg/myrepo",
        "pr_number": 7,
        "persona": "code_reviewer",
        "head_sha": "snapshot",
        "owner_token": "worker",
    })

    assert attempted.wait(1.0)
    heartbeat.thread.join(timeout=1.0)
    assert rerun._stop_review_claim_heartbeat(heartbeat) is False


def test_enqueue_review_is_snapshot_scoped_and_carries_quiet_window(monkeypatch):
    sent = {}
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/review.fifo")
    monkeypatch.setattr(
        rerun._sqs, "send_message", lambda **kwargs: sent.update(kwargs),
    )
    # Visibility check is best-effort and out of scope for the FIFO shape
    # assertion; pin it so a real token path is not exercised.
    monkeypatch.setattr(rerun, "_post_elder_in_progress_check", lambda **kwargs: None)

    rerun.enqueue_review(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        requested_base_sha="base-123",
        requested_head_sha="head-123",
        requested_title="Review intent",
        requested_body="Preserve compatibility.",
        settle_seconds=90,
    )

    body = json.loads(sent["MessageBody"])
    assert body["kind"] == "review"
    assert body["requested_head_sha"] == "head-123"
    expected_snapshot_id = rerun.review_snapshot_id(
        base_sha="base-123",
        head_sha="head-123",
        title="Review intent",
        body="Preserve compatibility.",
    )
    assert body["requested_snapshot_id"] == expected_snapshot_id
    assert body["settle_seconds"] == 90
    assert sent["MessageDeduplicationId"] == rerun._review_dedup_id(
        11, "myorg/myrepo", 7, expected_snapshot_id,
    )
    assert sent["MessageGroupId"] == rerun._review_group_id(
        11, "myorg/myrepo", 7,
    )
    assert len(sent["MessageDeduplicationId"]) <= 128
    assert len(sent["MessageGroupId"]) <= 128


def test_enqueue_review_posts_in_progress_check_after_sqs(monkeypatch):
    """Required-check rulesets treat a missing Elder check as BLOCKED. Post
    in_progress immediately after the durable enqueue so agents see pending
    instead of 'never ran' while deep review runs for minutes."""
    order: list[str] = []
    posted: dict = {}

    def send_message(**kwargs):
        order.append("sqs")
        return {"MessageId": "m-1"}

    def with_token(install_id, fn):
        order.append("token")
        assert install_id == 11
        return fn("tok-1")

    def post_check(token, owner, repo_name, result, external_id=None):
        order.append("check")
        posted.update(
            {
                "token": token,
                "owner": owner,
                "repo": repo_name,
                "result": result,
                "external_id": external_id,
            }
        )
        return {"id": 99}

    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/review.fifo")
    monkeypatch.setattr(rerun._sqs, "send_message", send_message)
    monkeypatch.setattr(
        rerun, "_elder_check_already_terminal_or_pending", lambda **k: None,
    )
    monkeypatch.setattr(rerun, "with_install_token_retry", with_token)
    monkeypatch.setattr(rerun, "post_check_run", post_check)

    rerun.enqueue_review(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        requested_base_sha="base-123",
        requested_head_sha="head-123",
        requested_title="t",
        requested_body="b",
        settle_seconds=10,
    )

    assert order == ["sqs", "token", "check"]
    assert posted["token"] == "tok-1"
    assert posted["owner"] == "myorg"
    assert posted["repo"] == "myrepo"
    result = posted["result"]
    assert result.name == "Grug — Code Review"
    assert result.head_sha == "head-123"
    assert result.status == "in_progress"
    assert result.conclusion is None
    assert posted["external_id"] == "grug-cr-pending:myorg/myrepo#7:head-123"



def test_enqueue_review_skips_in_progress_when_check_already_terminal(monkeypatch):
    """FIFO-deduped re-enqueue must not reopen a completed Elder check."""
    sent = {}
    posts = []

    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/review.fifo")
    monkeypatch.setattr(
        rerun._sqs, "send_message", lambda **kwargs: sent.update(kwargs),
    )
    monkeypatch.setattr(
        rerun,
        "_elder_check_already_terminal_or_pending",
        lambda **k: "already_completed_success",
    )
    monkeypatch.setattr(
        rerun,
        "post_check_run",
        lambda *a, **k: posts.append((a, k)) or {"id": 1},
    )
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )

    rerun.enqueue_review(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        requested_base_sha="base-123",
        requested_head_sha="head-123",
        requested_title="t",
        requested_body="b",
        settle_seconds=10,
    )

    assert sent["QueueUrl"].endswith("review.fifo")
    assert posts == []


def test_enqueue_review_survives_in_progress_check_failure(monkeypatch):
    """A GitHub blip on the pending check must not fail the durable enqueue —
    the SQS job is the correctness path; visibility is best-effort."""
    sent = {}

    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/review.fifo")
    monkeypatch.setattr(
        rerun._sqs, "send_message", lambda **kwargs: sent.update(kwargs),
    )
    monkeypatch.setattr(
        rerun, "_elder_check_already_terminal_or_pending", lambda **k: None,
    )
    monkeypatch.setattr(
        rerun,
        "with_install_token_retry",
        lambda iid, fn: (_ for _ in ()).throw(RuntimeError("github 500")),
    )

    # Must not raise.
    rerun.enqueue_review(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        requested_base_sha="base-123",
        requested_head_sha="head-123",
        requested_title="t",
        requested_body="b",
        settle_seconds=10,
    )

    assert sent["QueueUrl"] == "https://sqs.example/review.fifo"
    assert json.loads(sent["MessageBody"])["kind"] == "review"


def test_review_dedup_id_is_bounded_and_snapshot_scoped_for_long_repo_names():
    repo = f"{'o' * 39}/{'r' * 100}"
    first = rerun._review_dedup_id(11, repo, 7, "head-1")
    second = rerun._review_dedup_id(11, repo, 7, "head-2")

    assert len(first) <= 128
    assert first != second


def test_review_group_is_bounded_per_pr_not_global_per_installation():
    repo = f"{'o' * 39}/{'r' * 100}"
    first = rerun._review_group_id(11, repo, 7)
    other_pr = rerun._review_group_id(11, repo, 8)

    assert len(first) <= 128
    assert first != other_pr


def test_rerun_and_ask_use_separate_bounded_workload_groups(monkeypatch):
    sent = []
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/jobs.fifo")
    monkeypatch.setattr(
        rerun._sqs, "send_message", lambda **kwargs: sent.append(kwargs),
    )

    rerun.enqueue_rerun(
        install_id=11, repo="myorg/myrepo", pr_number=7, persona="elder",
    )
    rerun.enqueue_ask(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        comment_id=99,
        question="What changed?",
    )

    rerun_group = sent[0]["MessageGroupId"]
    ask_group = sent[1]["MessageGroupId"]
    assert rerun_group == rerun._rerun_group_id(11, "myorg/myrepo", 7, "elder")
    assert ask_group == rerun._ask_group_id(11, "myorg/myrepo", 7)
    assert rerun_group != ask_group
    assert len(rerun_group) <= 128
    assert len(ask_group) <= 128


def test_review_snapshot_identity_changes_for_same_head_intent_or_base_edit():
    common = {
        "head_sha": "same-head",
        "base_sha": "base-1",
        "title": "Initial title",
        "body": "Initial body",
    }
    original = rerun.review_snapshot_id(**common)

    for changed in (
        {**common, "base_sha": "base-2"},
        {**common, "title": "Updated title"},
        {**common, "body": "Updated body"},
    ):
        assert rerun.review_snapshot_id(**changed) != original


def test_hot_review_settles_then_dispatches_same_current_snapshot(monkeypatch):
    pulls = iter((_pr_data(head_sha="latest"), _pr_data(head_sha="latest")))
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(pulls),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {
        "code_reviewer_blocking": True,
    })
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    sleep = MagicMock()
    monkeypatch.setattr(rerun.time, "sleep", sleep)
    dispatch = MagicMock(return_value={"persona": "code_reviewer", "result": "pass"})
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    status = rerun._run_one(_job(
        kind="review", requested_head_sha="event-head", settle_seconds=90,
    ))

    assert status == "dispatched"
    sleep.assert_called_once_with(90)
    acquire.assert_called_once_with(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        persona="code_reviewer",
        head_sha=rerun.review_snapshot_id_from_pr(_pr_data(head_sha="latest")),
        owner_token=ANY,
        lease_seconds=rerun._REVIEW_CLAIM_LEASE_SECONDS,
    )
    owner_token = acquire.call_args.kwargs["owner_token"]
    assert owner_token
    complete.assert_called_once_with(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        persona="code_reviewer",
        head_sha=rerun.review_snapshot_id_from_pr(_pr_data(head_sha="latest")),
        owner_token=owner_token,
    )
    payload = dispatch.call_args.args[0]
    assert payload["pull_request"]["title"] == "Improve review depth"
    assert payload["pull_request"]["body"].startswith("Closes #6")
    assert payload["pull_request"]["base"]["sha"] == "base-sha"
    assert dispatch.call_args.kwargs["blocking"] is True
    release.assert_not_called()


def test_hot_review_cancels_when_snapshot_moves_during_quiet_window(monkeypatch):
    pulls = iter((
        _pr_data(head_sha="same-head", title="Before"),
        _pr_data(head_sha="same-head", title="After"),
    ))
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(pulls),
    )
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(rerun.time, "sleep", lambda seconds: None)
    requeue = MagicMock()
    monkeypatch.setattr(rerun, "_enqueue_current_review", requeue)
    dispatch = MagicMock()
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    status = rerun._run_one(_job(
        kind="review", requested_head_sha="same-head", settle_seconds=90,
    ))

    assert status == "stale_snapshot"
    release.assert_called_once_with(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        persona="code_reviewer",
        head_sha=rerun.review_snapshot_id_from_pr(
            _pr_data(head_sha="same-head", title="Before"),
        ),
        owner_token=acquire.call_args.kwargs["owner_token"],
    )
    complete.assert_not_called()
    dispatch.assert_not_called()
    requeue.assert_called_once_with(
        install_id=11,
        repo_full="myorg/myrepo",
        pr_number=7,
        pr=_pr_data(head_sha="same-head", title="After"),
        settle_seconds=90,
    )


def test_hot_review_stops_when_pr_becomes_draft_during_settle(monkeypatch):
    pulls = iter((
        _pr_data(head_sha="same-head"),
        _pr_data(head_sha="same-head", draft=True),
    ))
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(pulls),
    )
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(rerun.time, "sleep", lambda seconds: None)
    requeue = MagicMock()
    monkeypatch.setattr(rerun, "_enqueue_current_review", requeue)
    dispatch = MagicMock()
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    status = rerun._run_one(_job(kind="review", settle_seconds=90))

    assert status == "pr_ineligible"
    release.assert_called_once()
    assert release.call_args.kwargs["owner_token"] == acquire.call_args.kwargs["owner_token"]
    complete.assert_not_called()
    requeue.assert_not_called()
    dispatch.assert_not_called()


def test_hot_review_requeues_latest_when_dispatch_detects_stale(monkeypatch):
    original = _pr_data(head_sha="same-head", title="Before")
    latest = _pr_data(head_sha="same-head", title="After")
    pulls = iter((original, original, latest))
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(pulls),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    requeue = MagicMock()
    monkeypatch.setattr(rerun, "_enqueue_current_review", requeue)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "stale_snapshot",
        }),
    )

    status = rerun._run_one(_job(kind="review", settle_seconds=0))

    assert status == "stale_snapshot"
    requeue.assert_called_once_with(
        install_id=11,
        repo_full="myorg/myrepo",
        pr_number=7,
        pr=latest,
        settle_seconds=0,
    )
    release.assert_called_once()
    assert release.call_args.kwargs["owner_token"] == acquire.call_args.kwargs["owner_token"]
    complete.assert_not_called()


def test_hot_review_duplicate_current_snapshot_skips_without_wait(monkeypatch):
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: _pr_data(head_sha="same"),
    )
    acquire, complete, release = _patch_hot_claims(
        monkeypatch, status="completed",
    )
    sleep = MagicMock()
    monkeypatch.setattr(rerun.time, "sleep", sleep)
    dispatch = MagicMock()
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    status = rerun._run_one(_job(
        kind="review", requested_head_sha="same", settle_seconds=90,
    ))

    assert status == "duplicate_snapshot"
    acquire.assert_called_once()
    complete.assert_not_called()
    release.assert_not_called()
    sleep.assert_not_called()
    dispatch.assert_not_called()


def test_hot_review_draft_skips_before_claim_so_ready_event_can_reuse_head(monkeypatch):
    monkeypatch.setattr(
        rerun,
        "with_install_token_retry",
        lambda iid, fn: _pr_data(head_sha="draft-head", draft=True),
    )
    acquire = MagicMock()
    monkeypatch.setattr("adapters.install_store.acquire_review_claim", acquire)
    sleep = MagicMock()
    monkeypatch.setattr(rerun.time, "sleep", sleep)
    dispatch = MagicMock()
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    out = rerun.handle_rerun_jobs(
        _event(_job(kind="review", requested_head_sha="draft-head")),
    )

    assert out == {"records": 1, "dispatched": 0, "skipped": 1}
    acquire.assert_not_called()
    sleep.assert_not_called()
    dispatch.assert_not_called()


def test_hot_review_dispatch_error_releases_claim_and_raises_for_redrive(monkeypatch):
    monkeypatch.setattr(
        rerun,
        "with_install_token_retry",
        lambda iid, fn: _pr_data(head_sha="retry-head"),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(side_effect=RuntimeError("review crashed")),
    )

    with pytest.raises(RuntimeError, match="review crashed"):
        rerun._run_one(_job(kind="review", settle_seconds=0))

    release.assert_called_once_with(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        persona="code_reviewer",
        head_sha=rerun.review_snapshot_id_from_pr(_pr_data(head_sha="retry-head")),
        owner_token=acquire.call_args.kwargs["owner_token"],
    )
    complete.assert_not_called()


def test_shutdown_release_frees_claims_orphaned_by_a_killed_review(monkeypatch):
    """grug#515 blocking hardening: a consumer pod killed mid-review never
    reaches its except/finally, orphaning the snapshot claim for up to the
    900s lease and bouncing the SQS redelivery off "claim busy". main() calls
    release_active_review_claims() on shutdown; every still-registered claim
    must be released exactly once and the registry drained."""
    release = MagicMock(return_value=True)
    monkeypatch.setattr("adapters.install_store.release_review_claim", release)

    args = {
        "install_id": 11, "repo": "o/r", "pr_number": 7,
        "persona": "code_reviewer", "head_sha": "snap", "owner_token": "tok-1",
    }
    rerun._register_active_review_claim("tok-1", args)

    assert rerun.release_active_review_claims() == 1
    release.assert_called_once_with(**args)
    # Registry drained: a second sweep has nothing to do.
    assert rerun.release_active_review_claims() == 0


def test_shutdown_release_is_best_effort_per_claim(monkeypatch):
    """One claim whose release raises (or lost ownership) must not stop the
    rest of the sweep - shutdown has a hard deadline."""
    calls = []

    def _release(**kw):
        calls.append(kw["owner_token"])
        if kw["owner_token"] == "tok-a":
            raise RuntimeError("store down")
        return True

    monkeypatch.setattr("adapters.install_store.release_review_claim", _release)
    base = {"install_id": 1, "repo": "o/r", "pr_number": 1,
            "persona": "code_reviewer", "head_sha": "s"}
    rerun._register_active_review_claim("tok-a", {**base, "owner_token": "tok-a"})
    rerun._register_active_review_claim("tok-b", {**base, "owner_token": "tok-b"})

    assert rerun.release_active_review_claims() == 1  # tok-b released
    assert set(calls) == {"tok-a", "tok-b"}


def test_in_process_exits_unregister_so_shutdown_sweep_sees_nothing(monkeypatch):
    """Every IN-PROCESS exit (here: dispatch error -> except-release -> raise)
    must unregister its claim, so the shutdown sweep never double-releases a
    claim the handler already released itself."""
    monkeypatch.setattr(
        rerun,
        "with_install_token_retry",
        lambda iid, fn: _pr_data(head_sha="unreg-head"),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(side_effect=RuntimeError("review crashed")),
    )

    with pytest.raises(RuntimeError, match="review crashed"):
        rerun._run_one(_job(kind="review", settle_seconds=0))

    assert rerun.release_active_review_claims() == 0


def test_hot_review_publish_failure_releases_claim_and_raises_for_redrive(monkeypatch):
    monkeypatch.setattr(
        rerun,
        "with_install_token_retry",
        lambda iid, fn: _pr_data(head_sha="publish-head"),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "publish_failed",
        }),
    )

    with pytest.raises(RuntimeError, match="publication failed"):
        rerun._run_one(_job(kind="review", settle_seconds=0))

    release.assert_called_once_with(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        persona="code_reviewer",
        head_sha=rerun.review_snapshot_id_from_pr(_pr_data(head_sha="publish-head")),
        owner_token=acquire.call_args.kwargs["owner_token"],
    )
    complete.assert_not_called()


def test_hot_review_unexpected_result_releases_claim_and_redrives(monkeypatch):
    monkeypatch.setattr(
        rerun,
        "with_install_token_retry",
        lambda iid, fn: _pr_data(head_sha="unexpected-head"),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "unhandled_error",
        }),
    )

    with pytest.raises(RuntimeError, match="unexpected result"):
        rerun._run_one(_job(kind="review", settle_seconds=0))

    release.assert_called_once()
    assert release.call_args.kwargs["owner_token"] == (
        acquire.call_args.kwargs["owner_token"]
    )
    complete.assert_not_called()


def test_hot_review_model_outage_releases_claim_and_raises_for_redrive(monkeypatch):
    monkeypatch.setattr(
        rerun,
        "with_install_token_retry",
        lambda iid, fn: _pr_data(head_sha="outage-head"),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "all_failed",
        }),
    )

    with pytest.raises(RuntimeError, match="all_failed"):
        rerun._run_one(_job(kind="review", settle_seconds=0))

    release.assert_called_once_with(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        persona="code_reviewer",
        head_sha=rerun.review_snapshot_id_from_pr(_pr_data(head_sha="outage-head")),
        owner_token=acquire.call_args.kwargs["owner_token"],
    )
    complete.assert_not_called()


def test_hot_review_release_failure_redrives_until_lease_is_reclaimable(monkeypatch):
    """A failed claim release must not turn redelivery into a dropped job."""
    monkeypatch.setattr(
        rerun,
        "with_install_token_retry",
        lambda iid, fn: _pr_data(head_sha="recover-head"),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    acquire.side_effect = ["acquired", "busy", "acquired"]
    release.side_effect = RuntimeError("database unavailable during release")
    dispatch = MagicMock(side_effect=[
        RuntimeError("review crashed"),
        {"persona": "code_reviewer", "result": "pass"},
    ])
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    with pytest.raises(RuntimeError, match="review crashed"):
        rerun._run_one(_job(kind="review", settle_seconds=0))
    with pytest.raises(RuntimeError, match="claim is still in progress"):
        rerun._run_one(_job(kind="review", settle_seconds=0))

    assert rerun._run_one(_job(kind="review", settle_seconds=0)) == "dispatched"
    assert acquire.call_count == 3
    assert dispatch.call_count == 2
    assert release.call_count == 1
    complete.assert_called_once()


def test_rerun_dispatches_persona_on_current_head():
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response(head_sha="deadbeef", repo_id=222)), \
         patch.object(rerun, "get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch.object(rerun, "dispatch_code_review") as disp:
        out = rerun.handle_rerun_jobs(_event(_job()))
    assert out == {"records": 1, "dispatched": 1, "skipped": 0}
    payload, kwargs = disp.call_args.args[0], disp.call_args.kwargs
    # Re-run targets the PR's CURRENT head (fetched), not the errored row's.
    assert payload["pull_request"]["head"]["sha"] == "deadbeef"
    assert payload["pull_request"]["base"]["sha"] == "base-sha"
    assert payload["pull_request"]["title"] == "Improve review depth"
    assert payload["pull_request"]["user"]["login"] == "evan"
    assert payload["repository"]["owner"]["login"] == "myorg"  # from the repo string
    assert payload["repository"]["name"] == "myrepo"
    assert payload["repository"]["id"] == 222  # from pr.base.repo.id
    assert payload["installation"]["id"] == 11
    assert kwargs["blocking"] is False


def test_rerun_blocking_flag_from_repo_config():
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response()), \
         patch.object(rerun, "get_repo_config", return_value={"code_reviewer_blocking": True}) as cfg, \
         patch.object(rerun, "dispatch_code_review") as disp:
        rerun.handle_rerun_jobs(_event(_job()))
    cfg.assert_called_once_with(11, 222)  # repo_id derived from the PR fetch
    assert disp.call_args.kwargs["blocking"] is True


def test_rerun_skips_unsupported_persona_without_dispatch():
    with patch.object(rerun, "dispatch_code_review") as disp, \
         patch("httpx.get") as get:
        out = rerun.handle_rerun_jobs(_event(_job(persona="chief")))
    assert out == {"records": 1, "dispatched": 0, "skipped": 1}
    disp.assert_not_called()
    get.assert_not_called()  # never even fetches for a persona we don't drive


def test_rerun_raises_on_github_fetch_failure_so_esm_retries():
    # An infra failure must propagate → ESM retry (visibility timeout) → DLQ.
    with patch.object(rerun, "with_install_token_retry",
                      side_effect=httpx.RequestError("github down")):
        with pytest.raises(httpx.RequestError):
            rerun.handle_rerun_jobs(_event(_job()))


def test_rerun_raises_on_malformed_message():
    with pytest.raises(json.JSONDecodeError):
        rerun.handle_rerun_jobs(_event("not json"))  # → DLQ after retries


def test_rerun_failure_raises_and_never_reenqueues():
    """#418 loop guard: a failing re-run RAISES (so SQS redrives → DLQ) and
    NEVER enqueues another re-run — the consumer calls dispatch directly, so
    Elder self-recovery enqueues at most once per drop, never loops."""
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response()), \
         patch.object(rerun, "get_repo_config", return_value={}), \
         patch.object(rerun, "dispatch_code_review", side_effect=RuntimeError("review boom")), \
         patch.object(rerun, "enqueue_rerun") as enq:
        with pytest.raises(RuntimeError):
            rerun.handle_rerun_jobs(_event(_job()))
    enq.assert_not_called()


def test_run_one_dispatches_guard(monkeypatch):
    """#466 (codex PR #482): a guard rerun job drives dispatch_guard_review
    with the repo's guard_blocking flag - previously it was skipped as an
    unsupported persona while the API happily queued it."""
    import json as _json

    import rerun as rr

    monkeypatch.setattr(
        rr, "with_install_token_retry",
        lambda iid, fn: {"head": {"sha": "abc"}, "base": {"repo": {"id": 7}}},
    )
    monkeypatch.setattr(
        rr, "get_repo_config",
        lambda iid, rid: {"guard_blocking": True, "code_reviewer_blocking": False},
    )
    called = {}
    monkeypatch.setattr(
        rr, "dispatch_guard_review",
        lambda payload, *, blocking: called.update(blocking=blocking) or {},
    )
    monkeypatch.setattr(
        rr, "dispatch_code_review",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("elder must not run")),
    )
    status = rr._run_one(_json.dumps({
        "install_id": 1, "repo": "o/r", "pr_number": 5, "persona": "guard",
    }))
    assert status == "dispatched"
    assert called["blocking"] is True


def test_elder_check_already_terminal_treats_any_completed_conclusion(monkeypatch):
    """action_required and stale are completed; do not reopen as in_progress."""
    for conclusion in ("action_required", "stale", "success", ""):
        captured = {}

        def with_token(install_id, fn, _c=conclusion):
            class Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "check_runs": [
                            {
                                "name": "Grug — Code Review",
                                "status": "completed",
                                "conclusion": _c,
                            }
                        ]
                    }

            import httpx as _httpx
            # inject via monkeypatch on httpx.get below
            return fn("tok")

        def fake_get(url, **kwargs):
            class Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "check_runs": [
                            {
                                "name": "Grug — Code Review",
                                "status": "completed",
                                "conclusion": conclusion,
                            }
                        ]
                    }
            return Resp()

        monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
        monkeypatch.setattr(rerun.httpx, "get", fake_get)
        reason = rerun._elder_check_already_terminal_or_pending(
            install_id=1, owner="o", repo_name="r", head_sha="h" * 40,
        )
        assert reason is not None
        assert reason.startswith("already_completed_"), (conclusion, reason)
