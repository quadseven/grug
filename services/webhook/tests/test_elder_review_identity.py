"""Elder snapshot identity + Swift Hunt settle (#Apex)."""

from __future__ import annotations

from personas.code_reviewer.elder_review import (
    adaptive_elder_settle_seconds,
    normalize_intent_text,
    review_freshness_id_from_pr,
    review_snapshot_id,
    review_snapshot_id_from_pr,
)


def test_normalize_strips_html_comment_footers():
    raw = (
        "## Why\n\nShip the fix.\n\n"
        "<!-- auto-generated release notes by tool\n"
        "stuff inside comment\n"
        "end of footer -->\n"
    )
    cleaned = normalize_intent_text(raw)
    assert "Ship the fix." in cleaned
    assert "auto-generated" not in cleaned
    assert "stuff inside comment" not in cleaned


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
    a = review_snapshot_id(
        base_sha="b", head_sha="h", title="t", body="## Why\n\nAlpha",
    )
    b = review_snapshot_id(
        base_sha="b", head_sha="h", title="t", body="## Why\n\nBeta",
    )
    assert a != b


def test_adaptive_settle_swift_for_tiny_pr():
    pr = {"additions": 12, "deletions": 3, "changed_files": 2}
    assert adaptive_elder_settle_seconds(pr, base_seconds=10) == 0


def test_adaptive_settle_medium_caps_at_three():
    pr = {"additions": 100, "deletions": 40, "changed_files": 6}
    assert adaptive_elder_settle_seconds(pr, base_seconds=10) == 3


def test_adaptive_settle_swift_boundaries():
    """At 5 files / 120 churn stay Swift (0); one past either bound is Steady."""
    assert adaptive_elder_settle_seconds(
        {"additions": 120, "deletions": 0, "changed_files": 5},
        base_seconds=10,
    ) == 0
    assert adaptive_elder_settle_seconds(
        {"additions": 121, "deletions": 0, "changed_files": 5},
        base_seconds=10,
    ) == 3
    assert adaptive_elder_settle_seconds(
        {"additions": 50, "deletions": 0, "changed_files": 6},
        base_seconds=10,
    ) == 3


def test_adaptive_settle_steady_boundaries():
    """At 12 files / 400 churn stay Steady (3); one past either bound is Full."""
    assert adaptive_elder_settle_seconds(
        {"additions": 400, "deletions": 0, "changed_files": 12},
        base_seconds=10,
    ) == 3
    assert adaptive_elder_settle_seconds(
        {"additions": 401, "deletions": 0, "changed_files": 12},
        base_seconds=10,
    ) == 10
    assert adaptive_elder_settle_seconds(
        {"additions": 100, "deletions": 0, "changed_files": 13},
        base_seconds=10,
    ) == 10


def test_adaptive_settle_large_keeps_base():
    pr = {"additions": 800, "deletions": 200, "changed_files": 40}
    assert adaptive_elder_settle_seconds(pr, base_seconds=10) == 10


def test_adaptive_settle_missing_stats_keeps_base():
    """Never invent a Swift path from absent GitHub size stats."""
    assert adaptive_elder_settle_seconds({}, base_seconds=10) == 10
    assert adaptive_elder_settle_seconds(
        {"additions": 0, "deletions": 0, "changed_files": 0},
        base_seconds=10,
    ) == 10


def test_review_snapshot_id_from_pr_uses_normalized_body():
    pr = {
        "base": {"sha": "b"},
        "head": {"sha": "h"},
        "title": "t",
        "body": "intent\n\n<!-- footer only -->",
    }
    pr2 = {
        "base": {"sha": "b"},
        "head": {"sha": "h"},
        "title": "t",
        "body": "intent",
    }
    assert review_snapshot_id_from_pr(pr) == review_snapshot_id_from_pr(pr2)


# --- Base-branch churn must not cancel in-flight reviews -------------------
# Regression tests for the Elder stall measured on quadseven/infra 2026-07-25:
# eight merges in one session left Elder unable to publish on multiple PRs,
# logging `code_review_stale_before_publish` with reviewed_head_sha ==
# current_head_sha and only the snapshot differing. Root cause: `base_sha` is
# the base-branch TIP, so every unrelated merge changed the snapshot_id of
# EVERY open PR and cancelled their in-flight reviews.


def _pr(*, base_sha: str, head_sha: str = "aaa111", title: str = "t", body: str = "b"):
    return {
        "base": {"sha": base_sha},
        "head": {"sha": head_sha},
        "title": title,
        "body": body,
    }


def test_base_branch_move_changes_snapshot_id():
    """Pins the CAUSE, so nobody 'simplifies' freshness back onto snapshot_id."""
    a = review_snapshot_id_from_pr(_pr(base_sha="base_before"))
    b = review_snapshot_id_from_pr(_pr(base_sha="base_after"))
    assert a != b, "snapshot_id is expected to include base_sha"


def test_base_branch_move_does_not_change_freshness_id():
    """The fix: an unrelated merge must NOT invalidate a review."""
    a = review_freshness_id_from_pr(_pr(base_sha="base_before"))
    b = review_freshness_id_from_pr(_pr(base_sha="base_after"))
    assert a == b, (
        "a merge to the base branch cancelled an in-flight Elder review of "
        "unchanged code - this is the stall from 2026-07-25"
    )


def test_new_commit_still_invalidates_freshness():
    """Do not fix the stall by making freshness permanently constant."""
    a = review_freshness_id_from_pr(_pr(base_sha="b", head_sha="aaa111"))
    b = review_freshness_id_from_pr(_pr(base_sha="b", head_sha="bbb222"))
    assert a != b, "a real push must still supersede an in-flight review"


def test_intent_edits_still_invalidate_freshness():
    """Title/body are the author's stated intent and still count."""
    base = _pr(base_sha="b", title="orig", body="orig body")
    assert review_freshness_id_from_pr(base) != review_freshness_id_from_pr(
        _pr(base_sha="b", title="rewritten", body="orig body")
    )
    assert review_freshness_id_from_pr(base) != review_freshness_id_from_pr(
        _pr(base_sha="b", title="orig", body="rewritten body")
    )


def test_freshness_still_ignores_bot_footers():
    """The v2 HTML-comment normalisation must survive on the freshness path."""
    a = review_freshness_id_from_pr(_pr(base_sha="b", body="## Why\n\nreal intent"))
    b = review_freshness_id_from_pr(
        _pr(base_sha="b", body="## Why\n\nreal intent\n\n<!-- bot footer -->")
    )
    assert a == b
