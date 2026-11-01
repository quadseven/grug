"""Tests for the re-run consumer (#305, ADR-0004).

External behavior: a job (keyed by repo "owner/name") re-fetches the PR's
current head and dispatches the named persona; an unsupported persona is
skipped (not retried); an infra failure RAISES so the ESM retries → DLQ.
"""
from __future__ import annotations

import json
import logging
import threading
import time
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
        "user": {"login": "alice"},
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
    assert result.name == "Grug - Elder"
    assert result.head_sha == "head-123"
    assert result.status == "in_progress"
    assert result.conclusion is None
    assert posted["external_id"] == "grug-cr-pending:myorg/myrepo#7:head-123"  # leak-guard-allow: placeholder repo, not a real one



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
        kind="review", requested_head_sha="latest", settle_seconds=90,
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
        redrive_count=1,
    )


def test_hot_review_gives_up_at_entry_once_the_redrive_cap_is_exhausted(monkeypatch):
    """grug#773 AC3: a review discarded and re-enqueued against the SAME
    head_sha _MAX_INTENT_DRIFT_REDRIVES times running gives up with a
    terminal check instead of looping forever - the backstop for a PR whose
    title/body keeps changing faster than Elder can finish. WARN mode here
    (neutral); BLOCK mode fails closed, see
    test_elder_degraded_fail_closed.py (grug#1212)."""
    pulls = iter((
        _pr_data(head_sha="same-head", title="Before"),
        _pr_data(head_sha="same-head", title="After"),
    ))

    def _fake_token_retry(iid, fn):
        try:
            return next(pulls)
        except StopIteration:
            return fn("tok")  # the give-up check-post call

    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    posted: list = []
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    monkeypatch.setattr(rerun.time, "sleep", lambda seconds: None)
    requeue = MagicMock()
    monkeypatch.setattr(rerun, "_enqueue_current_review", requeue)
    dispatch = MagicMock()
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    status = rerun._run_one(_job(
        kind="review", requested_head_sha="same-head", settle_seconds=90,
        redrive_count=rerun._MAX_INTENT_DRIFT_REDRIVES,
    ))

    assert status == "intent_drift_exhausted"
    requeue.assert_not_called()  # never loops again
    dispatch.assert_not_called()
    assert len(posted) == 1
    assert posted[0].head_sha == "same-head"
    assert posted[0].status == "completed"
    assert posted[0].conclusion == "neutral"


def test_hot_review_new_head_resets_the_redrive_count(monkeypatch):
    """A genuinely new commit always redrives with a fresh count - AC3 bounds
    looping on an UNCHANGED sha, never an ordinary incremental push, even if
    the job carried an exhausted count from the OLD head's own loop."""
    pulls = iter((
        _pr_data(head_sha="same-head", title="Before"),
        _pr_data(head_sha="new-head", title="Before"),
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
        redrive_count=rerun._MAX_INTENT_DRIFT_REDRIVES,
    ))

    assert status == "stale_snapshot"
    requeue.assert_called_once_with(
        install_id=11,
        repo_full="myorg/myrepo",
        pr_number=7,
        pr=_pr_data(head_sha="new-head", title="Before"),
        settle_seconds=90,
        redrive_count=0,
    )


def test_hot_review_new_head_is_not_redriven_while_elder_is_paused(monkeypatch):
    """grug#1188: a push held by auto-pause enqueued nothing on the webhook
    side; the consumer cancelling a superseded review must not enqueue that
    held head behind the pause's back."""
    pulls = iter((
        _pr_data(head_sha="same-head", title="Before"),
        _pr_data(head_sha="new-head", title="Before"),
    ))
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(pulls),
    )
    _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(rerun.time, "sleep", lambda seconds: None)
    paused_lookups: list[dict] = []
    monkeypatch.setattr(
        rerun, "is_elder_paused",
        lambda **k: paused_lookups.append(k) or True,
    )
    requeue = MagicMock()
    monkeypatch.setattr(rerun, "_enqueue_current_review", requeue)
    dispatch = MagicMock()
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    status = rerun._run_one(_job(
        kind="review", requested_head_sha="same-head", settle_seconds=90,
    ))

    assert status == "stale_snapshot"
    requeue.assert_not_called()
    dispatch.assert_not_called()
    assert paused_lookups == [
        {"install_id": 11, "repo": "myorg/myrepo", "pr_number": 7},
    ]


def test_hot_review_same_head_redrive_ignores_the_pause(monkeypatch):
    """A title/body change on the head already under review is not a held
    push: it redrives as before, without consulting the pause."""
    pulls = iter((
        _pr_data(head_sha="same-head", title="Before"),
        _pr_data(head_sha="same-head", title="After"),
    ))
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(pulls),
    )
    _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(rerun.time, "sleep", lambda seconds: None)

    def _no_lookup(**_k):
        raise AssertionError("pause consulted for a same-head redrive")

    monkeypatch.setattr(rerun, "is_elder_paused", _no_lookup)
    requeue = MagicMock()
    monkeypatch.setattr(rerun, "_enqueue_current_review", requeue)
    monkeypatch.setattr(rerun, "dispatch_code_review", MagicMock())

    assert rerun._run_one(_job(
        kind="review", requested_head_sha="same-head", settle_seconds=90,
    )) == "stale_snapshot"
    requeue.assert_called_once()


def test_hot_review_pause_lookup_failure_redrives_the_new_head(monkeypatch, caplog):
    """The pause check fails open: a store error redrives as before."""
    pulls = iter((
        _pr_data(head_sha="same-head", title="Before"),
        _pr_data(head_sha="new-head", title="Before"),
    ))
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(pulls),
    )
    _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(rerun.time, "sleep", lambda seconds: None)

    def _down(**_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(rerun, "is_elder_paused", _down)
    requeue = MagicMock()
    monkeypatch.setattr(rerun, "_enqueue_current_review", requeue)
    monkeypatch.setattr(rerun, "dispatch_code_review", MagicMock())

    assert rerun._run_one(_job(
        kind="review", requested_head_sha="same-head", settle_seconds=90,
    )) == "stale_snapshot"
    requeue.assert_called_once()
    assert "elder_redrive_pause_lookup_failed" in caplog.text


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
    # Same-SHA intent change: NO neutral check post happens (the requeued
    # review on this head completes the in_progress check), so only the
    # three PR fetches consume the iterator.
    pulls = iter((original, original, latest))
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(pulls),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    # #819: board note not under test here
    monkeypatch.setattr(rerun, "_set_board_in_progress_note", lambda *a: None)
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
        redrive_count=1,
    )
    release.assert_called_once()
    assert release.call_args.kwargs["owner_token"] == acquire.call_args.kwargs["owner_token"]
    complete.assert_not_called()


def test_hot_review_pr_read_403_completes_check_without_redrive(monkeypatch):
    response = httpx.Response(
        403,
        request=httpx.Request(
            "GET", "https://api.github.com/repos/myorg/myrepo/pulls/7",
        ),
    )

    attempts = []

    def denied_then_post(iid, fn):
        attempts.append(iid)
        if len(attempts) == 1:
            raise httpx.HTTPStatusError("Forbidden", request=response.request, response=response)
        return fn("token")

    posted = []
    monkeypatch.setattr(rerun, "with_install_token_retry", denied_then_post)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )

    status = rerun._run_one(_job(
        kind="review", requested_head_sha="head-that-was-queued",
    ))

    assert status == "pr_read_forbidden"
    assert len(posted) == 1
    assert posted[0].title == "Grug cannot read this pull request: 403 from GitHub"
    assert posted[0].head_sha == "head-that-was-queued"
    assert posted[0].conclusion == "failure"
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "headers",
    [
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1800000000"},
        {"Retry-After": "60"},
    ],
    ids=["primary", "secondary"],
)
def test_hot_review_pr_read_rate_limit_403_redrives_without_a_permission_title(
    monkeypatch, headers,
):
    """A rate-limited 403 clears on its own: a terminal "cannot read" check
    would send the operator to the App's permissions over nothing."""
    response = httpx.Response(
        403,
        headers=headers,
        request=httpx.Request(
            "GET", "https://api.github.com/repos/myorg/myrepo/pulls/7",
        ),
    )

    def limited(iid, fn):
        raise httpx.HTTPStatusError(
            "Forbidden", request=response.request, response=response,
        )

    posted = []
    monkeypatch.setattr(rerun, "with_install_token_retry", limited)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )

    with pytest.raises(httpx.HTTPStatusError):
        rerun._run_one(_job(kind="review", requested_head_sha="head-that-was-queued"))

    assert posted == []


@pytest.mark.parametrize(
    ("refusal", "title", "cause"),
    [
        (
            "forbidden",
            "Grug cannot read this pull request: 403 from GitHub",
            "HTTP 403",
        ),
        (
            "rate_limited",
            "Elder eyes clouded - GitHub rate limit",
            "rate limit",
        ),
        (None, "Elder eyes clouded - GitHub unavailable", "5xx / transport"),
    ],
)
def test_hot_review_freshness_refusal_names_what_github_said(
    monkeypatch, refusal, title, cause,
):
    """The second case on the ticket: a read that GitHub refused mid-review
    read as "GitHub unavailable" whatever the refusal actually was."""
    pr = _pr_data(head_sha="fresh-head")
    fetches = iter((pr, pr))

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    posted: list = []
    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    _patch_hot_claims(monkeypatch)
    dispatched = {
        "persona": "code_reviewer",
        "result": "skipped",
        "degraded_reason": "freshness_check_failed",
    }
    if refusal:
        dispatched["github_refusal"] = refusal
    monkeypatch.setattr(rerun, "dispatch_code_review", MagicMock(return_value=dispatched))

    status = rerun._run_one(_job(kind="review", settle_seconds=0))

    assert status == "fail_open_freshness"
    assert posted[0].title == title
    assert cause in posted[0].summary


def test_hot_review_gives_up_mid_flight_once_the_redrive_cap_is_exhausted(monkeypatch):
    """grug#773 AC3's other call site: the EXPENSIVE mid-flight staleness
    path (a real dispatch already ran) must also stop redriving once the
    cap is hit, rather than repeating a full reasoner-model call forever."""
    original = _pr_data(head_sha="same-head", title="Before")
    latest = _pr_data(head_sha="same-head", title="After")
    fetches = iter((original, original, latest))
    posted: list = []

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")  # the give-up check-post call

    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    # #819: board note not under test here
    monkeypatch.setattr(rerun, "_set_board_in_progress_note", lambda *a: None)
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

    status = rerun._run_one(_job(
        kind="review", settle_seconds=0,
        redrive_count=rerun._MAX_INTENT_DRIFT_REDRIVES,
    ))

    assert status == "intent_drift_exhausted"
    requeue.assert_not_called()
    assert len(posted) == 1
    assert posted[0].head_sha == "same-head"
    assert posted[0].status == "completed"
    assert posted[0].conclusion == "neutral"
    release.assert_called_once()


