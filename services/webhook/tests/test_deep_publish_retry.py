"""Durable retry of a failed async deep-review publish (#738).

The store is faked in-process with the same contract as
adapters/pg_publish_retry_store.py (JSON round-trip, win-once enqueue,
leased claim); the real-Postgres semantics live in
services/api/tests/test_pg_stores.py. GitHub is faked at the dispatch
module's seams: no network.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import httpx
import pytest
from llm_client import Backend, FindingOrigin, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer import publish_retry
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

_HEAD = "abcd1234efgh"


class _FakeStore:
    """grug_kv's PUBLISH_RETRY rows over a dict, JSON round-tripped."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def enqueue_publish_retry(self, key, publication):
        if key in self.rows:
            return False
        self.rows[key] = {
            "state": "pending", "attempts": 0, "next_attempt_at": 0,
            "lease_owner": "", "lease_expires_at": 0, "last_error": "",
            "publication": json.loads(json.dumps(publication)),
        }
        return True

    def _due(self, row) -> bool:
        now = time.time()
        return (
            row["state"] == "pending"
            and row["next_attempt_at"] <= now
            and row["lease_expires_at"] <= now
        )

    def list_due_publish_retries(self, limit):
        return [k for k, row in self.rows.items() if self._due(row)][:limit]

    def claim_publish_retry(self, key, *, owner_token, lease_seconds):
        row = self.rows.get(key)
        if row is None or not self._due(row):
            return None
        row.update(lease_owner=owner_token, lease_expires_at=time.time() + lease_seconds)
        return json.loads(json.dumps(row))

    def complete_publish_retry(self, key, *, owner_token):
        if self.rows.get(key, {}).get("lease_owner") != owner_token:
            return False
        del self.rows[key]
        return True

    def defer_publish_retry(
        self, key, *, owner_token, attempts, next_attempt_at, last_error, abandon,
    ):
        row = self.rows.get(key)
        if row is None or row["lease_owner"] != owner_token:
            return False
        row.update(
            state="abandoned" if abandon else "pending", attempts=attempts,
            next_attempt_at=next_attempt_at, last_error=last_error,
            lease_owner="", lease_expires_at=0,
        )
        return True

    def make_due(self) -> None:
        for row in self.rows.values():
            row["next_attempt_at"] = 0


class _FakeGitHub:
    """The PR's reviews as GitHub holds them, plus the calls that reached it."""

    def __init__(self) -> None:
        self.reviews: list[dict] = []
        self.post_failures: list[Exception] = []
        self.lose_response = False
        self.head = _HEAD
        self.state = "open"
        self.stack_bodies: list[str] = []
        self.captured: list[dict] = []

    def post_review(self, token, owner, repo, *, pull_number, result):
        if self.post_failures:
            raise self.post_failures.pop(0)
        review = {"id": 900 + len(self.reviews), "body": result.body}
        self.reviews.append(review)
        if self.lose_response:
            # The write landed on GitHub; the response never came back.
            self.lose_response = False
            raise httpx.ReadTimeout("response lost")
        return review

    def list_reviews(self, token, owner, repo, *, pull_number):
        return list(self.reviews)

    def snapshot(self, token, owner, repo, pull_number):
        return ("snap", self.head, self.state, False)

    def review_comments(self, token, owner, repo, *, pull_number, review_id):
        return [{"id": review_id * 10, "path": "src/x.py", "line": 2,
                 "body": "m\n<!-- grug-rule:deep-bug -->"}]

    def upsert_stack(self, token, owner, repo, pull_number, body, *, create_if_absent):
        self.stack_bodies.append(body)


@pytest.fixture()
def store(monkeypatch):
    fake = _FakeStore()
    for name in (
        "enqueue_publish_retry", "list_due_publish_retries", "claim_publish_retry",
        "complete_publish_retry", "defer_publish_retry",
    ):
        monkeypatch.setattr(publish_retry.store, name, getattr(fake, name))
    return fake


@pytest.fixture()
def github(monkeypatch):
    fake = _FakeGitHub()
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda inst, fn: fn("tok"))
    monkeypatch.setattr(cr_dispatch, "post_review", fake.post_review)
    monkeypatch.setattr(cr_dispatch, "list_reviews", fake.list_reviews)
    monkeypatch.setattr(cr_dispatch, "_fetch_current_review_snapshot", fake.snapshot)
    monkeypatch.setattr(cr_dispatch, "get_review_comments", fake.review_comments)
    monkeypatch.setattr(cr_dispatch, "_upsert_review_stack_comment", fake.upsert_stack)
    monkeypatch.setattr(
        cr_dispatch, "put_comment_record", lambda **kw: fake.captured.append(kw),
    )
    monkeypatch.setattr(cr_dispatch, "_record_hunt_run", lambda **kw: None)
    monkeypatch.setattr(cr_dispatch, "_precedent_notes_for", lambda repo, findings: {})
    return fake


def _finding() -> Finding:
    return Finding(
        file="src/x.py", line=2, severity="high", rule_name="deep-bug",
        message="deep finding", suggestion="fix it",
        origins=(FindingOrigin(
            backend=Backend.POOLSIDE, model="laguna",
            review_span_context={"span_id": "s", "trace_id": "t"},
            evidence_paths=("src/x.py",), head_sha=_HEAD,
        ),),
    )


def _publish_deep() -> None:
    finding = _finding()
    evaluation = CodeReviewEvaluation(findings=(finding,), conclusion="failure")
    cr_dispatch._publish_deep_review(
        evaluation,
        novel_deep=(finding,), head_sha=_HEAD, event="COMMENT",
        all_prior=frozenset(), installation_id=11, owner="myorg",
        repo_name="myrepo", pull_number=7,
        deep_llm=LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
            model_name="laguna",
            review_span_context={"span_id": "deep-span", "trace_id": "deep-trace"},
        ),
        author_login="alice", mode="advisory", living_range="",
        deep_suppressed_count=0, combined_eval=evaluation,
    )


