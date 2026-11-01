"""Cave Drawings (#1203) at the prompt seam: which learnings a review is
given, and that usage is counted only for the ones actually supplied."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import cave_drawings as cd
import llm_client as lc

_CTX = {"repo": "o/r", "pr_number": 7, "head_sha": "abc123", "installation_id": 1}


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _row(text, *, status=None, at=_ago(30), **kw):
    row = {"text": text, "repo": "o/r", "scope_path": "", "reinforced_at": at,
           "id": cd.learning_id(text), **kw}
    if status:
        row["status"] = status
    return row


@pytest.fixture()
def store(monkeypatch):
    """The two store calls the prompt block makes, faked: rows to list and a
    log of usage writes."""
    from adapters import pg_install_store

    state = {"rows": [], "usage": []}
    monkeypatch.setattr(pg_install_store, "list_learnings", lambda repo, limit=None: state["rows"])

    def fake_usage(repo, ids, *, review_key):
        state["usage"].append((repo, list(ids), review_key))
        return len(ids)

    monkeypatch.setattr(pg_install_store, "record_learning_usage", fake_usage)
    return state


def test_only_active_unconflicted_learnings_reach_the_prompt(store):
    older = _row("allow print in scripts", at=_ago(30))
    store["rows"] = [
        older,
        _row("pending rule", status=cd.STATUS_PENDING),
        _row("disabled rule", status=cd.STATUS_DISABLED),
        _row("superseded rule", status=cd.STATUS_SUPERSEDED),
        _row("never allow print", at=_ago(20),
             conflicts_with=[older["id"]]),
    ]
    block = lc._repo_learnings_block(_CTX)
    assert "never allow print" in block
    for absent in ("allow print in scripts", "pending rule", "disabled rule", "superseded rule"):
        assert absent not in block


def test_usage_is_recorded_for_exactly_the_supplied_learnings(store):
    store["rows"] = [_row("rule a"), _row("rule b", status=cd.STATUS_DISABLED), _row("rule c")]
    lc._repo_learnings_block(_CTX)
    assert store["usage"] == [
        ("o/r", sorted([cd.learning_id("rule a"), cd.learning_id("rule c")]), "7@abc123"),
    ]


def test_a_learning_cut_by_the_byte_bound_is_not_counted_as_used():
    rows = [_row(f"rule {i} " + "x" * 80) for i in range(10)]
    block, ids = lc._render_learnings_block_with_ids(rows, max_chars=300)
    # Newest first: rows 9, 8, 7 fit; everything older was omitted.
    for i in (9, 8, 7):
        assert f"rule {i} " in block
    assert "rule 6 " not in block
    assert ids == [rows[9]["id"], rows[8]["id"], rows[7]["id"]]


def test_no_usage_without_a_review_identity(store):
    store["rows"] = [_row("rule a")]
    block = lc._repo_learnings_block({"repo": "o/r"})
    assert "rule a" in block
    assert store["usage"] == []


def test_a_usage_write_failure_never_drops_the_block(store, monkeypatch):
    from adapters import pg_install_store

    store["rows"] = [_row("rule a")]

    def boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(pg_install_store, "record_learning_usage", boom)
    assert "rule a" in lc._repo_learnings_block(_CTX)