def test_hot_review_retires_at_entry_when_already_superseded(monkeypatch):
    """grug#892: a job whose requested_head_sha no longer matches the live
    current head retires BEFORE any claim/settle/dispatch work - the exact
    incident shape (a stale job burning its retry budget on GitHub errors
    tied to a commit nobody cares about anymore, blocking the real job for
    the current head behind it in the same FIFO group)."""
    fetches = iter((_pr_data(head_sha="fresh-head"),))
    posted: list = []

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            # Fetch exhausted: this is the check-post call.
            return fn("tok")

    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    dispatch = MagicMock()
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    status = rerun._run_one(_job(
        kind="review", requested_head_sha="stale-head", settle_seconds=90,
    ))

    assert status == "superseded_at_entry"
    acquire.assert_not_called()   # no claim work for a job that never runs
    dispatch.assert_not_called()  # never reaches the real review
    complete.assert_not_called()
    release.assert_not_called()
    assert len(posted) == 1
    assert posted[0].head_sha == "stale-head"  # closes the SUPERSEDED head
    assert posted[0].status == "completed"
    assert posted[0].conclusion == "neutral"


def test_hot_review_does_not_retire_when_requested_head_matches_current(monkeypatch):
    """The other half of #892's contract: a job whose head IS current must
    proceed exactly as before - no early-retire false positive."""
    fetches = iter((_pr_data(head_sha="current-head"),))
    posted: list = []
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(fetches),
    )
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    acquire, complete, release = _patch_hot_claims(monkeypatch, status="completed")

    status = rerun._run_one(_job(
        kind="review", requested_head_sha="current-head", settle_seconds=90,
    ))

    assert status == "duplicate_snapshot"  # reached the claim step, not retired
    acquire.assert_called_once()
    assert posted == []  # no superseded-head check posted


def test_hot_review_newer_job_dispatches_when_stale_job_precedes_it_in_group(monkeypatch):
    """grug#892 acceptance: the newer job for the current head is processed
    even though an older, now-superseded job for the same PR precedes it in
    the same FIFO group's batch - the stale one retires without blocking it.

    Decouples the check-post token call from PR-data fetches (rather than
    one flat with_install_token_retry iterator, per the other tests here) -
    with two jobs in one batch, the stale job's check-post call would
    otherwise land BETWEEN the two jobs' real PR fetches and desync a
    single shared iterator's exhaustion point from which call is which."""
    pr_fetches = iter((
        _pr_data(head_sha="fresh-head"),   # stale job's entry check
        _pr_data(head_sha="fresh-head"),   # fresh job's entry check
        _pr_data(head_sha="fresh-head"),   # fresh job's post-settle check
    ))
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_fetch_current_pr", lambda *a, **k: next(pr_fetches))
    monkeypatch.setattr(rerun, "post_check_run", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    dispatch = MagicMock(return_value={"persona": "code_reviewer", "result": "pass"})
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    out = rerun.handle_rerun_jobs(_event(
        _job(kind="review", requested_head_sha="stale-head", settle_seconds=0),
        _job(kind="review", requested_head_sha="fresh-head", settle_seconds=0),
    ))

    assert out == {"records": 2, "dispatched": 1, "skipped": 1}
    dispatch.assert_called_once()  # only the fresh job's job ever reached dispatch
    acquire.assert_called_once()   # only the fresh job ever acquired a claim


def test_hot_review_stale_with_moved_head_closes_abandoned_check(monkeypatch):
    """When the head actually moved, the abandoned head's in_progress check
    is completed neutral (fail-open) before the requeue."""
    original = _pr_data(head_sha="old-head", title="T")
    latest = _pr_data(head_sha="new-head", title="T")
    fetches = iter((original, original, latest))
    posted: list = []

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            # Fetches exhausted: this is the check-post call.
            return fn("tok")

    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    # #819: board note not under test here
    monkeypatch.setattr(rerun, "_set_board_in_progress_note", lambda *a: None)
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
    assert len(posted) == 1
    assert posted[0].status == "completed"
    assert posted[0].conclusion == "neutral"
    requeue.assert_called_once()
    release.assert_called_once()
    complete.assert_not_called()


def test_hot_review_same_sha_stale_never_posts_terminal_neutral(monkeypatch):
    """A title/body-only staleness on the SAME head must not complete the
    required check - that would green the merge button before the requeued
    review runs."""
    original = _pr_data(head_sha="same-head", title="Before")
    latest = _pr_data(head_sha="same-head", title="After")
    pulls = iter((original, original, latest))
    posted: list = []
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: next(pulls),
    )
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda *a, **kw: posted.append(a) or {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    # #819: board note not under test here
    monkeypatch.setattr(rerun, "_set_board_in_progress_note", lambda *a: None)
    _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(rerun, "_enqueue_current_review", MagicMock())
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "stale_snapshot",
        }),
    )

    assert rerun._run_one(_job(kind="review", settle_seconds=0)) == "stale_snapshot"
    assert posted == []


def test_hot_review_freshness_outage_fail_open_completes_and_finishes(monkeypatch):
    """GH-brownout skip completes the required check neutral and finishes the
    claim (no redrive) when the neutral post LANDS."""
    pr = _pr_data(head_sha="fresh-head")
    fetches = iter((pr, pr))

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    posted: list = []
    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "freshness_check_failed",
        }),
    )

    status = rerun._run_one(_job(kind="review", settle_seconds=0))

    assert status == "fail_open_freshness"
    assert len(posted) == 1
    assert posted[0].conclusion == "neutral"
    complete.assert_called_once()
    release.assert_not_called()


def test_partial_review_keeps_elders_own_check_instead_of_overwriting_it(monkeypatch):
    """A partial-coverage review ALREADY published a complete check carrying
    real cohort coverage. It used to fall through to the "Unknown skip"
    fail-open branch, which re-posted a generic "Elder skipped -
    partial_review" over it - replacing real content with less, and leaving
    the check and the board saying different things about the same pass."""
    pr = _pr_data(head_sha="fresh-head")
    fetches = iter((pr, pr))

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    posted: list = []
    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "partial_review",
        }),
    )

    status = rerun._run_one(_job(kind="review", settle_seconds=0))

    assert status == "dispatched"
    # No fail-open re-post: Elder's own check stands.
    assert posted == []
    complete.assert_called_once()
    release.assert_not_called()


def test_genuinely_unknown_skip_still_fails_open(monkeypatch):
    """The fail-open net still catches reasons nobody modelled - it just no
    longer swallows partial_review, which is a KNOWN, self-completing state."""
    pr = _pr_data(head_sha="fresh-head")
    fetches = iter((pr, pr))

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    posted: list = []
    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "something_nobody_modelled",
        }),
    )

    status = rerun._run_one(_job(kind="review", settle_seconds=0))

    assert status == "fail_open_something_nobody_modelled"
    assert len(posted) == 1
    assert posted[0].conclusion == "neutral"


def test_hot_review_freshness_outage_post_failure_raises_for_redrive(monkeypatch):
    """If the SAME brownout eats the neutral completion, the job must raise
    for redrive - otherwise the required check sticks in_progress forever
    with no retry, the exact bug fail-open exists to prevent."""
    pr = _pr_data(head_sha="fresh-head")
    fetches = iter((pr, pr))

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            raise httpx.ConnectError("github still down")

    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "freshness_check_failed",
        }),
    )

    with pytest.raises(RuntimeError, match="did not land"):
        rerun._run_one(_job(kind="review", settle_seconds=0))

    release.assert_called_once()
    complete.assert_not_called()


def test_hot_review_ineligible_dispatch_leaves_check_non_terminal(monkeypatch):
    """Draft/closed dispatch exits must NOT terminal-complete the check: a
    neutral on this head would satisfy the required check the moment the PR
    is reopened, before the fresh review posts."""
    posted: list = []
    monkeypatch.setattr(
        rerun, "with_install_token_retry", lambda iid, fn: _pr_data(),
    )
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda *a, **kw: posted.append(a) or {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "pr_ineligible",
        }),
    )

    assert rerun._run_one(_job(kind="review", settle_seconds=0)) == "pr_ineligible"
    assert posted == []


def test_elder_terminal_guard_ignores_fail_open_completions(monkeypatch):
    """A grug-cr-open (fail-open) completion must not suppress a fresh
    review's in_progress post - the new enqueue IS the worker that will
    re-complete the check."""
    def fake_get(_url: str, **kwargs: object) -> object:
        class Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "check_runs": [
                        {
                            "name": rerun._ELDER_CHECK_NAME,
                            "status": "completed",
                            "conclusion": "neutral",
                            "external_id": "grug-cr-open:o/r#9:" + "a" * 40,
                        }
                    ]
                }
        return Resp()

    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun.httpx, "get", fake_get)
    reason = rerun._elder_check_already_terminal_or_pending(
        install_id=1, owner="o", repo_name="r", head_sha="a" * 40,
    )
    assert reason is None


def test_hot_review_fail_open_both_claim_settlements_failing_raises(monkeypatch):
    """complete fails AND fallback release fails -> raise for redrive, never
    report success with the claim still held."""
    pr = _pr_data(head_sha="fresh-head")
    fetches = iter((pr, pr))

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    _patch_hot_claims(monkeypatch, complete_result=False, release_result=False)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "freshness_check_failed",
        }),
    )

    with pytest.raises(RuntimeError, match="claim settlement failed"):
        rerun._run_one(_job(kind="review", settle_seconds=0))


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


def test_hot_review_rejected_review_completes_claim_and_does_not_redrive(monkeypatch):
    """grug#770: a deterministic 4xx on the inline review (GitHub 422 for an
    out-of-diff comment) is NOT a publish failure to redrive - the identical
    payload is rejected identically every time, and four jobs each burned
    all five redrives into the DLQ proving it. The check-run already landed,
    so the job completes its claim and returns normally; the consumer then
    deletes the message. Exactly one attempt."""
    monkeypatch.setattr(
        rerun,
        "with_install_token_retry",
        lambda iid, fn: _pr_data(head_sha="rejected-head"),
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    dispatch = MagicMock(return_value={
        "persona": "code_reviewer",
        "result": "review_rejected",
    })
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)

    assert rerun._run_one(_job(kind="review", settle_seconds=0)) == "dispatched"

    assert dispatch.call_count == 1
    complete.assert_called_once_with(
        install_id=11,
        repo="myorg/myrepo",
        pr_number=7,
        persona="code_reviewer",
        head_sha=rerun.review_snapshot_id_from_pr(_pr_data(head_sha="rejected-head")),
        owner_token=acquire.call_args.kwargs["owner_token"],
    )
    release.assert_not_called()


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
    assert payload["pull_request"]["user"]["login"] == "alice"
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
                                "name": "Grug - Elder",
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
                                "name": "Grug - Elder",
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


