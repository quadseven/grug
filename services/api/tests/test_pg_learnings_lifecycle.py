"""Real-Postgres tests for the Cave Drawings learnings lifecycle (#1203):
inspect, edit with version history, disable, approve, delete, the
all-or-nothing import, and the per-review usage counter."""

from __future__ import annotations

import pytest

import cave_drawings as cd


@pytest.fixture()
def store(pg_store):
    from adapters import pg_install_store

    return pg_install_store


def _teach(store, text, repo="o/r", **kw):
    store.put_learning(repo=repo, text=text, **kw)
    return cd.learning_id(text)


def test_rows_carry_their_id_and_read_back_by_id(store):
    lid = _teach(store, "prefer early returns", scope_path="src/*.py", source_pr=4)
    [row] = store.list_learnings("o/r")
    assert row["id"] == lid
    got = store.get_learning("o/r", lid)
    assert got is not None and got["text"] == "prefer early returns"
    assert store.get_learning("o/r", "000000000000") is None
    assert store.list_learning_repos() == ["o/r"]


def test_disable_then_approve_records_history(store):
    lid = _teach(store, "rule")
    row = store.set_learning_status("o/r", lid, cd.STATUS_DISABLED, actor="op")
    assert row["status"] == cd.STATUS_DISABLED
    row = store.set_learning_status("o/r", lid, cd.STATUS_ACTIVE, actor="op2")
    assert row["status"] == cd.STATUS_ACTIVE
    assert row["approved_by"] == "op2" and row["approved_at"]
    assert [h["action"] for h in row["history"]] == ["disabled", "approved"]


def test_a_superseded_learning_cannot_be_reactivated(store):
    lid = _teach(store, "rule v1")
    store.edit_learning("o/r", lid, actor="op", text="rule v2")
    with pytest.raises(ValueError):
        store.set_learning_status("o/r", lid, cd.STATUS_ACTIVE, actor="op")


def test_set_status_on_a_missing_learning_raises_lookup(store):
    with pytest.raises(LookupError):
        store.set_learning_status("o/r", "000000000000", cd.STATUS_DISABLED, actor="op")


def test_reteach_never_overrides_an_operator_decision(store):
    lid = _teach(store, "rule")
    store.set_learning_status("o/r", lid, cd.STATUS_DISABLED, actor="op")
    _teach(store, "rule", source_pr=99)  # the same reply mined again
    row = store.get_learning("o/r", lid)
    assert row["status"] == cd.STATUS_DISABLED
    assert [h["action"] for h in row["history"]] == ["disabled"]
    assert row["source_pr"] == 99  # provenance still refreshes


def test_editing_the_text_supersedes_and_keeps_version_history(store):
    old = _teach(store, "rule v1", scope_path="src/*.py", source_pr=4, author="dev")
    new_row = store.edit_learning("o/r", old, actor="op", text="rule v2")
    new = new_row["id"]
    assert new != old
    assert new_row["supersedes"] == old and new_row["version"] == 2
    assert new_row["status"] == cd.STATUS_ACTIVE
    # Provenance and scope carry over to the successor.
    assert new_row["source_pr"] == 4 and new_row["scope_path"] == "src/*.py"
    assert new_row["history"][-1]["previous_text"] == "rule v1"
    old_row = store.get_learning("o/r", old)
    assert old_row["status"] == cd.STATUS_SUPERSEDED
    assert old_row["superseded_by"] == new
    # Only the successor reaches a prompt.
    sel = cd.select_for_prompt(store.list_learnings("o/r"))
    assert [r["text"] for r in sel.selected] == ["rule v2"]


def test_editing_into_an_existing_rule_is_refused(store):
    a = _teach(store, "rule a")
    _teach(store, "rule b")
    with pytest.raises(ValueError):
        store.edit_learning("o/r", a, actor="op", text="rule b")
    assert cd.effective_status(store.get_learning("o/r", a)) == cd.STATUS_ACTIVE


def test_edit_scope_and_conflicts_in_place(store):
    a = _teach(store, "rule a")
    b = _teach(store, "rule b")
    row = store.edit_learning("o/r", a, actor="op", scope_path="docs/*.md", conflicts_with=[b])
    assert row["id"] == a
    assert row["scope_path"] == "docs/*.md" and row["conflicts_with"] == [b]
    assert row["history"][-1]["action"] == "edited"
    with pytest.raises(ValueError):
        store.edit_learning("o/r", a, actor="op", scope_path="../escape")
    with pytest.raises(ValueError):
        store.edit_learning("o/r", a, actor="op", conflicts_with=["000000000000"])


def test_delete_learning(store):
    lid = _teach(store, "rule")
    assert store.delete_learning("o/r", lid) is True
    assert store.get_learning("o/r", lid) is None
    assert store.delete_learning("o/r", lid) is False


def test_import_lands_pending_until_approved(store):
    doc = cd.export_document([{"text": "imported rule", "repo": "src/repo", "source_pr": 1}])
    plan = cd.plan_import(doc, target_repo="o/new", existing_ids=set())
    ids = store.import_learnings("o/new", plan.to_create, actor="op")
    [row] = store.list_learnings("o/new")
    assert row["id"] == ids[0] and row["status"] == cd.STATUS_PENDING
    assert row["history"][0]["action"] == "imported"
    assert cd.select_for_prompt([row]).selected == []
    store.set_learning_status("o/new", ids[0], cd.STATUS_ACTIVE, actor="op")
    assert cd.select_for_prompt(store.list_learnings("o/new")).selected


def test_import_rolls_back_entirely_when_one_row_fails(store):
    _teach(store, "already here", repo="o/r")
    items = [
        {"text": "fresh rule", "repo": "o/r"},
        # Raced in after the dry run: this insert collides, and the whole
        # import must roll back, including the row before it.
        {"text": "already here", "repo": "o/r"},
    ]
    with pytest.raises(store.LearningImportConflict):
        store.import_learnings("o/r", items, actor="op")
    assert [r["text"] for r in store.list_learnings("o/r")] == ["already here"]


def test_record_usage_counts_once_per_review(store):
    a = _teach(store, "rule a")
    b = _teach(store, "rule b")
    assert store.record_learning_usage("o/r", [a], review_key="7@abc") == 1
    # The same review supplying it again (another chunk, the judge) does not
    # count twice; a new head sha does.
    assert store.record_learning_usage("o/r", [a], review_key="7@abc") == 0
    assert store.record_learning_usage("o/r", [a], review_key="7@def") == 1
    rows = {r["id"]: r for r in store.list_learnings("o/r")}
    assert rows[a]["usage_count"] == 2 and rows[a]["last_used_at"]
    assert rows[b]["usage_count"] == 0 and rows[b]["last_used_at"] == ""
    # Usage survives a re-teach (put_learning preserves the counters).
    _teach(store, "rule a")
    assert store.get_learning("o/r", a)["usage_count"] == 2
