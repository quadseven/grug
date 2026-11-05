"""grug#1275: a Grug restart neither loses nor doubles an Elder review.

Box 4: replaying an interrupted review is idempotent. A consumer killed
after Elder published (a rollout's SIGTERM, a crash before the claim
settled or the message was deleted) hands the same SQS message back, and
the redelivery must not post a second review body or a second check
conclusion on the head.

Box 5: the whole recovery flow, end to end through the real modules: an
Elder check left `in_progress` past the review deadline is found by the
reconciler, re-queued on the durable lane, consumed, and concluded with no
`/grug improve` comment.

GitHub, SQS and the claim store are in-memory fakes; the modules under
test (`rerun`, `check_run_reconciler`, `elder_review`'s claim lease) run
for real.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import check_run_reconciler as reconciler
import pytest
import rerun
from personas.code_reviewer.degraded_gate import lead_with_degraded_line
from personas.registry import PersonaSpec

OWNER, REPO, PR = "myorg", "myrepo", 7
HEAD = "head-sha-1"
ELDER = "Grug - Elder"
REVIEW_TAG = "<!-- grug:review persona=elder -->"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class FakeClock:
    """One clock for GitHub timestamps and SQS receive times."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

    def tick(self, seconds: float = 1.0) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


class FakeGitHub:
    """The slice of GitHub a review touches: check runs and review bodies."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.check_runs: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.comments: list[str] = []
        self.title = "Improve review depth"
        self.body = "Closes #6."

    def pr(self) -> dict[str, Any]:
        return {
            "number": PR,
            "title": self.title,
            "body": self.body,
            "state": "open",
            "draft": False,
            "user": {"login": "author"},
            "head": {"sha": HEAD},
            "base": {"sha": "base-sha", "repo": {"id": 222}},
        }

    # check-runs -----------------------------------------------------------
    def post_check_run(self, _token, _owner, _repo, result, external_id=None):
        stamp = _iso(self.clock.tick())
        run = {
            "id": len(self.check_runs) + 1,
            "name": result.name,
            "head_sha": result.head_sha,
            "status": result.status,
            "conclusion": result.conclusion,
            "external_id": external_id or "",
            "output": {"title": result.title, "summary": result.summary},
            "started_at": stamp,
            "completed_at": stamp if result.status == "completed" else None,
        }
        self.check_runs.append(run)
        return run

    def patch_check_run(self, _token, _owner, _repo, run_id, result):
        run = next(r for r in self.check_runs if r["id"] == run_id)
        run.update(
            status=result.status,
            conclusion=result.conclusion,
            output={"title": result.title, "summary": result.summary},
            completed_at=_iso(self.clock.tick()),
        )

    def list_check_runs_for_ref(self, _token, _owner, _repo, ref):
        """`filter=latest`: the newest run per name on the ref."""
        latest: dict[str, dict[str, Any]] = {}
        for run in self.check_runs:
            if run["head_sha"] == ref:
                latest[run["name"]] = run
        return list(latest.values())

    def elder_conclusions(self) -> list[dict[str, Any]]:
        """Completed Elder runs a review (not the sweep) concluded."""
        return [
            r for r in self.check_runs
            if r["name"] == ELDER and r["status"] == "completed"
            and r["output"]["title"] != "Grug lost track of this review"
        ]

    # reviews --------------------------------------------------------------
    def post_review(self, state: str = "COMMENTED") -> dict[str, Any]:
        review = {
            "id": 900 + len(self.reviews),
            "commit_id": HEAD,
            "state": state,
            "body": f"**Grug Elder** gaze upon your PR\n\n{REVIEW_TAG}",
            "submitted_at": _iso(self.clock.tick()),
            "html_url": f"https://github.example/pr/{PR}#review",
        }
        self.reviews.append(review)
        return review

    def list_reviews(self, _token, _owner, _repo, *, pull_number):
        assert pull_number == PR
        return list(self.reviews)


class FakeClaims:
    """The snapshot-claim store: acquired, completed, released."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.fail_next_complete: BaseException | None = None

    @staticmethod
    def _key(kw: dict[str, Any]) -> str:
        return f"{kw['repo']}#{kw['pr_number']}:{kw['persona']}:{kw['head_sha']}"

    def acquire(self, **kw):
        key = self._key(kw)
        held = self.state.get(key)
        if held == "completed":
            return "completed"
        if held is not None:
            return "busy"
        self.state[key] = kw["owner_token"]
        return "acquired"

    def complete(self, **kw):
        if self.fail_next_complete is not None:
            error, self.fail_next_complete = self.fail_next_complete, None
            raise error
        key = self._key(kw)
        if self.state.get(key) != kw["owner_token"]:
            return False
        self.state[key] = "completed"
        return True

    def release(self, **kw):
        key = self._key(kw)
        if self.state.get(key) != kw["owner_token"]:
            return False
        del self.state[key]
        return True