def test_elder_check_already_terminal_accepts_legacy_alias(monkeypatch):
    """Cutover: a completed legacy 'Grug - Code Review' still blocks re-post."""
    seen_params: list[dict] = []

    def fake_get(_url: str, **kwargs: object) -> object:
        seen_params.append(dict(kwargs.get("params") or {}))  # type: ignore[call-overload]

        class Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "check_runs": [
                        {
                            "name": "Grug - Code Review",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
        return Resp()

    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun.httpx, "get", fake_get)
    reason = rerun._elder_check_already_terminal_or_pending(
        install_id=1, owner="o", repo_name="r", head_sha="h" * 40,
    )
    assert reason == "already_completed_success"
    # Guard the cutover contract server-side too: a check_name param on ANY
    # request would make GitHub filter OUT the legacy alias run and this test
    # would pass for the wrong reason. Also pin the params that make the
    # pagination guard correct so a refactor can't silently drop them.
    assert seen_params
    for params in seen_params:
        assert "check_name" not in params
        assert params.get("filter") == "latest"
        assert params.get("per_page") == 100


def test_elder_terminal_guard_paginates_to_find_later_page_run(monkeypatch):
    """A busy commit can push the Elder run onto a later page; the guard must
    paginate or it would miss it and wrongly re-post in_progress."""
    pages = {
        1: {
            "total_count": 150,
            "check_runs": [
                {"name": f"CI-{i}", "status": "completed", "conclusion": "success"}
                for i in range(100)
            ],
        },
        2: {
            "total_count": 150,
            "check_runs": (
                [{"name": f"CI-{i}", "status": "completed", "conclusion": "success"}
                 for i in range(100, 149)]
                + [{"name": "Grug — Code Review", "status": "completed",
                    "conclusion": "success", "external_id": "grug-cr:x"}]
            ),
        },
    }
    seen_pages: list[int] = []

    def fake_get(_url: str, **kwargs: object) -> object:
        params = dict(kwargs.get("params") or {})  # type: ignore[call-overload]
        page = int(params.get("page", 1))
        seen_pages.append(page)

        class Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return pages[page]
        return Resp()

    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun.httpx, "get", fake_get)
    reason = rerun._elder_check_already_terminal_or_pending(
        install_id=1, owner="o", repo_name="r", head_sha="h" * 40,
    )
    assert reason == "already_completed_success"
    assert seen_pages == [1, 2], "must walk to page 2 to find the Elder run"


def test_complete_elder_check_open_posts_neutral(monkeypatch):
    """Fail-open: infra skip completes the required check (never stuck)."""
    posted = {}

    def fake_post(token, owner, repo, result, external_id=None):
        posted["result"] = result
        posted["external_id"] = external_id
        return {"id": 1}

    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "post_check_run", fake_post)
    rerun._complete_elder_check_open(
        install_id=1,
        owner="o",
        repo_name="r",
        pr_number=9,
        head_sha="a" * 40,
        title="Elder eyes clouded - GitHub unavailable",
        summary="fail open",
        conclusion="neutral",
    )
    assert posted["result"].status == "completed"
    assert posted["result"].conclusion == "neutral"
    assert posted["result"].name == rerun._ELDER_CHECK_NAME
    assert "open" in posted["external_id"]


# --- reply-mined learnings consumer job (#670, ADR-0020) --------------------

def test_run_learn_durable_stores_and_acks(monkeypatch):
    put_calls = []
    posted = []
    monkeypatch.setattr(
        "adapters.install_store.get_learning_by_source_comment",
        lambda repo, cid: None,
    )
    monkeypatch.setattr(
        "adapters.install_store.get_comment_record",
        lambda iid, cid: {
            "finding_text": "consider nested try/except",
            "finding_tags": {"rule_name": "error-handling"},
            "author_login": "dev",
        },
    )
    monkeypatch.setattr(
        "adapters.install_store.put_learning",
        lambda **kw: put_calls.append(kw),
    )
    monkeypatch.setattr("adapters.install_store.claim_delivery", lambda k: True)
    monkeypatch.setattr(
        "llm_client.classify_learning",
        lambda *a, **k: {"durable": True, "learning": "prefer early returns",
                         "scope_path": "**/mw/*.py"},
    )
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body: posted.append((url, body)))
    monkeypatch.setattr(rerun, "_org_learning_approval_delay",
                        lambda install_id, repo_full: 0)

    result = rerun._run_learn(11, "o/r", 7, 5001, 4000, "we prefer early returns", "teammate")

    assert result == "learned"
    assert put_calls and put_calls[0]["text"] == "prefer early returns"
    assert put_calls[0]["author"] == "teammate"  # the teacher, not the PR author
    assert put_calls[0]["scope_path"] == "**/mw/*.py" and put_calls[0]["source_pr"] == 7
    # A reaction on the teacher's reply, plus a collapsible acknowledgment.
    assert posted and "/pulls/comments/5001/reactions" in posted[0][0]
    assert posted[0][1] == {"content": rerun.LEARN_REACTION_LEARNED}
    comments = [b["body"] for (_u, b) in posted if "/pulls/comments/5001/replies" in _u]
    assert comments and "Markings remembered" in comments[0] and "prefer early returns" in comments[0]


def test_run_learn_one_off_declines_without_storing(monkeypatch):
    put_calls = []
    posted = []
    monkeypatch.setattr(
        "adapters.install_store.get_learning_by_source_comment",
        lambda repo, cid: None,
    )
    monkeypatch.setattr(
        "adapters.install_store.get_comment_record",
        lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}},
    )
    monkeypatch.setattr("adapters.install_store.put_learning",
                        lambda **kw: put_calls.append(kw))
    monkeypatch.setattr("adapters.install_store.claim_delivery", lambda k: True)
    monkeypatch.setattr("llm_client.classify_learning",
                        lambda *a, **k: {"durable": False, "learning": "", "scope_path": ""})
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body: posted.append((url, body)))

    result = rerun._run_learn(11, "o/r", 7, 5001, 4000, "just for this PR")

    assert result == "learn_one_off"
    assert put_calls == []  # nothing stored
    assert posted and "/pulls/comments/5001/reactions" in posted[0][0]
    assert posted[0][1] == {"content": rerun.LEARN_REACTION_ONE_OFF}
    # Declining is explicit in the acknowledgment comment (not just a reaction).
    bodies = [b["body"] for (_u, b) in posted if "/pulls/comments/5001/replies" in _u]
    assert bodies and "Not remembered" in bodies[0] and "one reply only" in bodies[0]


def test_run_learn_classifier_none_raises_for_redrive(monkeypatch):
    posted = []
    monkeypatch.setattr(
        "adapters.install_store.get_learning_by_source_comment",
        lambda repo, cid: None,
    )
    monkeypatch.setattr(
        "adapters.install_store.get_comment_record",
        lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}},
    )
    monkeypatch.setattr("adapters.install_store.put_learning",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("must not store")))
    monkeypatch.setattr("adapters.install_store.claim_delivery",
                        lambda k: (_ for _ in ()).throw(AssertionError("must not claim")))
    monkeypatch.setattr("llm_client.classify_learning", lambda *a, **k: None)
    monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body: posted.append(body))

    # A transient classifier failure must redrive, NOT mislabel as one-off.
    with pytest.raises(RuntimeError, match="classifier unavailable"):
        rerun._run_learn(11, "o/r", 7, 5001, 4000, "?")
    assert posted == []  # no misleading ack posted on the failure path


def test_run_learn_every_backend_unusable_completes_without_redrive(monkeypatch, caplog):
    """2026-09-23: with the OpenRouter key over its limit, every retry of a
    learn job whose other backend also failed walked toward the rerun DLQ.
    When EVERY backend is unusable for a config/billing reason no redrive can
    succeed, so the job completes: nothing stored, and the maintainer is told
    in the thread that the reply was not judged (re-replying is the replay
    path). No win-once claim: it would swallow the retry of a failed post."""
    import llm_client
    posted = []
    monkeypatch.setattr(
        "adapters.install_store.get_learning_by_source_comment",
        lambda repo, cid: None,
    )
    monkeypatch.setattr(
        "adapters.install_store.get_comment_record",
        lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}},
    )
    monkeypatch.setattr("adapters.install_store.put_learning",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("must not store")))
    monkeypatch.setattr("adapters.install_store.claim_delivery",
                        lambda k: (_ for _ in ()).throw(AssertionError("must not claim")))
    gauges = []
    monkeypatch.setattr("observability.emit_gauge", lambda *a, **k: gauges.append(a))

    def unusable(*a, **k):
        raise llm_client.LearnClassifierUnusable(("openrouter:http_403", "poolside:http_401"))
    monkeypatch.setattr("llm_client.classify_learning", unusable)
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body: posted.append((url, body)))

    with caplog.at_level(logging.ERROR):
        result = rerun._run_learn(11, "o/r", 7, 5001, 4000, "?")

    assert result == "learn_classifier_unusable"
    assert posted and "/pulls/comments/5001/reactions" in posted[0][0]
    assert posted[0][1] == {"content": rerun.LEARN_REACTION_UNJUDGED}
    assert gauges == []  # not a classification
    unusable_logs = [r for r in caplog.records if r.msg == "learn_classifier_unusable"]
    assert unusable_logs and unusable_logs[0].statuses == ["openrouter:http_403", "poolside:http_401"]


def test_run_learn_unusable_notice_post_failure_redrives(monkeypatch):
    """The notice is the maintainer's only replay cue, so a failed post must
    raise for redrive rather than complete the job with nothing said."""
    import llm_client
    monkeypatch.setattr(
        "adapters.install_store.get_learning_by_source_comment",
        lambda repo, cid: None,
    )
    monkeypatch.setattr(
        "adapters.install_store.get_comment_record",
        lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}},
    )

    def unusable(*a, **k):
        raise llm_client.LearnClassifierUnusable(("openrouter:http_403",))
    monkeypatch.setattr("llm_client.classify_learning", unusable)
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))

    def github_down(token, url, body):
        raise httpx.ConnectError("github down")
    monkeypatch.setattr(rerun, "_gh_post", github_down)

    with pytest.raises(httpx.ConnectError):
        rerun._run_learn(11, "o/r", 7, 5001, 4000, "?")


def test_run_learn_redelivery_is_win_once(monkeypatch):
    posted = []
    monkeypatch.setattr(
        "adapters.install_store.get_learning_by_source_comment",
        lambda repo, cid: None,
    )
    monkeypatch.setattr(
        "adapters.install_store.get_comment_record",
        lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}},
    )
    monkeypatch.setattr("adapters.install_store.put_learning", lambda **kw: None)
    # claim_delivery returns False -> already processed (redelivery).
    monkeypatch.setattr("adapters.install_store.claim_delivery", lambda k: False)
    monkeypatch.setattr("llm_client.classify_learning",
                        lambda *a, **k: {"durable": True, "learning": "r", "scope_path": ""})
    monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body: posted.append(body))

    assert rerun._run_learn(11, "o/r", 7, 5001, 4000, "x") == "learn_duplicate"
    assert posted == []  # no duplicate ack on the finding thread


def test_run_learn_no_parent_record_is_silent(monkeypatch):
    posted = []
    monkeypatch.setattr("adapters.install_store.get_learning_by_source_comment", lambda repo, cid: None)
    monkeypatch.setattr("adapters.install_store.get_comment_record", lambda iid, cid: None)
    monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body: posted.append(body))

    assert rerun._run_learn(11, "o/r", 7, 5001, 4000, "reply") == "learn_no_parent"
    assert posted == []  # no noise on a reply to a non-grug comment


def test_run_one_routes_learn_kind(monkeypatch):
    called = {}
    monkeypatch.setattr(
        rerun, "_run_learn",
        lambda iid, repo, pr, cid, parent, text, author="", **_defer: called.update(
            iid=iid, repo=repo, pr=pr, cid=cid, parent=parent, text=text,
            author=author) or "learned",
    )
    body = json.dumps({
        "kind": "learn", "install_id": 11, "repo": "o/r", "pr_number": 7,
        "comment_id": 5001, "parent_comment_id": 4000, "reply_text": "hi",
        "author": "teammate",
    })
    assert rerun._run_one(body) == "learned"
    assert called["parent"] == 4000 and called["cid"] == 5001
    assert called["author"] == "teammate"



