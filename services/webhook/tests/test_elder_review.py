"""Elder durable-attempt lifecycle unit tests.

Covers identity, eligibility, settle, claim lease, freshness, staleness
watch, decide_redrive, and the active-claim registry — all without
mocking SQS or psycopg at depth.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import httpx
import pytest

from personas.code_reviewer.elder_review import (
    RedriveDecision,
    ReviewClaimHeartbeat,
    StalenessWatch,
    _MAX_INTENT_DRIFT_REDRIVES,
    _STALENESS_WATCH_INTERVAL_S,
    adaptive_elder_settle_seconds,
    decide_redrive,
    durable_elder_settle_seconds,
    normalize_intent_text,
    register_active_review_claim,
    release_active_review_claims,
    review_eligible,
    review_freshness_failure,
    review_freshness_id,
    review_freshness_id_from_pr,
    review_snapshot_id,
    review_snapshot_id_from_pr,
    start_review_claim_heartbeat,
    start_staleness_watch,
    stop_review_claim_heartbeat,
    stop_staleness_watch,
    unregister_active_review_claim,
)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_normalize_strips_html_comment_footers():
    raw = "## Why\n\nShip the fix.\n\n<!-- auto-generated release notes by tool\nstuff inside comment\nend of footer -->\n"
    cleaned = normalize_intent_text(raw)
    assert "Ship the fix." in cleaned
    assert "auto-generated" not in cleaned
    assert "stuff inside comment" not in cleaned


def test_normalize_collapses_blank_lines():
    assert normalize_intent_text("a\n\n\n\nb") == "a\n\nb"


def test_snapshot_stable_when_only_html_footer_changes():
    base = {
        "base_sha": "b1",
        "head_sha": "h1",
        "title": "fix: something",
        "body": "## Why\n\nReal intent.\n",
    }
    with_footer = {
        **base,
        "body": base["body"] + "\n<!-- generated block\nnoise\n-->\n",
    }
    assert review_snapshot_id(**base) == review_snapshot_id(**with_footer)


def test_snapshot_changes_when_human_intent_changes():
    a = review_snapshot_id(base_sha="b", head_sha="h", title="t", body="## Why\n\nAlpha")
    b = review_snapshot_id(base_sha="b", head_sha="h", title="t", body="## Why\n\nBeta")
    assert a != b


def test_snapshot_includes_base_sha():
    a = review_snapshot_id(base_sha="b1", head_sha="h", title="t", body="b")
    b = review_snapshot_id(base_sha="b2", head_sha="h", title="t", body="b")
    assert a != b


def test_freshness_excludes_base_sha():
    a = review_freshness_id(head_sha="h", title="t", body="b")
    b = review_freshness_id(head_sha="h", title="t", body="b", base_sha="ignored") if False else None
    # freshness_id has no base_sha param; the point is two PRs with the same
    # head/title/body but different base_sha get the same freshness_id
    pr_a = {"base": {"sha": "base_before"}, "head": {"sha": "h"}, "title": "t", "body": "b"}
    pr_b = {"base": {"sha": "base_after"}, "head": {"sha": "h"}, "title": "t", "body": "b"}
    assert review_freshness_id_from_pr(pr_a) == review_freshness_id_from_pr(pr_b)


def test_review_snapshot_id_from_pr_uses_normalized_body():
    pr = {"base": {"sha": "b"}, "head": {"sha": "h"}, "title": "t", "body": "intent\n\n<!-- footer -->\n"}
    pr2 = {"base": {"sha": "b"}, "head": {"sha": "h"}, "title": "t", "body": "intent"}
    assert review_snapshot_id_from_pr(pr) == review_snapshot_id_from_pr(pr2)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_review_eligible_open_non_draft():
    assert review_eligible({"state": "open", "draft": False}) is True


def test_review_eligible_draft_false():
    assert review_eligible({"state": "open", "draft": True}) is False


def test_review_eligible_closed_false():
    assert review_eligible({"state": "closed", "draft": False}) is False


def test_review_eligible_missing_state_false():
    assert review_eligible({}) is False


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------


def test_adaptive_settle_swift_for_tiny_pr():
    assert adaptive_elder_settle_seconds({"additions": 12, "deletions": 3, "changed_files": 2}, base_seconds=10) == 0


def test_adaptive_settle_medium_caps_at_three():
    assert adaptive_elder_settle_seconds({"additions": 100, "deletions": 40, "changed_files": 6}, base_seconds=10) == 3


def test_adaptive_settle_large_keeps_base():
    assert adaptive_elder_settle_seconds({"additions": 800, "deletions": 200, "changed_files": 40}, base_seconds=10) == 10


def test_adaptive_settle_missing_stats_keeps_base():
    assert adaptive_elder_settle_seconds({}, base_seconds=10) == 10


def test_durable_settle_clamps_to_max(monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_SETTLE_SECONDS", "999")
    pr = {"additions": 800, "deletions": 200, "changed_files": 40}
    assert durable_elder_settle_seconds(pr) == 300


def test_durable_settle_invalid_env_uses_default(monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_SETTLE_SECONDS", "not-a-number")
    pr = {"additions": 800, "deletions": 200, "changed_files": 40}
    # Invalid env falls back to default (90s base), large PR keeps base
    assert durable_elder_settle_seconds(pr) == 90


def test_durable_settle_default_env(monkeypatch):
    monkeypatch.delenv("GRUG_ELDER_SETTLE_SECONDS", raising=False)
    pr = {"additions": 12, "deletions": 3, "changed_files": 2}
    # Swift Hunt: tiny PR -> 0 settle
    assert durable_elder_settle_seconds(pr) == 0


# ---------------------------------------------------------------------------
# decide_redrive (pure, no mocks)
# ---------------------------------------------------------------------------


def test_decide_redrive_new_sha_redrives():
    decision = decide_redrive(same_sha=False, job_redrive_count=5)
    assert decision.action == "redrive"
    assert decision.next_redrive_count == 0


def test_decide_redrive_same_sha_under_cap_redrives():
    decision = decide_redrive(same_sha=True, job_redrive_count=2)
    assert decision.action == "redrive"
    assert decision.next_redrive_count == 3


def test_decide_redrive_same_sha_at_cap_gives_up():
    decision = decide_redrive(same_sha=True, job_redrive_count=_MAX_INTENT_DRIFT_REDRIVES)
    assert decision.action == "give_up"
    assert decision.next_redrive_count == _MAX_INTENT_DRIFT_REDRIVES + 1


def test_decide_redrive_new_sha_held_by_pause():
    decision = decide_redrive(same_sha=False, job_redrive_count=0, is_held_by_autopause=True)
    assert decision.action == "hold"
    assert decision.next_redrive_count == 0


def test_decide_redrive_same_sha_held_by_pause_still_gives_up():
    # AC3 takes precedence over pause for same-sha looping
    decision = decide_redrive(same_sha=True, job_redrive_count=_MAX_INTENT_DRIFT_REDRIVES, is_held_by_autopause=True)
    assert decision.action == "give_up"


# ---------------------------------------------------------------------------
# Claim lease heartbeat
# ---------------------------------------------------------------------------


def test_start_stop_heartbeat_retains_ownership():
    args = {"install_id": 1, "repo": "owner/name", "pr_number": 42}
    heartbeat = start_review_claim_heartbeat(args)
    assert isinstance(heartbeat, ReviewClaimHeartbeat)
    assert heartbeat.thread.is_alive()
    assert stop_review_claim_heartbeat(heartbeat) is True
    assert not heartbeat.thread.is_alive()


def test_stop_heartbeat_none_returns_true():
    assert stop_review_claim_heartbeat(None) is True


# ---------------------------------------------------------------------------
# Staleness watch
# ---------------------------------------------------------------------------


def test_start_stop_staleness_watch():
    args = {"install_id": 1, "repo": "owner/name", "pr_number": 42}
    watch = start_staleness_watch(
        install_id=1, owner="owner", repo_name="name", pr_number=42,
        expected_freshness_id="fresh-abc",
        expected_head_sha="head-sha",
        fetch_current_pr=lambda *a, **k: {"head": {"sha": "head-sha"}},
    )
    assert isinstance(watch, StalenessWatch)
    assert watch.thread.is_alive()
    stop_staleness_watch(watch)
    assert not watch.thread.is_alive()


def test_stop_staleness_watch_none_is_noop():
    stop_staleness_watch(None)


def test_staleness_watch_cancels_on_head_sha_change(monkeypatch):
    monkeypatch.setattr(
        "personas.code_reviewer.elder_review._STALENESS_WATCH_INTERVAL_S", 0.05,
    )
    cancel_triggered = threading.Event()

    def fetch(*a, **k):
        return {"head": {"sha": "new-sha"}}

    watch = start_staleness_watch(
        install_id=1, owner="owner", repo_name="name", pr_number=42,
        expected_freshness_id="fresh-abc",
        expected_head_sha="old-sha",
        fetch_current_pr=fetch,
    )
    # Give the watcher a tick to detect the change
    time.sleep(0.2)
    assert watch.cancel.is_set()
    stop_staleness_watch(watch)


def test_staleness_watch_transient_fetch_failure_does_not_cancel():
    calls = {"n": 0}

    def fetch(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RequestError("boom", request=httpx.Request("GET", "https://example.com"))
        return {"head": {"sha": "expected-sha"}}

    watch = start_staleness_watch(
        install_id=1, owner="owner", repo_name="name", pr_number=42,
        expected_freshness_id="fresh-abc",
        expected_head_sha="expected-sha",
        fetch_current_pr=fetch,
    )
    # Let it run a couple of ticks, then stop
    time.sleep(0.35)
    stop_staleness_watch(watch)
    assert not watch.cancel.is_set()


# ---------------------------------------------------------------------------
# Active claim registry (graceful shutdown)
# ---------------------------------------------------------------------------


def test_register_unregister_release_claims(monkeypatch):
    released = []

    def fake_release(**kwargs):
        released.append(kwargs.get("repo"))
        return True

    monkeypatch.setattr("personas.code_reviewer.elder_review._adapter_release_review_claim", fake_release)

    args_a = {"install_id": 1, "repo": "a/repo", "pr_number": 1, "owner_token": "tok-a"}
    args_b = {"install_id": 2, "repo": "b/repo", "pr_number": 2, "owner_token": "tok-b"}

    register_active_review_claim("tok-a", args_a)
    register_active_review_claim("tok-b", args_b)
    assert release_active_review_claims() == 2
    assert released == ["a/repo", "b/repo"]

    # After release, registry is empty
    assert release_active_review_claims() == 0


def test_release_active_review_claims_best_effort(monkeypatch):
    results = {}

    def fake_release(**kwargs):
        repo = kwargs.get("repo")
        if repo == "fail/repo":
            return False
        if repo == "raise/repo":
            raise RuntimeError("store down")
        results[repo] = True
        return True

    monkeypatch.setattr("personas.code_reviewer.elder_review._adapter_release_review_claim", fake_release)

    register_active_review_claim("tok-1", {"repo": "ok/repo", "pr_number": 1, "owner_token": "tok-1"})
    register_active_review_claim("tok-2", {"repo": "fail/repo", "pr_number": 2, "owner_token": "tok-2"})
    register_active_review_claim("tok-3", {"repo": "raise/repo", "pr_number": 3, "owner_token": "tok-3"})

    # Best-effort: one failure/raise doesn't stop the rest
    assert release_active_review_claims() == 1


def test_unregister_removes_from_registry():
    register_active_review_claim("tok", {"repo": "r/repo", "pr_number": 1, "owner_token": "tok"})
    unregister_active_review_claim("tok")
    assert release_active_review_claims() == 0


# ---------------------------------------------------------------------------
# review_freshness_failure (httpx + install token seam)
# ---------------------------------------------------------------------------


def test_review_freshness_failure_stale_head(monkeypatch):
    def fake_with_install_token_retry(install_id, func):
        token = "tok"
        return func(token)

    def fake_fetch(token, owner, repo, pull_number):
        return (
            "new-freshness-id",
            "new-head",
            "open",
            False,
        )

    monkeypatch.setattr("personas.code_reviewer.elder_review.with_install_token_retry", fake_with_install_token_retry)
    monkeypatch.setattr(
        "personas.code_reviewer.elder_review._fetch_current_review_snapshot",
        fake_fetch,
    )

    result = review_freshness_failure(
        installation_id=1,
        owner="owner",
        repo_name="repo",
        pull_number=42,
        expected_snapshot_id="old-snapshot",
        expected_head_sha="old-head",
    )
    assert result is not None
    assert result["degraded_reason"] == "stale_snapshot"


def test_review_freshness_failure_same_head_publishes_anyway(monkeypatch):
    def fake_with_install_token_retry(install_id, func):
        token = "tok"
        return func(token)

    def fake_fetch(token, owner, repo, pull_number):
        return (
            "different-freshness-id",
            "same-head",
            "open",
            False,
        )

    monkeypatch.setattr("personas.code_reviewer.elder_review.with_install_token_retry", fake_with_install_token_retry)
    monkeypatch.setattr(
        "personas.code_reviewer.elder_review._fetch_current_review_snapshot",
        fake_fetch,
    )

    result = review_freshness_failure(
        installation_id=1,
        owner="owner",
        repo_name="repo",
        pull_number=42,
        expected_snapshot_id="old-snapshot",
        expected_head_sha="same-head",
    )
    assert result is None  # same head -> publish anyway


def test_review_freshness_failure_ineligible(monkeypatch):
    def fake_with_install_token_retry(install_id, func):
        token = "tok"
        return func(token)

    def fake_fetch(token, owner, repo, pull_number):
        return (
            "freshness-id",
            "head",
            "closed",
            False,
        )

    monkeypatch.setattr("personas.code_reviewer.elder_review.with_install_token_retry", fake_with_install_token_retry)
    monkeypatch.setattr(
        "personas.code_reviewer.elder_review._fetch_current_review_snapshot",
        fake_fetch,
    )

    result = review_freshness_failure(
        installation_id=1,
        owner="owner",
        repo_name="repo",
        pull_number=42,
        expected_snapshot_id="snap",
        expected_head_sha="head",
    )
    assert result is not None
    assert result["degraded_reason"] == "pr_ineligible"


def test_review_freshness_failure_github_unavailable(monkeypatch):
    def fake_with_install_token_retry(install_id, func):
        token = "tok"
        raise httpx.HTTPStatusError(
            "500", request=httpx.Request("GET", "https://example.com"),
            response=httpx.Response(500),
        )

    monkeypatch.setattr("personas.code_reviewer.elder_review.with_install_token_retry", fake_with_install_token_retry)

    result = review_freshness_failure(
        installation_id=1,
        owner="owner",
        repo_name="repo",
        pull_number=42,
        expected_snapshot_id="snap",
        expected_head_sha="head",
    )
    assert result is not None
    assert result["degraded_reason"] == "freshness_check_failed"