class ConsumerKilled(BaseException):
    """The pod dying mid-job. BaseException, like the SystemExit a SIGTERM
    handler raises, so no `except Exception` in the handler swallows it."""


class FakeQueue:
    """FIFO SQS as the consumer sees it: a message stays until deleted, and
    every receive bumps ApproximateReceiveCount."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.messages: list[dict[str, Any]] = []

    def send_message(self, **kw):
        self.messages.append({
            "MessageId": f"m{len(self.messages) + 1}",
            "Body": kw["MessageBody"],
            "receives": 0,
            "first_received_ms": 0,
        })
        return {"MessageId": self.messages[-1]["MessageId"]}

    def deliver(self, message: dict[str, Any]) -> str | dict[str, int]:
        """Hand `message` to the real handler. A clean return deletes it; a
        kill leaves it for redelivery (the visibility timeout)."""
        message["receives"] += 1
        if not message["first_received_ms"]:
            message["first_received_ms"] = int(self.clock.tick().timestamp() * 1000)
        else:
            self.clock.tick(30)
        record = {
            "eventSource": "aws:sqs",
            "messageId": message["MessageId"],
            "body": message["Body"],
            "attributes": {
                "ApproximateReceiveCount": str(message["receives"]),
                "ApproximateFirstReceiveTimestamp": str(message["first_received_ms"]),
            },
        }
        summary = rerun.handle_rerun_jobs({"Records": [record]})
        self.messages.remove(message)
        return summary


@pytest.fixture
def world(monkeypatch):
    clock = FakeClock()
    gh = FakeGitHub(clock)
    claims = FakeClaims()
    queue = FakeQueue(clock)

    monkeypatch.setattr(rerun, "with_install_token_retry", lambda _iid, fn: fn("tok"))
    monkeypatch.setattr(rerun, "_fetch_current_pr", lambda *_a, **_k: gh.pr())
    monkeypatch.setattr(rerun, "_gh_get", lambda _tok, _url: gh.pr())
    monkeypatch.setattr(rerun, "list_check_runs_for_ref", gh.list_check_runs_for_ref)
    monkeypatch.setattr(rerun, "post_check_run", gh.post_check_run)
    monkeypatch.setattr(rerun, "list_reviews", gh.list_reviews, raising=False)
    monkeypatch.setattr(rerun, "get_repo_config", lambda _iid, _rid: {
        "code_reviewer_blocking": True,
    })
    monkeypatch.setattr(
        rerun, "_board_in_progress_note",
        lambda *_a, **_k: contextlib.nullcontext(None),
    )
    monkeypatch.setattr(
        rerun, "start_staleness_watch",
        lambda *_a, **_k: SimpleNamespace(cancel=None),
    )
    monkeypatch.setattr(rerun, "stop_staleness_watch", lambda _watch: None)
    monkeypatch.setattr(
        rerun, "count_review_calls", lambda *_a, **_k: contextlib.nullcontext(),
    )
    monkeypatch.setattr(rerun, "_mark_reviewing_label", lambda **_k: None)
    monkeypatch.setattr(rerun, "_clear_reviewing_label", lambda **_k: None)
    monkeypatch.setattr(rerun, "_sqs", queue)
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/rerun.fifo")
    monkeypatch.setattr("adapters.install_store.acquire_review_claim", claims.acquire)
    monkeypatch.setattr("adapters.install_store.complete_review_claim", claims.complete)
    monkeypatch.setattr("adapters.install_store.release_review_claim", claims.release)
    monkeypatch.setattr("adapters.install_store.renew_review_claim", lambda **_k: True)
    return SimpleNamespace(clock=clock, gh=gh, claims=claims, queue=queue)


def _publishing_dispatch(gh: FakeGitHub, *, kill_after_review_body: bool = False):
    """A stand-in for `dispatch_code_review` that publishes like the real
    one: a review body and a concluded `Grug - Elder` check on the head.
    `kill_after_review_body` kills the consumer between the two."""
    calls: list[dict[str, Any]] = []

    def dispatch(payload, *, blocking, **_kw):
        calls.append(payload)
        head = payload["pull_request"]["head"]["sha"]
        gh.post_review("CHANGES_REQUESTED")
        if kill_after_review_body and len(calls) == 1:
            raise ConsumerKilled("SIGTERM between review body and check")
        gh.post_check_run(
            "tok", OWNER, REPO,
            rerun.CheckRunResult(
                name=ELDER, head_sha=head, status="completed",
                conclusion="failure", title="Elder found 1 issue",
                summary="1 blocking finding.",
            ),
            external_id=f"grug-cr:{OWNER}/{REPO}#{PR}:{head}",
        )
        return {"persona": "code_reviewer", "result": "fail"}

    return dispatch, calls


def _enqueue_review(world) -> dict[str, Any]:
    rerun.enqueue_review(
        install_id=11, repo=f"{OWNER}/{REPO}", pr_number=PR,
        requested_base_sha="base-sha", requested_head_sha=HEAD,
        requested_title=world.gh.title, requested_body=world.gh.body,
        settle_seconds=0,
    )
    return world.queue.messages[-1]


def test_review_killed_after_its_body_concludes_once_on_redelivery(world, monkeypatch):
    """The characterization: the review body lands, the consumer dies before
    the check concludes, and the same message is redelivered. The head ends
    with ONE review body and ONE Elder conclusion, taken from that body."""
    dispatch, calls = _publishing_dispatch(world.gh, kill_after_review_body=True)
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    message = _enqueue_review(world)

    with pytest.raises(ConsumerKilled):
        world.queue.deliver(message)
    # The killed handler never reached its own release; the shutdown sweep
    # (or the lease running out) hands the claim back.
    world.claims.state.clear()
    world.queue.deliver(message)

    assert len(calls) == 1
    assert len(world.gh.reviews) == 1
    conclusions = world.gh.elder_conclusions()
    assert len(conclusions) == 1
    assert conclusions[0]["conclusion"] == "failure"
    latest = world.gh.list_check_runs_for_ref("tok", OWNER, REPO, HEAD)
    assert [r["status"] for r in latest if r["name"] == ELDER] == ["completed"]
    assert world.queue.messages == []


def test_review_killed_before_its_claim_settles_is_not_reposted(world, monkeypatch):
    """Dispatch published both the body and the check, then the consumer
    died before marking the snapshot reviewed (the claim went back to the
    store). The redelivery settles the claim and publishes nothing."""
    dispatch, calls = _publishing_dispatch(world.gh)
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    message = _enqueue_review(world)
    world.claims.fail_next_complete = ConsumerKilled("SIGTERM before settle")

    with pytest.raises(ConsumerKilled):
        world.queue.deliver(message)
    # The killed handler never reached its own release; the shutdown sweep
    # hands the claim back so the redelivery is not bounced off "busy".
    world.claims.state.clear()
    summary = world.queue.deliver(message)

    assert len(calls) == 1
    assert len(world.gh.reviews) == 1
    assert len(world.gh.elder_conclusions()) == 1
    assert summary["skipped"] == 1
    assert "completed" in world.claims.state.values()


def test_rerun_job_killed_after_publishing_is_not_reposted(world, monkeypatch):
    """The `/grug improve` lane (kind=rerun) has no snapshot claim: a
    consumer killed after Elder published, before the message was deleted,
    must not post a second body or conclusion on the redelivery."""
    dispatch, calls = _publishing_dispatch(world.gh)
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    killed = {"done": False}

    def clear_label(**_kw):
        if not killed["done"]:
            killed["done"] = True
            raise ConsumerKilled("SIGTERM after publish")

    monkeypatch.setattr(rerun, "_clear_reviewing_label", clear_label)
    rerun.enqueue_rerun(
        install_id=11, repo=f"{OWNER}/{REPO}", pr_number=PR, persona="elder",
    )
    message = world.queue.messages[-1]

    with pytest.raises(ConsumerKilled):
        world.queue.deliver(message)
    world.queue.deliver(message)

    assert len(calls) == 1
    assert len(world.gh.reviews) == 1
    assert len(world.gh.elder_conclusions()) == 1


def test_redelivery_after_a_retried_degradation_still_reviews(world, monkeypatch):
    """A degraded check the queue retries on purpose (every model call
    failed) is not a verdict: the redelivery runs the review again."""
    calls: list[int] = []

    def dispatch(payload, *, blocking, **_kw):
        calls.append(1)
        head = payload["pull_request"]["head"]["sha"]
        if len(calls) == 1:
            world.gh.post_check_run(
                "tok", OWNER, REPO,
                rerun.CheckRunResult(
                    name=ELDER, head_sha=head, status="completed",
                    conclusion="failure", title="WARN Elder review skipped",
                    summary=lead_with_degraded_line("No model answered.", "all_failed"),
                ),
                external_id=f"grug-cr:{OWNER}/{REPO}#{PR}:{head}",
            )
            return {
                "persona": "code_reviewer", "result": "skipped",
                "degraded_reason": "all_failed",
            }
        return _publishing_dispatch(world.gh)[0](payload, blocking=blocking)

    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    message = _enqueue_review(world)

    with pytest.raises(RuntimeError, match="all_failed"):
        world.queue.deliver(message)
    world.queue.deliver(message)

    assert len(calls) == 2
    assert len(world.gh.reviews) == 1


def test_a_new_message_for_the_same_head_reviews_again(world, monkeypatch):
    """The guard is per message, not per head: an intent edit on an
    already-reviewed head enqueues a new snapshot, and that one is
    reviewed even on a redelivery."""
    dispatch, calls = _publishing_dispatch(world.gh)
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    world.queue.deliver(_enqueue_review(world))

    world.gh.body = "Closes #6. Also keep the old fallback."
    second = _enqueue_review(world)
    second["receives"] = 1  # its first delivery was lost before any publish
    second["first_received_ms"] = int(world.clock.tick().timestamp() * 1000)
    world.queue.deliver(second)

    assert len(calls) == 2


def _spec(key: str, check_run_name: str) -> PersonaSpec:
    return PersonaSpec(
        key=key, canonical=key, check_run_name=check_run_name,
        enabled_flag=f"{key}_enabled", enabled_default=True,
        blocking_flag=None, blocking_default=False,
        dispatch_style="inline", missing_repo_policy="enabled",
        events=("pull_request",),
        dispatch_module=f"personas.{key}.webhook_dispatch",
        posts_pr_check_run=True,
    )


def test_rollout_mid_review_still_concludes_elder_with_no_manual_command(world, monkeypatch):
    """Box 5, end to end. A review starts (Elder `in_progress` on the head)
    and the consumers roll, so nothing ever concludes it. Past the review
    deadline the reconciler finds it, sweeps it and re-queues it once; a
    consumer picks the job up and Elder concludes on the head. No comment
    is posted, and a second reconciler pass leaves the head alone."""
    gh = world.gh
    gh.post_check_run(
        "tok", OWNER, REPO,
        rerun.CheckRunResult(
            name=ELDER, head_sha=HEAD, status="in_progress", conclusion=None,
            title="Elder is reading the markings", summary="",
        ),
        external_id=f"grug-cr-pending:{OWNER}/{REPO}#{PR}:{HEAD}",
    )
    orphaned = gh.check_runs[-1]
    orphaned["started_at"] = _iso(
        datetime.now(timezone.utc)
        - timedelta(minutes=reconciler._STUCK_IN_PROGRESS_MAX_AGE_MINUTES + 5),
    )
    dispatch, calls = _publishing_dispatch(gh)
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    inline_dispatch = MagicMock()

    def reconcile_pass():
        with patch.object(
            reconciler, "_list_open_pull_requests", return_value=[gh.pr()],
        ), patch.object(
            reconciler, "expected_personas",
            return_value=[_spec("code_reviewer", ELDER)],
        ), patch.object(
            reconciler, "list_check_runs_for_ref", gh.list_check_runs_for_ref,
        ), patch.object(
            reconciler, "patch_check_run", gh.patch_check_run,
        ), patch("dispatcher.dispatch", inline_dispatch):
            return reconciler.reconcile_repo("tok", 11, OWNER, REPO, 222)

    assert reconcile_pass() == (1, 1)
    assert orphaned["status"] == "completed"
    assert len(world.queue.messages) == 1
    job = json.loads(world.queue.messages[0]["Body"])
    assert (job["persona"], job["pr_number"]) == ("elder", PR)
    assert "comment_id" not in job

    world.queue.deliver(world.queue.messages[0])

    assert len(calls) == 1
    latest = [
        r for r in gh.list_check_runs_for_ref("tok", OWNER, REPO, HEAD)
        if r["name"] == ELDER
    ]
    assert len(latest) == 1
    assert latest[0]["status"] == "completed"
    assert latest[0]["external_id"] == f"grug-cr:{OWNER}/{REPO}#{PR}:{HEAD}"
    assert latest[0]["conclusion"] == "failure"
    assert gh.comments == []
    inline_dispatch.assert_not_called()

    assert reconcile_pass() == (0, 0)
    assert world.queue.messages == []