def test_enqueue_learn_dedup_id_is_bounded(monkeypatch):
    # A long owner/repo must not push the FIFO dedup id past SQS's 128 limit.
    sent = {}
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/q.fifo")
    monkeypatch.setattr(rerun._sqs, "send_message", lambda **kw: sent.update(kw))
    rerun.enqueue_learn(
        install_id=11, repo="a" * 90 + "/" + "b" * 90, pr_number=7,
        comment_id=5001, parent_comment_id=4000, reply_text="x", author="dev",
    )
    assert len(sent["MessageDeduplicationId"]) <= 128
    assert len(sent["MessageGroupId"]) <= 128


def test_run_learn_ack_failure_keeps_learning_and_does_not_raise(monkeypatch):
    put_calls = []
    monkeypatch.setattr(
        "adapters.install_store.get_learning_by_source_comment",
        lambda repo, cid: None,
    )
    monkeypatch.setattr(
        "adapters.install_store.get_comment_record",
        lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}},
    )
    monkeypatch.setattr("adapters.install_store.put_learning",
                        lambda **kw: put_calls.append(kw))
    monkeypatch.setattr("adapters.install_store.claim_delivery", lambda k: True)
    monkeypatch.setattr("llm_client.classify_learning",
                        lambda *a, **k: {"durable": True, "learning": "r", "scope_path": ""})
    # The threaded reply POST fails transiently.
    monkeypatch.setattr(rerun, "with_install_token_retry",
                        lambda iid, fn: (_ for _ in ()).throw(httpx.ConnectError("down")))

    # Learning is already stored, so a failed ack must NOT raise (which would
    # redrive + risk a different verdict) - it degrades to no courtesy reply.
    result = rerun._run_learn(11, "o/r", 7, 5001, 4000, "x", "dev")
    assert result == "learned"
    assert put_calls  # the learning WAS stored despite the ack failure


def test_run_learn_redelivery_skips_classifier_and_reuses_stored_rule(monkeypatch):
    """A redelivery whose reply already produced a stored learning must NOT
    re-run the non-deterministic classifier (a re-run could store a second,
    differently-worded row); the claim still gates the ack win-once."""
    posted = []
    monkeypatch.setattr(
        "adapters.install_store.get_comment_record",
        lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}},
    )
    monkeypatch.setattr(
        "adapters.install_store.get_learning_by_source_comment",
        lambda repo, cid: {"text": "stored rule", "scope_path": "", "source_comment_id": cid},
    )
    monkeypatch.setattr(
        "llm_client.classify_learning",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("classifier must not run")),
    )
    monkeypatch.setattr(
        "adapters.install_store.put_learning",
        lambda **kw: None,  # idempotent re-store is harmless if reached
    )
    # Ack already claimed on the first delivery -> no duplicate ack.
    monkeypatch.setattr("adapters.install_store.claim_delivery", lambda k: False)
    monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body: posted.append(body))
    monkeypatch.setattr(rerun, "_org_learning_approval_delay",
                        lambda install_id, repo_full: 0)

    assert rerun._run_learn(11, "o/r", 7, 5001, 4000, "x", "dev") == "learn_duplicate"
    assert posted == []


def test_defuse_md_breaks_mentions():
    out = rerun._defuse_md("thanks @org/team and @user")
    assert "@org" not in out  # zwsp inserted after @ breaks the mention
    assert "@​org/team" in out and "@​user" in out


def test_defuse_md_logs_when_truncated(caplog):
    """PR #679 Elder finding: a silent 600-char cap hides truncation from
    the operator - now logged."""
    import logging as _logging
    with caplog.at_level(_logging.INFO):
        out = rerun._defuse_md("x" * 1000)
    assert len(out) == rerun._DEFUSE_MD_CAP
    hits = [r for r in caplog.records if r.msg == "defuse_md_truncated"]
    assert hits and hits[0].raw_len == 1000 and hits[0].cap == rerun._DEFUSE_MD_CAP


def test_defuse_md_no_log_under_cap(caplog):
    import logging as _logging
    with caplog.at_level(_logging.INFO):
        rerun._defuse_md("short")
    assert not [r for r in caplog.records if r.msg == "defuse_md_truncated"]


# --- _run_ask mention neutralization (#561) ---------------------------

_ZWSP = chr(0x200B)  # kept as a runtime chr() call, not a literal
# character, so this source file stays 7-bit ASCII on disk.


def test_run_ask_neutralizes_mention_in_posted_answer(monkeypatch):
    """A prompt-injected diff could get the model to include a live
    @mention in its answer; posted under Grug's own installation-token
    authority, that would notify a real GitHub user as if Grug itself
    pinged them (#561, same class as Teller's #554 round-4 fix)."""
    posted = []
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda _iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_get_text", lambda _token, _url, accept=None: "diff --git a b")
    monkeypatch.setattr(rerun, "_gh_post", lambda _token, _url, body: posted.append(body))
    monkeypatch.setattr(
        "llm_client.answer_pr_question",
        lambda _q, _diff, _iid, pr_context=None: "ping @cait to review this",
    )
    monkeypatch.setattr("llm_client._redact_secrets", lambda s: s)

    result = rerun._run_ask(11, "o/r", 7, "who should review this?")

    assert result == "answered"
    assert posted, "expected a comment to be posted"
    body = posted[0]["body"]
    assert "@cait" not in body
    assert f"@{_ZWSP}cait" in body


def test_run_ask_no_answer_falls_back_without_crashing_on_none(monkeypatch):
    """answer_pr_question returning None (degraded) must not raise inside
    the mention-neutralization step - None has no mentions to break."""
    posted = []
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda _iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_get_text", lambda _token, _url, accept=None: "diff --git a b")
    monkeypatch.setattr(rerun, "_gh_post", lambda _token, _url, body: posted.append(body))
    monkeypatch.setattr(
        "llm_client.answer_pr_question",
        lambda _q, _diff, _iid, pr_context=None: None,
    )
    monkeypatch.setattr("llm_client._redact_secrets", lambda s: s)

    result = rerun._run_ask(11, "o/r", 7, "who should review this?")

    assert result == "ask_no_answer"
    assert "thinking-rock is tired" in posted[0]["body"]


# --- #716 follow-up: the staleness watch must not kill live work ------------
#
# Live on quadseven/infra#2157 (2026-08-02), a PR titled "break the packet-mode
# bootstrap deadlock with per-leg keepalives":
#
#   13:51:12  llm_staged_review_planned      cohort_count=2, 43111 chars
#   13:52:43  (PR body edited)
#   13:52:51  code_review_llm_degraded       kind=all_failed
#             error="cancelled: review input changed while the review was
#                    running; cohort skipped: review cancelled"
#   13:52:53  elder_review_stack_upserted    -> board CREATED -> email
#
# Elder threw away a running review of a hard concurrency change because the
# author edited the body, then emailed the human "eyes cloudy - read for self"
# carrying a single cyclomatic-complexity nitpick. A retry at 13:56:51
# succeeded ("no bad omens from the Cave"), but GitHub does not mail an EDIT,
# so the inbox keeps the degraded verdict forever.


def _pr(head_sha: str, body: str = "why", title: str = "t") -> dict:
    return {"head": {"sha": head_sha}, "base": {"sha": "base"},
            "title": title, "body": body}


def _run_watch_once(monkeypatch, current_pr, expected_pr):
    """Drive exactly one poll of the staleness loop and report cancellation."""
    import threading

    monkeypatch.setattr(rerun, "_STALENESS_WATCH_INTERVAL_S", 0.001)
    stop, cancel = threading.Event(), threading.Event()

    def _fetch(*_a, **_kw):
        stop.set()  # one iteration only
        return current_pr, "etag"

    monkeypatch.setattr(rerun, "_fetch_pr_if_changed", _fetch)
    rerun._review_staleness_watch_loop(
        1, "o", "r", 7,
        rerun.review_freshness_id_from_pr(expected_pr),
        str((expected_pr.get("head") or {}).get("sha") or ""),
        stop, cancel,
    )
    return cancel.is_set()


def test_staleness_watch_does_not_cancel_on_an_intent_only_edit(monkeypatch):
    """The diff is what Elder reviews. Editing the body does not change one
    byte of it, so a running review is still producing a valid answer."""
    before = _pr("aaaa1111", body="original why")
    after = _pr("aaaa1111", body="rewritten why, same code")
    assert rerun.review_freshness_id_from_pr(after) != \
        rerun.review_freshness_id_from_pr(before), "fixture must move the id"

    assert _run_watch_once(monkeypatch, after, before) is False, (
        "an intent-only edit must not cancel a running review - that is what "
        "turned infra#2157 into all_failed"
    )


def test_staleness_watch_still_cancels_on_a_new_commit(monkeypatch):
    """A real push changes the diff, so the in-flight answer IS worthless and
    should die rather than burn its full budget."""
    before = _pr("aaaa1111")
    after = _pr("bbbb2222")

    assert _run_watch_once(monkeypatch, after, before) is True


def test_staleness_watch_ignores_a_transient_fetch_failure(monkeypatch):
    """A GitHub hiccup must never cancel a genuinely current review."""
    import threading

    monkeypatch.setattr(rerun, "_STALENESS_WATCH_INTERVAL_S", 0.001)
    stop, cancel = threading.Event(), threading.Event()

    def _boom(*_a, **_kw):
        stop.set()
        raise RuntimeError("GitHub 502")

    monkeypatch.setattr(rerun, "_fetch_pr_if_changed", _boom)
    rerun._review_staleness_watch_loop(
        1, "o", "r", 7,
        rerun.review_freshness_id_from_pr(_pr("aaaa1111")),
        "aaaa1111", stop, cancel,
    )
    assert cancel.is_set() is False


def _watch_github(monkeypatch, answers):
    """Run the real staleness loop against scripted GitHub answers and return
    each request's If-None-Match plus whether the review was cancelled."""
    import threading

    monkeypatch.setattr(rerun, "_STALENESS_WATCH_INTERVAL_S", 0.001)
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda _i, fn: fn("tok"))
    stop, cancel = threading.Event(), threading.Event()
    sent, pending = [], list(answers)

    def _get(_url, headers=None, timeout=None):
        sent.append(headers.get("If-None-Match", ""))
        if len(pending) == 1:
            stop.set()
        return pending.pop(0)

    monkeypatch.setattr(rerun.httpx, "get", _get)
    rerun._review_staleness_watch_loop(
        1, "o", "r", 7, "freshness", "aaaa1111", stop, cancel,
    )
    return sent, cancel.is_set()


def _pr_answer(head_sha, etag):
    request = httpx.Request("GET", "https://api.github.com/repos/o/r/pulls/7")
    return httpx.Response(
        200, json=_pr(head_sha), headers={"ETag": etag}, request=request,
    )


def test_staleness_watch_pays_once_for_an_unchanged_pull_request(monkeypatch):
    """Only the first poll is a full read. Every later one sends the ETag and
    a 304 keeps the review running, which costs no rate limit."""
    request = httpx.Request("GET", "https://api.github.com/repos/o/r/pulls/7")
    unchanged = [httpx.Response(304, request=request) for _ in range(3)]
    sent, cancelled = _watch_github(
        monkeypatch, [_pr_answer("aaaa1111", 'W/"one"'), *unchanged],
    )
    assert sent == ["", 'W/"one"', 'W/"one"', 'W/"one"']
    assert cancelled is False


