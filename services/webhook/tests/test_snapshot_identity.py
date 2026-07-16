"""Elder snapshot identity + Swift Hunt settle (#Apex)."""

from __future__ import annotations

from personas.code_reviewer.snapshot import (
    adaptive_elder_settle_seconds,
    normalize_intent_text,
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
