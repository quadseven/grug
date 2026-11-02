"""Real-Postgres tests for the Cave Drawings learnings lifecycle (#1203):
inspect, edit with version history, disable, approve, delete, the
all-or-nothing import, and the per-review usage counter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def test_review_reads_skip_inactive_rows_but_the_operator_sees_them(store):
    on = _teach(store, "on")
    off = _teach(store, "off")
    store.set_learning_status("o/r", off, cd.STATUS_DISABLED, actor="op")
    assert [r["id"] for r in store.list_learnings("o/r")] == [on]
    assert {r["id"] for r in store.list_learnings("o/r", include_inactive=True)} == {on, off}


def test_a_row_written_before_the_active_flag_stays_active(store):
    _teach(store, "legacy rule")
    with store.get_pool().connection() as conn:
        conn.execute("UPDATE grug_kv SET data = data - 'active' WHERE pk = 'LEARN#o/r'")
    assert [r["text"] for r in store.list_learnings("o/r")] == ["legacy rule"]


def test_redelivery_detection_sees_a_disabled_learning(store):
    lid = _teach(store, "rule", source_comment_id=77)
    store.set_learning_status("o/r", lid, cd.STATUS_DISABLED, actor="op")
    assert store.get_learning_by_source_comment("o/r", 77)["id"] == lid


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
    assert store.list_learnings("o/new") == []  # nothing reaches a review yet
    [row] = store.list_learnings("o/new", include_inactive=True)
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


# --- governed learnings (#1571) ---------------------------------------------

def _age(store, lid, days, repo="o/r"):
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store.get_pool().connection() as conn:
        conn.execute(
            "UPDATE grug_kv SET data = data || jsonb_build_object("
            "'created_at', %s::text, 'reinforced_at', %s::text) WHERE pk = %s AND sk = %s",
            (stamp, stamp, f"LEARN#{repo}", f"LEARNING#{lid}"),
        )


def test_a_rule_past_expiry_leaves_review_reads_but_stays_visible(store):
    lid = _teach(store, "old rule")
    _age(store, lid, 91)
    assert store.list_learnings("o/r") == []
    [row] = store.list_learnings("o/r", include_inactive=True)
    assert cd.effective_status(dict(row)) == cd.STATUS_EXPIRED


def test_approving_an_expired_rule_reaffirms_it(store):
    lid = _teach(store, "old rule")
    _age(store, lid, 91)
    row = store.set_learning_status("o/r", lid, cd.STATUS_ACTIVE, actor="op")
    assert row["approved_by"] == "op"
    assert [r["id"] for r in store.list_learnings("o/r")] == [lid]


def test_approving_a_live_rule_restarts_its_90_days_and_logs_it(store):
    lid = _teach(store, "live rule")
    _age(store, lid, 80)
    row = store.set_learning_status("o/r", lid, cd.STATUS_ACTIVE, actor="op")
    assert [h["action"] for h in row["history"]] == ["reaffirmed"]
    _age(store, lid, 89)  # the taught dates are old; only the affirmation is new
    assert [r["id"] for r in store.list_learnings("o/r")] == [lid]


def test_a_reteach_restarts_the_90_days(store):
    lid = _teach(store, "old rule")
    _age(store, lid, 91)
    _teach(store, "old rule")
    assert [r["id"] for r in store.list_learnings("o/r")] == [lid]


def test_editing_an_expired_rule_reaffirms_it(store):
    lid = _teach(store, "old rule")
    _age(store, lid, 91)
    new = store.edit_learning("o/r", lid, actor="op", text="new rule")
    assert cd.effective_status(dict(new)) == cd.STATUS_ACTIVE
    assert [r["text"] for r in store.list_learnings("o/r")] == ["new rule"]


def test_a_scope_edit_reaffirms_a_rule(store):
    lid = _teach(store, "old rule")
    _age(store, lid, 91)
    store.edit_learning("o/r", lid, actor="op", scope_path="src/*.py")
    assert [r["id"] for r in store.list_learnings("o/r")] == [lid]


def test_a_reaffirmed_rule_outlives_its_original_row_ttl(store):
    lid = _teach(store, "rule")
    with store.get_pool().connection() as conn:
        conn.execute(
            "UPDATE grug_kv SET ttl = EXTRACT(EPOCH FROM now())::bigint + 86400 "
            "WHERE pk = 'LEARN#o/r' AND sk = %s", (f"LEARNING#{lid}",),
        )
    store.set_learning_status("o/r", lid, cd.STATUS_ACTIVE, actor="op")
    with store.get_pool().connection() as conn:
        (ttl,) = conn.execute(
            "SELECT ttl FROM grug_kv WHERE pk = 'LEARN#o/r' AND sk = %s",
            (f"LEARNING#{lid}",),
        ).fetchone()
    assert ttl > datetime.now(timezone.utc).timestamp() + 90 * 86400


def test_a_taught_rule_keeps_its_reason_and_finding_class(store):
    lid = _teach(
        store, "rule", reason="the caller validates", finding_class="null-deref",
        finding_category="correctness",
    )
    row = store.get_learning("o/r", lid)
    assert row["reason"] == "the caller validates"
    assert row["finding_class"] == "null-deref"
    assert row["finding_category"] == "correctness"


def test_each_review_records_which_rules_fired(store):
    a, b = _teach(store, "rule a"), _teach(store, "rule b")
    store.record_learning_usage("o/r", [a], review_key="9@abc")
    # A later prompt of the same review adds to the record and counts once.
    store.record_learning_usage("o/r", [a, b], review_key="9@abc")
    store.record_learning_usage("o/r", [b], review_key="10@def")
    assert store.get_review_fired_learnings("o/r", "9@abc") == sorted([a, b])
    assert store.get_review_fired_learnings("o/r", "10@def") == [b]
    assert store.get_review_fired_learnings("o/r", "11@none") == []