def test_staleness_watch_still_cancels_when_the_etag_moves(monkeypatch):
    sent, cancelled = _watch_github(monkeypatch, [
        _pr_answer("aaaa1111", 'W/"one"'),
        _pr_answer("bbbb2222", 'W/"two"'),
    ])
    assert sent == ["", 'W/"one"']
    assert cancelled is True


def test_diff_too_large_completes_instead_of_redriving(monkeypatch):
    """A diff past GitHub's API size limit used to raise "Elder review degraded" for SQS redrive, and the
    identical answer took the job through all five receives into the DLQ.
    The dispatch already published a terminal neutral check naming the
    cause, so the lane must complete the claim and leave that check alone."""
    from personas.code_reviewer.dispatch import DIFF_TOO_LARGE

    pr = _pr_data(head_sha="fresh-head")
    fetches = iter((pr, pr))

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    posted: list = []
    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: posted.append(result) or {"id": 1},
    )
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    acquire, complete, release = _patch_hot_claims(monkeypatch)
    monkeypatch.setattr(
        rerun,
        "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": DIFF_TOO_LARGE,
        }),
    )

    status = rerun._run_one(_job(kind="review", settle_seconds=0))

    assert status == "dispatched"
    assert posted == []
    complete.assert_called_once()
    release.assert_not_called()


# --- free-tier outage defers a learn job (operator decision 2026-09-23) ------


def _learn_unavailable_fixture(monkeypatch):
    """Every classifier backend refusing for a retry-later reason."""
    import llm_client
    monkeypatch.setattr(
        "adapters.install_store.get_learning_by_source_comment",
        lambda repo, cid: None,
    )
    monkeypatch.setattr(
        "adapters.install_store.get_comment_record",
        lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}},
    )
    monkeypatch.setattr("adapters.install_store.put_learning",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("must not store")))
    monkeypatch.setattr("adapters.install_store.claim_delivery",
                        lambda k: (_ for _ in ()).throw(AssertionError("must not claim")))

    def unavailable(*a, **k):
        raise llm_client.LearnClassifierUnavailable(
            ("openrouter:http_403", "poolside:http_429"))
    monkeypatch.setattr("llm_client.classify_learning", unavailable)
    posted = []
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body: posted.append((url, body)))
    sent = []
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/q.fifo")
    monkeypatch.setattr(rerun._sqs, "send_message", lambda **kw: sent.append(kw))
    monkeypatch.setattr(rerun, "_now", lambda: 1_000_000.0)
    return posted, sent


def test_run_learn_all_backends_rate_limited_defers_instead_of_completing(monkeypatch):
    """The free-tier key refuses sometimes. That must neither drop the reply
    (complete as unusable) nor walk it into the DLQ (raise): the job is
    re-enqueued as a fresh message with a delay, and the current one ends."""
    posted, sent = _learn_unavailable_fixture(monkeypatch)

    result = rerun._run_learn(11, "o/r", 7, 5001, 4000, "a rule", "teammate")

    assert result == "learn_deferred"
    assert posted == []  # nothing said in the thread yet: it WILL be judged
    assert len(sent) == 1
    job = json.loads(sent[0]["MessageBody"])
    assert job["kind"] == "learn" and job["comment_id"] == 5001
    assert job["reply_text"] == "a rule" and job["author"] == "teammate"
    assert job["defer_count"] == 1
    assert job["not_before"] == 1_000_000.0 + rerun._LEARN_DEFER_BASE_SECONDS
    assert job["first_deferred_at"] == 1_000_000.0
    # A new dedup id per deferral, or SQS would collapse it into the original.
    first_dedup = sent[0]["MessageDeduplicationId"]
    rerun.enqueue_learn(install_id=11, repo="o/r", pr_number=7, comment_id=5001,
                        parent_comment_id=4000, reply_text="a rule")
    assert sent[1]["MessageDeduplicationId"] != first_dedup


def test_learn_defer_backoff_grows_and_is_bounded():
    delays = [rerun._learn_defer_delay(n) for n in range(12)]
    assert delays[0] == rerun._LEARN_DEFER_BASE_SECONDS
    assert delays == sorted(delays)
    assert max(delays) == rerun._LEARN_DEFER_MAX_SECONDS
    # SQS caps a visibility timeout at 12h; the delay must fit in one hide.
    assert rerun._LEARN_DEFER_MAX_SECONDS < 12 * 3600


def test_run_learn_deferral_carries_the_first_deferral_time(monkeypatch):
    _posted, sent = _learn_unavailable_fixture(monkeypatch)
    rerun._run_learn(11, "o/r", 7, 5001, 4000, "?", defer_count=3,
                     first_deferred_at=900_000.0)
    job = json.loads(sent[0]["MessageBody"])
    assert job["defer_count"] == 4
    assert job["first_deferred_at"] == 900_000.0
    assert job["not_before"] == 1_000_000.0 + rerun._learn_defer_delay(3)


def test_run_learn_deferral_past_the_horizon_tells_the_thread(monkeypatch):
    """Bounded: after the defer horizon the job completes with the same
    in-thread notice as an unusable backend, so the reply is never silently
    lost and never dead-lettered."""
    posted, sent = _learn_unavailable_fixture(monkeypatch)
    started = 1_000_000.0 - rerun._LEARN_DEFER_MAX_AGE_SECONDS
    result = rerun._run_learn(11, "o/r", 7, 5001, 4000, "?", defer_count=30,
                              first_deferred_at=started)
    assert result == "learn_defer_exhausted"
    assert sent == []
    assert posted and posted[0][1] == {"content": rerun.LEARN_REACTION_UNJUDGED}


def test_run_learn_deferral_enqueue_failure_redrives(monkeypatch):
    # If the deferred copy cannot be sent, the current message must survive.
    _learn_unavailable_fixture(monkeypatch)

    def sqs_down(**kw):
        raise RuntimeError("sqs down")
    monkeypatch.setattr(rerun._sqs, "send_message", sqs_down)
    with pytest.raises(RuntimeError, match="sqs down"):
        rerun._run_learn(11, "o/r", 7, 5001, 4000, "?")


def test_run_one_learn_not_yet_due_asks_the_consumer_to_hide_it(monkeypatch):
    from rerun_queue import JobNotDue
    monkeypatch.setattr(rerun, "_now", lambda: 1_000.0)
    monkeypatch.setattr(rerun, "_run_learn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("too early")))
    body = json.dumps({
        "kind": "learn", "install_id": 11, "repo": "o/r", "pr_number": 7,
        "comment_id": 5001, "parent_comment_id": 4000, "reply_text": "hi",
        "defer_count": 1, "not_before": 1_900.0, "first_deferred_at": 100.0,
    })
    with pytest.raises(JobNotDue) as info:
        rerun._run_one(body)
    assert info.value.delay_seconds == 900.0


def test_run_one_learn_due_passes_the_deferral_state(monkeypatch):
    seen = {}
    monkeypatch.setattr(rerun, "_now", lambda: 5_000.0)
    monkeypatch.setattr(rerun, "_run_learn", lambda *a, **k: seen.update(k) or "learned")
    body = json.dumps({
        "kind": "learn", "install_id": 11, "repo": "o/r", "pr_number": 7,
        "comment_id": 5001, "parent_comment_id": 4000, "reply_text": "hi",
        "defer_count": 2, "not_before": 4_000.0, "first_deferred_at": 100.0,
    })
    assert rerun._run_one(body) == "learned"
    assert seen["defer_count"] == 2 and seen["first_deferred_at"] == 100.0


def test_not_due_learn_seen_again_is_replaced_by_a_fresh_copy(monkeypatch):
    """Codex adversarial review: if the consumer's visibility hide fails, the
    not-due copy comes back every 15 minutes and each receive counts toward
    maxReceiveCount, so a visibility-API outage would dead-letter the reply.
    A repeat early arrival is replaced by a fresh copy (fresh receive count)
    and the current one completes."""
    sent = []
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/q.fifo")
    monkeypatch.setattr(rerun._sqs, "send_message", lambda **kw: sent.append(kw))
    monkeypatch.setattr(rerun, "_now", lambda: 1_000.0)
    monkeypatch.setattr(rerun, "_run_learn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("too early")))
    job = {
        "kind": "learn", "install_id": 11, "repo": "o/r", "pr_number": 7,
        "comment_id": 5001, "parent_comment_id": 4000, "reply_text": "hi",
        "author": "dev", "defer_count": 2, "not_before": 9_000.0,
        "first_deferred_at": 100.0,
    }
    event = {"Records": [{
        "body": json.dumps(job), "messageId": "m-9",
        "attributes": {"ApproximateReceiveCount": "2"},
    }]}
    rerun.handle_rerun_jobs(event)
    assert len(sent) == 1
    copy = json.loads(sent[0]["MessageBody"])
    for key in ("comment_id", "reply_text", "author", "defer_count",
                "not_before", "first_deferred_at"):
        assert copy[key] == job[key], key
    # Distinct from the copy it replaces, or SQS dedup would swallow it.
    rerun.enqueue_learn(install_id=11, repo="o/r", pr_number=7, comment_id=5001,
                        parent_comment_id=4000, reply_text="hi", defer_count=2,
                        not_before=9_000.0, first_deferred_at=100.0)
    assert sent[1]["MessageDeduplicationId"] != sent[0]["MessageDeduplicationId"]


def test_not_due_learn_first_receive_is_hidden_not_copied(monkeypatch):
    from rerun_queue import JobNotDue
    monkeypatch.setattr(rerun, "_now", lambda: 1_000.0)
    monkeypatch.setattr(rerun._sqs, "send_message",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("no copy")))
    job = {"kind": "learn", "install_id": 11, "repo": "o/r", "pr_number": 7,
           "comment_id": 5001, "defer_count": 1, "not_before": 2_000.0}
    event = {"Records": [{"body": json.dumps(job), "messageId": "m-9",
                          "attributes": {"ApproximateReceiveCount": "1"}}]}
    with pytest.raises(JobNotDue):
        rerun.handle_rerun_jobs(event)


def test_board_note_context_sets_and_clears(monkeypatch):
    """#819: the re-review note is set on entry and cleared on every exit."""
    calls = []
    monkeypatch.setattr(
        rerun,
        "_set_board_in_progress_note",
        lambda *a: calls.append("set") or "### saved",
    )
    monkeypatch.setattr(
        rerun,
        "_clear_board_in_progress_note",
        lambda *a: calls.append("clear"),
    )
    monkeypatch.setattr(
        rerun,
        "_register_board_note",
        lambda *a: calls.append("reg") or "tok",
    )
    monkeypatch.setattr(
        rerun,
        "_unregister_board_note",
        lambda t: calls.append("unreg"),
    )
    with rerun._board_in_progress_note(11, "o", "r", 7):
        calls.append("body")
    assert calls == ["set", "reg", "body", "unreg", "clear"]


def test_board_note_context_clears_on_exception(monkeypatch):
    """#819 requirement 2: a dying review still clears the note (the DLQ /
    redrive paths raise through here)."""
    calls = []
    monkeypatch.setattr(
        rerun,
        "_set_board_in_progress_note",
        lambda *a: "### saved",
    )
    monkeypatch.setattr(
        rerun,
        "_clear_board_in_progress_note",
        lambda *a: calls.append("clear"),
    )
    monkeypatch.setattr(rerun, "_register_board_note", lambda *a: "tok")
    monkeypatch.setattr(rerun, "_unregister_board_note", lambda t: None)
    with pytest.raises(RuntimeError, match="boom"), \
            rerun._board_in_progress_note(11, "o", "r", 7):
        raise RuntimeError("boom")
    assert calls == ["clear"]


def test_board_note_context_no_board_no_ops(monkeypatch):
    """No board on the PR: nothing registered, nothing cleared, no error."""
    calls = []
    monkeypatch.setattr(rerun, "_set_board_in_progress_note", lambda *a: None)
    monkeypatch.setattr(
        rerun,
        "_register_board_note",
        lambda *a: calls.append("reg") or "tok",
    )
    monkeypatch.setattr(
        rerun,
        "_clear_board_in_progress_note",
        lambda *a: calls.append("clear"),
    )
    with rerun._board_in_progress_note(11, "o", "r", 7):
        pass
    assert calls == []


def test_set_board_note_uses_app_token_and_app_id(monkeypatch):
    """The note write goes through the install token and passes app_id so a
    human's marker-quoting comment is never selected."""
    seen = {}

    def _fake_retry(install_id, fn):
        seen["install_id"] = install_id
        return fn("fake-token")

    def _fake_mark(token, owner, repo, pr_number, *, app_id):
        seen.update(
            token=token, owner=owner, repo=repo,
            pr_number=pr_number, app_id=app_id,
        )
        return "### saved"

    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_retry)
    monkeypatch.setattr(rerun, "get_app_id", lambda: "123")
    monkeypatch.setattr(
        rerun.board_client, "mark_board_in_progress", _fake_mark,
    )
    saved = rerun._set_board_in_progress_note(11, "o", "r", 7)
    assert saved == "### saved"
    assert seen == {
        "install_id": 11, "token": "fake-token", "owner": "o", "repo": "r",
        "pr_number": 7, "app_id": "123",
    }