def _sweep() -> publish_retry.SweepSummary:
    return publish_retry.retry_due_publications(cr_dispatch.deliver_pending_publication)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.github.com/x")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request),
    )


def test_transient_publish_failure_stores_the_finished_review(store, github):
    github.post_failures = [httpx.ConnectError("down")]

    _publish_deep()

    assert github.reviews == []
    (key, row), = store.rows.items()
    pending = publish_retry.PendingPublication.from_record(row["publication"])
    assert pending.key == key
    assert publish_retry.PUBLISH_KEY_MARKER.format(key) in pending.result.body
    assert pending.findings == (_finding(),)
    assert pending.findings[0].origins[0].backend is Backend.POOLSIDE
    assert pending.review_span_context == {"span_id": "deep-span", "trace_id": "deep-trace"}
    assert pending.stack_body
    # The stack refresh waits for the review: none ran on the failed publish.
    assert github.stack_bodies == []


def test_permanent_rejection_is_not_queued(store, github):
    github.post_failures = [_status_error(422)]

    _publish_deep()

    assert store.rows == {}


def test_publish_failure_then_retry_succeeds(store, github):
    github.post_failures = [_status_error(502)]
    _publish_deep()

    summary = _sweep()

    assert summary.delivered == 1
    assert len(github.reviews) == 1
    assert store.rows == {}
    # Capture and stack refresh ran after the retried POST.
    assert [c["comment_id"] for c in github.captured] == [9000]
    # The finding's own origin span survived the store round-trip.
    assert github.captured[0]["review_span_context"] == {"span_id": "s", "trace_id": "t"}
    assert len(github.stack_bodies) == 1


def test_failed_retry_stays_queued_and_never_double_posts(store, github):
    github.post_failures = [httpx.ConnectError("down"), httpx.ConnectError("still down")]
    _publish_deep()

    first = _sweep()

    assert first.failed == 1
    (row,) = store.rows.values()
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > time.time()
    assert github.reviews == []
    # Not due yet: a second tick does nothing.
    assert _sweep().delivered == 0
    store.make_due()

    assert _sweep().delivered == 1
    assert len(github.reviews) == 1
    assert store.rows == {}


def test_retry_is_abandoned_after_the_attempt_cap(store, github):
    github.post_failures = [httpx.ConnectError("down")] * (publish_retry.MAX_RETRY_ATTEMPTS + 1)
    _publish_deep()

    for _ in range(publish_retry.MAX_RETRY_ATTEMPTS):
        store.make_due()
        _sweep()

    (row,) = store.rows.values()
    assert row["state"] == "abandoned"
    assert row["attempts"] == publish_retry.MAX_RETRY_ATTEMPTS
    store.make_due()
    assert _sweep().delivered == 0
    assert github.reviews == []


def test_lost_response_is_adopted_not_posted_again(store, github):
    """The original POST landed on GitHub but its response was lost: the
    retry finds the review by its key and posts nothing."""
    github.lose_response = True
    _publish_deep()
    assert len(github.reviews) == 1
    assert len(store.rows) == 1

    assert _sweep().delivered == 1

    assert len(github.reviews) == 1
    assert store.rows == {}
    assert [c["comment_id"] for c in github.captured] == [9000]


def test_duplicate_retry_attempts_post_once(store, github):
    github.post_failures = [httpx.ConnectError("down")]
    _publish_deep()
    (key,) = store.rows
    # A second enqueue of the same publication is a no-op.
    pending = publish_retry.PendingPublication.from_record(store.rows[key]["publication"])
    assert publish_retry.queue_publication(pending) is False

    # A second poller already holds the lease: this sweep must not deliver.
    store.claim_publish_retry(key, owner_token="other-pod", lease_seconds=300)
    assert _sweep().delivered == 0
    assert github.reviews == []

    # The other pod posted, then died before completing the row; once its
    # lease lapses the next sweep finds the posted review by key.
    cr_dispatch.deliver_pending_publication(pending)
    assert len(github.reviews) == 1
    store.rows[key]["lease_expires_at"] = 0

    assert _sweep().delivered == 1
    assert len(github.reviews) == 1
    assert store.rows == {}


def test_retry_dropped_when_the_head_moved(store, github):
    github.post_failures = [httpx.ConnectError("down")]
    _publish_deep()
    github.head = "newer5678"

    assert _sweep().delivered == 1

    assert github.reviews == []
    assert github.stack_bodies == []
    assert store.rows == {}


def test_unreadable_row_is_abandoned_without_delivery(store, github):
    store.enqueue_publish_retry("k", {"key": "k"})
    deliver = MagicMock()

    summary = publish_retry.retry_due_publications(deliver)

    assert summary.abandoned == 1
    deliver.assert_not_called()
    assert store.rows["k"]["state"] == "abandoned"


def test_publication_key_tracks_the_payload():
    result = cr_dispatch.ReviewResult(
        commit_id=_HEAD, event="COMMENT", body="b",
        comments=(cr_dispatch.InlineComment(path="a.py", line=1, body="c"),),
    )
    args = {"installation_id": 1, "owner": "o", "repo_name": "r", "pull_number": 2}
    key = publish_retry.publication_key(result, **args)

    assert key == publish_retry.publication_key(result, **args)
    assert key != publish_retry.publication_key(result, **{**args, "pull_number": 3})
    other = cr_dispatch.ReviewResult(
        commit_id=_HEAD, event="COMMENT", body="b2", comments=result.comments,
    )
    assert key != publish_retry.publication_key(other, **args)