def test_shutdown_sweep_clears_registered_board_notes(monkeypatch):
    """#819 consumer SIGTERM: notes still registered belong to reviews that
    will not finish in this process - the sweep restores their headers."""
    calls = []
    monkeypatch.setattr(
        rerun,
        "_clear_board_in_progress_note",
        lambda install_id, owner, repo, pr_number, saved: (
            calls.append(
                (pr_number, saved),
            )
            or True
        ),
    )
    token = rerun._register_board_note(11, "o", "r", 7, "### saved")
    assert rerun.clear_active_board_notes() == 1
    assert calls == [(7, "### saved")]
    # The sweep unregisters: a second sweep finds nothing.
    assert rerun.clear_active_board_notes() == 0
    rerun._unregister_board_note(token)  # idempotent, no error


def test_learn_acks_post_collapsible_comment(monkeypatch):
    """#1194: every durable/one-off learn outcome posts a collapsible
    acknowledgment comment on the finding thread (plus a reaction), so the
    team sees what was or wasn't remembered."""
    for verdict, expect_summary in (
        ({"durable": True, "learning": "prefer early returns", "scope_path": "**/mw/*.py"},
         "Markings remembered"),
        ({"durable": False, "learning": "", "scope_path": ""},
         "Not remembered"),
    ):
        posted = []
        monkeypatch.setattr("adapters.install_store.get_learning_by_source_comment", lambda repo, cid: None)
        monkeypatch.setattr("adapters.install_store.get_comment_record",
                            lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}})
        monkeypatch.setattr("adapters.install_store.put_learning", lambda **kw: None)
        monkeypatch.setattr("adapters.install_store.claim_delivery", lambda k: True)
        monkeypatch.setattr("llm_client.classify_learning", lambda *a, v=verdict, **k: v)
        monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
        monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body, sink=posted: sink.append((url, body)))
        monkeypatch.setattr(rerun, "_org_learning_approval_delay", lambda install_id, repo_full: 0)
        rerun._run_learn(11, "o/r", 7, 5001, 4000, "reply")

        reactions = [b for (_u, b) in posted if _u.endswith("/reactions")]
        comments = [b["body"] for (_u, b) in posted if "/pulls/comments/5001/replies" in _u]
        assert reactions, "expected a reaction ack"
        assert comments, "expected a collapsible acknowledgment comment"
        assert expect_summary in comments[0], f"expected {expect_summary!r} in comment"


def test_learn_reply_syncs_the_findings_living_review_state(monkeypatch):
    """#1187: a maintainer reply that resolved the thread is answered by
    the in-place Resolved chip, via the learn lane, never a comment."""
    from unittest.mock import MagicMock

    synced = MagicMock(return_value=1)
    monkeypatch.setattr(rerun, "_sync_living_review", synced)
    monkeypatch.setattr("adapters.install_store.get_learning_by_source_comment", lambda repo, cid: None)
    monkeypatch.setattr("adapters.install_store.get_comment_record",
                        lambda iid, cid: {"finding_text": "x", "finding_tags": {"rule_name": "r"}})
    monkeypatch.setattr("adapters.install_store.put_learning", lambda **kw: None)
    monkeypatch.setattr("adapters.install_store.claim_delivery", lambda k: True)
    monkeypatch.setattr("llm_client.classify_learning",
                        lambda *a, **k: {"durable": False, "learning": "", "scope_path": ""})
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_post", lambda token, url, body: None)
    assert rerun._run_learn(11, "o/r", 7, 5001, 4000, "done, resolving") == "learn_one_off"
    synced.assert_called_once_with(11, "o", "r", 7)


def test_learn_reply_to_an_untracked_comment_does_not_sync(monkeypatch):
    from unittest.mock import MagicMock

    synced = MagicMock(return_value=0)
    monkeypatch.setattr(rerun, "_sync_living_review", synced)
    monkeypatch.setattr("adapters.install_store.get_comment_record", lambda iid, cid: None)
    assert rerun._run_learn(11, "o/r", 7, 5001, 4000, "reply") == "learn_no_parent"
    synced.assert_not_called()


def test_review_payload_keeps_the_head_repo_for_the_fork_check():
    """#1187: living review trusts a same-repo push to label a finding
    `fixed`; the durable payload must carry the head repo id to say so."""
    pr = {"head": {"sha": "h", "repo": {"id": 99}},
          "base": {"sha": "b", "repo": {"id": 7}}}
    payload = rerun._review_payload(
        install_id=1, owner="o", repo_name="r", pr_number=2, pr=pr,
    )
    assert payload["pull_request"]["head"] == {"sha": "h", "repo": {"id": 99}}
    deleted_fork = {"head": {"sha": "h", "repo": None}, "base": {"sha": "b", "repo": {"id": 7}}}
    assert rerun._review_payload(
        install_id=1, owner="o", repo_name="r", pr_number=2, pr=deleted_fork,
    )["pull_request"]["head"] == {"sha": "h", "repo": None}


# --- grug#1270: the `grug:reviewing` label, and the deferred re-run --------


@pytest.mark.parametrize("conclusion", ["success", "failure", "neutral"])
def test_elder_rerun_clears_the_reviewing_label_when_the_check_concludes(
    monkeypatch, conclusion, _mock_review_label_http,
):
    """The sign goes with the review: success, failure and a degraded
    neutral all END the review, so all three clear the label."""
    import review_label as rl

    monkeypatch.setattr(
        rl, "list_check_runs_for_ref",
        lambda *a, **k: [
            {"name": "Grug - Elder", "status": "completed", "conclusion": conclusion}
        ],
    )
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response()), \
         patch.object(rerun, "get_repo_config", return_value={}), \
         patch.object(rerun, "dispatch_code_review"):
        rerun.handle_rerun_jobs(_event(_job()))
    deletes = [c for c in rl.httpx.calls if c[0] == "delete"]
    assert deletes, f"the label must go once Elder concluded ({conclusion})"


def test_elder_rerun_keeps_the_label_while_the_check_is_still_running(
    monkeypatch, _mock_review_label_http,
):
    """A check that has not concluded keeps the sign - the sweep clears it
    later if the review really is over."""
    import review_label as rl

    monkeypatch.setattr(
        rl, "list_check_runs_for_ref",
        lambda *a, **k: [{"name": "Grug - Elder", "status": "in_progress"}],
    )
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response()), \
         patch.object(rerun, "get_repo_config", return_value={}), \
         patch.object(rerun, "dispatch_code_review"):
        rerun.handle_rerun_jobs(_event(_job()))
    assert [c for c in rl.httpx.calls if c[0] == "delete"] == []


def test_deferred_rerun_is_held_until_the_budget_resets(monkeypatch):
    """A command the exhausted hourly GitHub budget refused is HELD, never
    dropped: the consumer hides the message until it is due."""
    from rerun_queue import JobNotDue

    not_before = time.time() + 600
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get") as get:
        with pytest.raises(JobNotDue):
            rerun.handle_rerun_jobs(_event(_job(not_before=not_before)))
    get.assert_not_called()  # not due yet, so nothing was even fetched


def test_deferred_rerun_runs_once_it_is_due(monkeypatch):
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response()), \
         patch.object(rerun, "get_repo_config", return_value={}), \
         patch.object(rerun, "dispatch_code_review") as disp:
        out = rerun.handle_rerun_jobs(_event(_job(not_before=time.time() - 5)))
    assert out["dispatched"] == 1
    disp.assert_called_once()


def test_deferred_rerun_is_requeued_when_the_hold_did_not_take(monkeypatch):
    """A repeat early arrival means the visibility call did not take, so the
    job is replaced by a fresh copy - otherwise the message would burn its
    receive budget and dead-letter a command that was never dropped."""
    with patch.object(rerun, "enqueue_rerun") as enq:
        out = rerun._hold_or_requeue_deferred(
            {
                "install_id": 11, "repo": "myorg/myrepo", "pr_number": 7,
                "persona": "elder", "not_before": time.time() + 600,
            },
            600.0, 2, "msg-1",
        )
    assert out == "rerun_deferred_requeued"
    assert enq.call_args.kwargs["requeue_of"] == "msg-1"
    assert enq.call_args.kwargs["not_before"] > 0


def test_deferred_enqueue_gets_its_own_dedup_id(monkeypatch):
    """The plain (install, repo, pr, persona) dedup id is still inside SQS's
    5-minute window when the deferral is sent - a collapsed send would lose
    the command."""
    sent = {}
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/jobs.fifo")
    monkeypatch.setattr(
        rerun._sqs, "send_message", lambda **kwargs: sent.update(kwargs),
    )
    rerun.enqueue_rerun(
        install_id=11, repo="myorg/myrepo", pr_number=7, persona="elder",
        not_before=1791218759.0,
    )
    body = json.loads(sent["MessageBody"])
    assert body["not_before"] == 1791218759.0
    assert sent["MessageDeduplicationId"] != "11:myorg/myrepo:7:elder"
    assert len(sent["MessageDeduplicationId"]) <= 128


def test_a_plain_rerun_keeps_its_content_dedup_id(monkeypatch):
    """The deferred id must not change the ordinary one: the api producer
    sends the same plain id, and the two have to collapse onto each other."""
    sent = {}
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/jobs.fifo")
    monkeypatch.setattr(
        rerun._sqs, "send_message", lambda **kwargs: sent.update(kwargs),
    )
    rerun.enqueue_rerun(
        install_id=11, repo="myorg/myrepo", pr_number=7, persona="elder",
    )
    assert sent["MessageDeduplicationId"] == "11:myorg/myrepo:7:elder"
    assert "not_before" not in json.loads(sent["MessageBody"])


def test_the_reviewing_label_is_cleared_even_when_the_review_raises(
    monkeypatch, _mock_review_label_http,
):
    """A review that RAISED is a review that is over: the `grug:reviewing`
    sign must not stay up on a dead review, or every later reader would
    think Elder is busy (grug#1270)."""
    import review_label as rl

    monkeypatch.setattr(
        rl, "list_check_runs_for_ref",
        lambda *a, **k: [
            {"name": "Grug - Elder", "status": "completed", "conclusion": "failure"}
        ],
    )
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response()), \
         patch.object(rerun, "get_repo_config", return_value={}), \
         patch.object(rerun, "dispatch_code_review", side_effect=RuntimeError("review died")):
        with pytest.raises(RuntimeError):
            rerun.handle_rerun_jobs(_event(_job()))
    deletes = [c for c in rl.httpx.calls if c[0] == "delete"]
    assert deletes, "the sign must go even when the review raised"


def test_a_refused_label_write_is_reported_not_swallowed(monkeypatch, caplog):
    """A failed label write must not look like a sign that is up: the
    helper says so, and the log says so (grug#1270)."""
    import review_label as rl

    monkeypatch.setattr(rl, "add_reviewing_label", lambda **k: False)
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         caplog.at_level("ERROR"):
        attached = rerun._mark_reviewing_label(
            install_id=11, owner="myorg", repo_name="myrepo", pr_number=7,
        )
    assert attached is False
    assert "reviewing_label_mark_refused" in caplog.text


def test_a_failed_label_clear_is_reported_not_swallowed(monkeypatch, caplog):
    """A sign that stays up on a head Elder already finished would tell
    every later reader that Elder is busy - the refusal must be visible."""
    import review_label as rl

    monkeypatch.setattr(rl, "elder_check_conclusion", lambda **k: "success")
    monkeypatch.setattr(rl, "remove_reviewing_label", lambda **k: False)
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         caplog.at_level("ERROR"):
        cleared = rerun._clear_reviewing_label(
            install_id=11, owner="myorg", repo_name="myrepo", pr_number=7,
            head_sha="abc123",
        )
    assert cleared is False
    assert "reviewing_label_clear_refused" in caplog.text


def test_the_sign_stays_when_the_check_has_not_concluded(monkeypatch, caplog):
    """A check that is still running keeps the sign, and nothing is
    reported as a failure: the review is not over."""
    import review_label as rl

    monkeypatch.setattr(rl, "elder_check_conclusion", lambda **k: "")
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         caplog.at_level("ERROR"):
        cleared = rerun._clear_reviewing_label(
            install_id=11, owner="myorg", repo_name="myrepo", pr_number=7,
            head_sha="abc123",
        )
    assert cleared is False
    assert "reviewing_label_clear_refused" not in caplog.text


# --- grug#1430: comment-triggered runs are queued per PR, never dropped -----


def _command_job(**over) -> str:
    base = {"persona": "code_reviewer", "comment_id": 901, "queued_at": 1_000.0}
    base.update(over)
    return _job(**base)


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _elder_run(age_s: float, *, now: float = 1_100.0, status="in_progress") -> dict:
    """An Elder check run on the head that started `age_s` seconds before
    `now`."""
    return {
        "id": 55, "name": rerun._ELDER_CHECK_NAME, "status": status,
        "head_sha": "abc123", "started_at": _iso(now - age_s),
    }


def _command_fixture(
    monkeypatch, *, running=False, last_run=None, lookup_raises=False,
    run=None, replies=None, patched=None,
):
    """Stub the PR fetch, the Elder check state, the command-run store and
    Elder itself; return (dispatch, requeue, recorded).

    `running=True` is a live Elder run (a minute old); `run` is any other
    in-flight run. Replies Grug posts land in `replies`, check-run PATCHes
    in `patched`."""
    monkeypatch.setattr(rerun, "_now", lambda: 1_100.0)
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_get", lambda tok, url: _pr_data(head_sha="abc123"))
    monkeypatch.setattr(rerun, "get_repo_config", lambda iid, rid: {})
    in_flight = run if run is not None else (_elder_run(60) if running else None)
    monkeypatch.setattr(rerun, "_elder_run_in_flight", lambda *a: in_flight)
    monkeypatch.setattr(
        rerun, "_answer_command",
        lambda **k: (replies if replies is not None else []).append(k) or True,
    )
    monkeypatch.setattr(
        rerun, "patch_check_run",
        lambda tok, owner, repo, run_id, result: (
            patched if patched is not None else []
        ).append((run_id, result)),
    )
    monkeypatch.setattr(rerun, "_mark_reviewing_label", lambda **k: True)
    monkeypatch.setattr(rerun, "_clear_reviewing_label", lambda **k: True)

    def _lookup(**_k):
        if lookup_raises:
            raise RuntimeError("db down")
        return last_run

    recorded = []
    monkeypatch.setattr("adapters.install_store.get_elder_command_run", _lookup)
    monkeypatch.setattr(
        "adapters.install_store.record_elder_command_run",
        lambda **k: recorded.append(k),
    )
    dispatch = MagicMock()
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    requeue = MagicMock()
    monkeypatch.setattr(rerun, "enqueue_rerun", requeue)
    return dispatch, requeue, recorded


def test_command_jobs_get_a_dedup_id_per_comment(monkeypatch):
    """Two different comments for the same PR are two different jobs: the
    plain (install, repo, pr, persona) id would let SQS collapse the second
    into the first for 5 minutes and drop it silently."""
    sent = []
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/jobs.fifo")
    monkeypatch.setattr(rerun, "_now", lambda: 1_234.0)
    monkeypatch.setattr(rerun._sqs, "send_message", lambda **kw: sent.append(kw))
    for cid in (1, 2):
        rerun.enqueue_rerun(
            install_id=11, repo="myorg/myrepo", pr_number=7,
            persona="code_reviewer", comment_id=cid,
        )
    ids = [m["MessageDeduplicationId"] for m in sent]
    assert ids[0] != ids[1]
    assert all(i != "11:myorg/myrepo:7:code_reviewer" and len(i) <= 128 for i in ids)
    # same group: commands for one PR stay in order
    assert sent[0]["MessageGroupId"] == sent[1]["MessageGroupId"]
    body = json.loads(sent[0]["MessageBody"])
    assert body["comment_id"] == 1 and body["queued_at"] == 1_234.0


def test_a_deferred_command_keeps_a_dedup_id_per_comment(monkeypatch):
    sent = []
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/jobs.fifo")
    monkeypatch.setattr(rerun._sqs, "send_message", lambda **kw: sent.append(kw))
    for cid in (1, 2):
        rerun.enqueue_rerun(
            install_id=11, repo="myorg/myrepo", pr_number=7,
            persona="code_reviewer", not_before=5_000.0, comment_id=cid,
        )
    assert sent[0]["MessageDeduplicationId"] != sent[1]["MessageDeduplicationId"]


def test_a_command_is_held_while_an_elder_review_runs_on_the_head(monkeypatch):
    """The command waits behind the review in flight: it is requeued with a
    short not_before, keeping its comment, its queued_at and the hold start,
    and Elder is not dispatched a second time."""
    dispatch, requeue, recorded = _command_fixture(monkeypatch, running=True)
    status = rerun._run_one(_command_job(), message_id="m-1")
    assert status == "rerun_held_behind_review"
    dispatch.assert_not_called()
    assert recorded == []
    kw = requeue.call_args.kwargs
    assert kw["comment_id"] == 901 and kw["queued_at"] == 1_000.0
    assert kw["hold_started_at"] == 1_100.0
    assert kw["not_before"] == 1_100.0 + rerun._COMMAND_HOLD_POLL_SECONDS
    assert kw["requeue_of"] == "m-1"
    assert kw["persona"] == "code_reviewer"


def test_a_held_command_keeps_its_first_hold_start(monkeypatch):
    _, requeue, _ = _command_fixture(monkeypatch, running=True)
    rerun._run_one(_command_job(hold_started_at=1_050.0), message_id="m-2")
    assert requeue.call_args.kwargs["hold_started_at"] == 1_050.0


def test_a_held_command_runs_once_the_review_ends(monkeypatch):
    dispatch, requeue, recorded = _command_fixture(monkeypatch, running=False)
    status = rerun._run_one(_command_job(hold_started_at=1_050.0), message_id="m-3")
    assert status == "dispatched"
    dispatch.assert_called_once()
    requeue.assert_not_called()
    assert recorded == [{
        "install_id": 11, "repo": "myorg/myrepo", "pr_number": 7,
        "head_sha": "abc123", "started_at": 1_100.0,
    }]


def test_a_held_command_is_answered_not_dropped_when_the_hold_cap_passes(monkeypatch):
    """grug#1447: past the hold cap the command is not run, and Grug says
    so on the PR with the next step - never a silent end."""
    replies = []
    dispatch, requeue, recorded = _command_fixture(
        monkeypatch, running=True, replies=replies,
    )
    started = 1_100.0 - rerun._COMMAND_HOLD_MAX_SECONDS
    status = rerun._run_one(_command_job(hold_started_at=started), message_id="m-4")
    assert status == "rerun_hold_cap_reached"
    dispatch.assert_not_called()
    requeue.assert_not_called()
    assert recorded == []
    assert len(replies) == 1
    body = replies[0]["body"]
    assert "was not run" in body
    assert "abc123"[:8] in body
    assert "/grug improve" in body and "push a new commit" in body
    assert replies[0]["pr_number"] == 7 and replies[0]["owner"] == "myorg"
    assert body.isascii()


def test_a_cap_reply_that_does_not_land_raises_so_the_command_is_redelivered(monkeypatch):
    """A cap answer GitHub refused must not end the command silently: it
    raises, SQS redelivers, and the answer is tried again."""
    dispatch, _, _ = _command_fixture(monkeypatch, running=True)
    monkeypatch.setattr(rerun, "_answer_command", lambda **k: False)
    started = 1_100.0 - rerun._COMMAND_HOLD_MAX_SECONDS
    with pytest.raises(RuntimeError):
        rerun._run_one(_command_job(hold_started_at=started), message_id="m-4b")
    dispatch.assert_not_called()


def test_the_hold_cap_is_the_stuck_run_cutoff():
    """grug#1447: the hold lasts as long as a run may live before it counts
    as abandoned, so one orphaned run always ends by the staleness rule
    before the cap - the cap comes from the existing sweeper config, not a
    second number."""
    import check_run_reconciler

    cutoff = check_run_reconciler._STUCK_IN_PROGRESS_MAX_AGE_MINUTES * 60
    assert rerun._ELDER_RUN_ABANDONED_SECONDS == cutoff
    assert rerun._COMMAND_HOLD_MAX_SECONDS == cutoff


def test_a_command_behind_an_abandoned_elder_run_runs_a_fresh_review(monkeypatch):
    """grug#1447: an Elder run in progress past the stuck-run cutoff has no
    worker. The command does not wait on it: the stale run is closed as
    abandoned and superseded, Grug says so, and a fresh review runs now."""
    replies, patched = [], []
    stale = _elder_run(rerun._ELDER_RUN_ABANDONED_SECONDS + 5)
    dispatch, requeue, recorded = _command_fixture(
        monkeypatch, run=stale, replies=replies, patched=patched,
    )
    status = rerun._run_one(_command_job(), message_id="m-10")
    assert status == "dispatched"
    dispatch.assert_called_once()
    requeue.assert_not_called()
    assert len(recorded) == 1
    assert len(patched) == 1
    run_id, result = patched[0]
    assert run_id == 55
    assert result.status == "completed"
    assert result.conclusion == "failure"
    assert result.name == stale["name"] and result.head_sha == "abc123"
    assert "abandoned" in result.title.lower()
    assert "superseded" in result.summary and result.summary.isascii()
    assert len(replies) == 1
    assert "fresh review" in replies[0]["body"] and replies[0]["body"].isascii()


def test_an_abandoned_run_that_cannot_be_closed_still_gets_a_fresh_review(monkeypatch):
    """Closing the stale check is best-effort: the fresh review's own check
    replaces it either way, so a failed PATCH must not hold the command."""
    dispatch, requeue, _ = _command_fixture(
        monkeypatch, run=_elder_run(rerun._ELDER_RUN_ABANDONED_SECONDS + 5),
    )

    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(rerun, "patch_check_run", _boom)
    assert rerun._run_one(_command_job(), message_id="m-11") == "dispatched"
    dispatch.assert_called_once()
    requeue.assert_not_called()


def test_an_abandoned_run_with_no_id_is_not_patched_but_still_superseded(monkeypatch):
    """A listing row with no id cannot be closed; the command still runs a
    fresh review and Grug still says why."""
    replies, patched = [], []
    run = _elder_run(rerun._ELDER_RUN_ABANDONED_SECONDS + 5)
    del run["id"]
    dispatch, _, _ = _command_fixture(
        monkeypatch, run=run, replies=replies, patched=patched,
    )
    assert rerun._run_one(_command_job(), message_id="m-13") == "dispatched"
    dispatch.assert_called_once()
    assert patched == []
    assert len(replies) == 1


def test_a_failed_supersede_reply_is_logged_and_the_review_still_runs(monkeypatch, caplog):
    dispatch, _, _ = _command_fixture(
        monkeypatch, run=_elder_run(rerun._ELDER_RUN_ABANDONED_SECONDS + 5),
    )
    monkeypatch.setattr(rerun, "_answer_command", lambda **k: False)
    with caplog.at_level(logging.WARNING):
        assert rerun._run_one(_command_job(), message_id="m-14") == "dispatched"
    dispatch.assert_called_once()
    assert "rerun_command_abandoned_reply_failed" in caplog.text


@pytest.mark.parametrize("run", [
    _elder_run(rerun._ELDER_RUN_ABANDONED_SECONDS),  # exactly at the cutoff
    _elder_run(60),                                  # a live review
    _elder_run(0, status="queued"),                  # queued, not started
    {"id": 55, "name": "x", "status": "in_progress"},  # no started_at
])
def test_a_command_still_holds_behind_a_run_that_is_not_abandoned(monkeypatch, run):
    patched = []
    dispatch, requeue, _ = _command_fixture(monkeypatch, run=run, patched=patched)
    assert rerun._run_one(_command_job(), message_id="m-12") == "rerun_held_behind_review"
    dispatch.assert_not_called()
    requeue.assert_called_once()
    assert patched == []


def test_elder_run_in_flight_returns_the_running_elder_run(monkeypatch):
    runs = [
        {"id": 1, "name": "Some other check", "status": "in_progress"},
        {"id": 2, "name": rerun._ELDER_CHECK_NAME, "status": "completed"},
        {"id": 3, "name": rerun._ELDER_CHECK_NAME, "status": "in_progress"},
    ]
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "list_check_runs_for_ref", lambda *a: runs)
    assert rerun._elder_run_in_flight(11, "o", "r", "abc")["id"] == 3
    monkeypatch.setattr(rerun, "list_check_runs_for_ref", lambda *a: runs[:2])
    assert rerun._elder_run_in_flight(11, "o", "r", "abc") is None


def test_a_command_served_by_a_later_command_run_is_coalesced(monkeypatch):
    """A command-triggered run of this same head started after the command
    was queued: it already read everything, so the command completes as
    coalesced without a second review."""
    dispatch, requeue, _ = _command_fixture(
        monkeypatch, running=True,
        last_run={"head_sha": "abc123", "started_at": 1_005.0},
    )
    assert rerun._run_one(_command_job(), message_id="m-5") == "rerun_coalesced"
    dispatch.assert_not_called()
    requeue.assert_not_called()


@pytest.mark.parametrize("last_run", [
    {"head_sha": "abc123", "started_at": 999.0},   # started before the command
    {"head_sha": "other", "started_at": 1_005.0},  # a different head
    None,
])
def test_a_command_not_served_by_an_earlier_run_still_runs(monkeypatch, last_run):
    dispatch, _, _ = _command_fixture(monkeypatch, last_run=last_run)
    assert rerun._run_one(_command_job(), message_id="m-6") == "dispatched"
    dispatch.assert_called_once()


def test_a_command_run_lookup_failure_runs_the_review(monkeypatch):
    """A store error is not "served": a repeat review beats a lost one."""
    dispatch, _, _ = _command_fixture(monkeypatch, lookup_raises=True)
    assert rerun._run_one(_command_job(), message_id="m-7") == "dispatched"
    dispatch.assert_called_once()


def test_a_failed_hold_requeue_raises_so_the_command_is_redelivered(monkeypatch):
    """A hold that cannot requeue must not drop the command: it raises, and
    SQS redelivers the original message."""
    _, requeue, _ = _command_fixture(monkeypatch, running=True)
    requeue.side_effect = RuntimeError("queue down")
    with pytest.raises(RuntimeError):
        rerun._run_one(_command_job(), message_id="m-8")


def test_a_plain_rerun_is_neither_held_nor_recorded(monkeypatch):
    """Self-recovery and api reruns carry no comment: they keep today's
    behaviour and never touch the command gate."""
    dispatch, requeue, recorded = _command_fixture(monkeypatch, running=True)
    status = rerun._run_one(_job(persona="code_reviewer"), message_id="m-9")
    assert status == "dispatched"
    dispatch.assert_called_once()
    requeue.assert_not_called()
    assert recorded == []


def test_a_requeued_deferred_command_keeps_its_identity():
    with patch.object(rerun, "enqueue_rerun") as enq:
        rerun._hold_or_requeue_deferred(
            {
                "install_id": 11, "repo": "myorg/myrepo", "pr_number": 7,
                "persona": "code_reviewer", "not_before": 9_000.0,
                "comment_id": 901, "queued_at": 1_000.0,
            },
            600.0, 2, "msg-1",
        )
    kw = enq.call_args.kwargs
    assert kw["comment_id"] == 901 and kw["queued_at"] == 1_000.0


def test_elder_running_on_reads_an_unreadable_listing_as_not_running(monkeypatch):
    def _boom(iid, fn):
        raise RuntimeError("token mint failed")

    monkeypatch.setattr(rerun, "with_install_token_retry", _boom)
    assert rerun._elder_run_in_flight(11, "o", "r", "abc") is None



# --- #822: ask in a review thread ------------------------------------------

def test_enqueue_ask_carries_reply_target(monkeypatch):
    sent = {}
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/review.fifo")
    monkeypatch.setattr(
        rerun._sqs, "send_message", lambda **kwargs: sent.update(kwargs),
    )
    rerun.enqueue_ask(
        install_id=11, repo="o/r", pr_number=7, comment_id=5001,
        question="why did you flag this?", reply_to_comment_id=4000,
    )
    job = json.loads(sent["MessageBody"])
    assert job["kind"] == "ask" and job["reply_to_comment_id"] == 4000


def test_run_ask_in_thread_replies_in_thread_with_hunk_context(monkeypatch):
    """The answer posts to the review-thread replies endpoint, and the
    model sees the thread's finding + diff hunk, not just the PR diff."""
    posted = []
    seen = {}
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda _iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_get_text", lambda _t, _u, accept=None: "diff --git a/x.py b/x.py")
    monkeypatch.setattr(rerun, "_gh_get", lambda _t, url: {
        "body": "Grug finding: this lock is held over a network call",
        "diff_hunk": "@@ -10,3 +10,4 @@ def f():\n lock.acquire()",
        "path": "x.py", "line": 12,
    })
    monkeypatch.setattr(rerun, "_gh_post", lambda _t, url, body: posted.append((url, body)))

    def _answer(q, diff, _iid, pr_context=None):
        seen["diff"] = diff
        return "Because the lock is held over the call in x.py."

    monkeypatch.setattr("llm_client.answer_pr_question", _answer)
    monkeypatch.setattr("llm_client._redact_secrets", lambda s: s)

    result = rerun._run_ask(11, "o/r", 7, "why did you flag this?", reply_to_comment_id=4000)

    assert result == "answered"
    assert posted and "/pulls/7/comments/4000/replies" in posted[0][0]
    assert "lock.acquire()" in seen["diff"]  # the thread's hunk
    assert "held over a network call" in seen["diff"]  # the finding under discussion
    assert "x.py" in posted[0][1]["body"]


def test_run_ask_top_level_quotes_the_question(monkeypatch):
    """#822 criterion 1 on the conversation surface: the answer names the
    question it answers."""
    posted = []
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda _iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_get_text", lambda _t, _u, accept=None: "diff --git a b")
    monkeypatch.setattr(rerun, "_gh_post", lambda _t, url, body: posted.append((url, body)))
    monkeypatch.setattr(
        "llm_client.answer_pr_question",
        lambda _q, _d, _iid, pr_context=None: "It adds a retry.",
    )
    monkeypatch.setattr("llm_client._redact_secrets", lambda s: s)

    rerun._run_ask(11, "o/r", 7, "what does this add?")

    assert posted[0][1]["body"].startswith("> what does this add?")


def test_run_ask_thread_context_failure_is_disclosed(monkeypatch):
    """If the thread itself cannot be loaded, the answer still posts in
    the thread but says plainly it never saw the finding."""
    posted = []
    monkeypatch.setattr(rerun, "with_install_token_retry", lambda _iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_gh_get_text", lambda _t, _u, accept=None: "diff --git a b")

    def _boom(_t, _url):
        raise RuntimeError("api down")

    monkeypatch.setattr(rerun, "_gh_get", _boom)
    monkeypatch.setattr(rerun, "_gh_post", lambda _t, url, body: posted.append((url, body)))
    monkeypatch.setattr(
        "llm_client.answer_pr_question",
        lambda _q, _d, _iid, pr_context=None: "From the diff alone.",
    )
    monkeypatch.setattr("llm_client._redact_secrets", lambda s: s)

    rerun._run_ask(11, "o/r", 7, "why did you flag this?", reply_to_comment_id=4000)

    assert "/pulls/7/comments/4000/replies" in posted[0][0]
    assert "could not load the thread" in posted[0][1]["body"]
