"""Tests for install_store + allowlist gate.

Uses moto to spin a local DDB so adapter logic runs against a real
boto3 table — closer to prod than naive mock objects (matches
`feedback_no_coding_by_analogy` — verify against ground-truth shape).
"""

from __future__ import annotations

import os

import boto3
import pytest


@pytest.fixture(autouse=True)
def _ddb_table(monkeypatch):
    """Spin a moto DDB grug-main with the production schema."""
    moto = pytest.importorskip("moto")
    from moto import mock_aws  # type: ignore

    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("GRUG_DDB_TABLE", "grug-main-test")
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="grug-main-test",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # Re-import after env-var set so module-scope _table picks up new name.
        import importlib
        import adapters.install_store as mod
        importlib.reload(mod)
        yield mod


def test_record_then_lookup(_ddb_table):
    mod = _ddb_table
    mod.record_installation(
        install_id=42, account_login="evan", account_type="User",
        installed_by_user_id=99,
    )
    inst = mod.get_installation(42)
    assert inst["account_login"] == "evan"
    assert inst["installed_by_user_id"] == "99"


def test_delete_installation(_ddb_table):
    mod = _ddb_table
    mod.record_installation(
        install_id=42, account_login="x", account_type="User",
        installed_by_user_id=1,
    )
    mod.delete_installation(42)
    assert mod.get_installation(42) is None


def test_allowlist_unknown_install_returns_false(_ddb_table):
    assert _ddb_table.is_install_allowlisted(404) is False


def test_allowlist_user_not_allowlisted_returns_false(_ddb_table):
    mod = _ddb_table
    mod.record_installation(
        install_id=10, account_login="bob", account_type="User",
        installed_by_user_id=200,
    )
    # USER#200 row missing entirely — should still return False, not crash.
    assert mod.is_install_allowlisted(10) is False


def test_allowlist_user_explicitly_false(_ddb_table):
    mod = _ddb_table
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    table.put_item(Item={"PK": "USER#200", "SK": "META", "allowlisted": False})
    mod.record_installation(
        install_id=10, account_login="bob", account_type="User",
        installed_by_user_id=200,
    )
    assert mod.is_install_allowlisted(10) is False


def test_allowlist_user_true(_ddb_table):
    mod = _ddb_table
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    table.put_item(Item={"PK": "USER#200", "SK": "META", "allowlisted": True})
    mod.record_installation(
        install_id=10, account_login="bob", account_type="User",
        installed_by_user_id=200,
    )
    assert mod.is_install_allowlisted(10) is True


def test_record_idempotent(_ddb_table):
    """Re-recording the same install must not error."""
    mod = _ddb_table
    for _ in range(3):
        mod.record_installation(
            install_id=10, account_login="bob", account_type="User",
            installed_by_user_id=200,
        )
    assert mod.get_installation(10)["account_login"] == "bob"


def test_record_preserves_installed_at_on_rewrite(_ddb_table):
    """Greptile P2 on PR #41 — re-record must NOT overwrite installed_at.
    Without this guard, a permissions-accept event months later would
    silently re-stamp the row with that day's date, losing the original
    install date forever."""
    import time
    mod = _ddb_table
    mod.record_installation(install_id=42, account_login="a", account_type="User",
                            installed_by_user_id=100)
    first_ts = mod.get_installation(42)["installed_at"]
    time.sleep(0.05)
    mod.record_installation(install_id=42, account_login="a", account_type="User",
                            installed_by_user_id=100)
    assert mod.get_installation(42)["installed_at"] == first_ts


# Slice 7 (#28) — per-repo persona toggles


def test_list_user_installations_via_gsi1(_ddb_table):
    mod = _ddb_table
    mod.record_installation(install_id=1, account_login="a", account_type="User",
                            installed_by_user_id=100)
    mod.record_installation(install_id=2, account_login="b", account_type="User",
                            installed_by_user_id=100)
    mod.record_installation(install_id=3, account_login="c", account_type="User",
                            installed_by_user_id=999)
    rows = mod.list_user_installations("100")
    ids = sorted(int(r["PK"].split("#")[1]) for r in rows)
    assert ids == [1, 2]


def test_repo_config_default_is_tpm_enabled_true(_ddb_table):
    mod = _ddb_table
    cfg = mod.get_repo_config(install_id=1, repo_id=42)
    assert cfg == {
        "tpm_enabled": True,
        "code_reviewer_enabled": True,
        "code_reviewer_blocking": False,
        "enforcement_ruleset_id": None,
        "force_disable_enforcement": False,
    }


def test_set_then_get_repo_config(_ddb_table):
    mod = _ddb_table
    mod.set_repo_config(install_id=1, repo_id=42, repo_full_name="x/y",
                        tpm_enabled=False, updated_by_user_id="100")
    # code_reviewer_* kwargs not passed → row carries no override →
    # `.get()` falls back to _DEFAULT_PERSONA_CONFIG (True/False).
    assert mod.get_repo_config(1, 42) == {
        "tpm_enabled": False,
        "code_reviewer_enabled": True,
        "code_reviewer_blocking": False,
        "enforcement_ruleset_id": None,
        "force_disable_enforcement": False,
    }


def test_is_persona_enabled_default_true(_ddb_table):
    assert _ddb_table.is_persona_enabled(1, 42, "tpm") is True


def test_is_persona_enabled_after_disable(_ddb_table):
    mod = _ddb_table
    mod.set_repo_config(install_id=1, repo_id=42, repo_full_name="x/y",
                        tpm_enabled=False, updated_by_user_id="100")
    assert mod.is_persona_enabled(1, 42, "tpm") is False


def test_is_persona_enabled_unknown_persona_defaults_true(_ddb_table):
    """v1 default policy: unrecognized personas don't gate via this fn."""
    assert _ddb_table.is_persona_enabled(1, 42, "release-manager") is True


def test_default_persona_config_includes_code_reviewer(_ddb_table):
    """Elder (code_reviewer) persona ships enabled by default. Newly-
    installed users get inline review comments out-of-the-box; opt-out
    is explicit per repo. `code_reviewer_blocking` defaults False —
    advisory mode (event=COMMENT, conclusion=neutral) so false-positives
    don't block velocity."""
    cfg = _ddb_table._DEFAULT_PERSONA_CONFIG
    assert cfg.get("code_reviewer_enabled") is True
    assert cfg.get("code_reviewer_blocking") is False


def test_is_persona_enabled_code_reviewer_default_true(_ddb_table):
    assert _ddb_table.is_persona_enabled(1, 42, "code_reviewer") is True


def test_get_repo_config_surfaces_code_reviewer_fields(_ddb_table):
    """No row exists → defaults dict carries all persona keys (TPM +
    Elder) + the two enforcement-state fields."""
    cfg = _ddb_table.get_repo_config(1, 42)
    assert cfg.get("code_reviewer_enabled") is True
    assert cfg.get("code_reviewer_blocking") is False


def test_set_repo_config_persists_code_reviewer_fields(_ddb_table):
    """Disabling Elder via set_repo_config must round-trip through
    DDB. Same shape as tpm_enabled — kwarg-driven update."""
    mod = _ddb_table
    mod.set_repo_config(
        install_id=1, repo_id=42, repo_full_name="x/y",
        tpm_enabled=True, code_reviewer_enabled=False,
        code_reviewer_blocking=False, updated_by_user_id="100",
    )
    cfg = mod.get_repo_config(1, 42)
    assert cfg["code_reviewer_enabled"] is False
    assert cfg["code_reviewer_blocking"] is False
    # TPM unchanged (kwarg sent True).
    assert cfg["tpm_enabled"] is True


def test_get_repo_config_legacy_row_falls_back_to_defaults(_ddb_table):
    """Pre-Elder rows (tpm_enabled only) must surface code_reviewer_*
    defaults from _DEFAULT_PERSONA_CONFIG. A regression that read the
    missing field as `None` then `bool(None) is False` would silently
    flip Elder OFF for every legacy repo on the first webhook event."""
    mod = _ddb_table
    table = boto3.resource(
        "dynamodb", region_name="us-east-1",
    ).Table("grug-main-test")
    table.put_item(Item={
        "PK": "INST#1", "SK": "REPO#42",
        "repo_full_name": "x/y",
        "tpm_enabled": False,
        # Intentionally no code_reviewer_enabled / code_reviewer_blocking.
    })
    cfg = mod.get_repo_config(1, 42)
    assert cfg["tpm_enabled"] is False
    assert cfg["code_reviewer_enabled"] is True   # default
    assert cfg["code_reviewer_blocking"] is False  # default


def test_set_repo_config_blocking_mode_round_trips(_ddb_table):
    """Operator flips advisory→blocking via dashboard. Must persist."""
    mod = _ddb_table
    mod.set_repo_config(
        install_id=1, repo_id=42, repo_full_name="x/y",
        tpm_enabled=True, code_reviewer_enabled=True,
        code_reviewer_blocking=True, updated_by_user_id="100",
    )
    cfg = mod.get_repo_config(1, 42)
    assert cfg["code_reviewer_blocking"] is True


# Elder reaction-poll comment records (#245a)


def test_put_then_list_comment_record(_ddb_table):
    """A Grug-posted inline comment is persisted so the reaction poller
    can later read its reactions + attribute the annotation to the
    review span."""
    mod = _ddb_table
    mod.put_comment_record(
        install_id=1, comment_id=555, repo="o/r", pr_number=7,
        review_span_context={"trace_id": "t1", "span_id": "s1"},
        finding_tags={"rule_name": "null-deref", "file": "x.py", "line": "2"},
    )
    recs = mod.list_comment_records(1)
    assert len(recs) == 1
    r = recs[0]
    assert r["comment_id"] == 555
    assert r["repo"] == "o/r"
    assert r["pr_number"] == 7
    assert r["review_span_context"] == {"trace_id": "t1", "span_id": "s1"}
    assert r["finding_tags"]["rule_name"] == "null-deref"
    # last_verdict starts unset — nothing submitted yet (dedup baseline).
    assert r["last_verdict"] is None


def test_list_comment_records_scoped_to_install(_ddb_table):
    """Comment records are listed per-install; another install's records
    must not leak into the poll batch."""
    mod = _ddb_table
    mod.put_comment_record(
        install_id=1, comment_id=1, repo="o/r", pr_number=1,
        review_span_context={"trace_id": "t", "span_id": "s"}, finding_tags={},
    )
    mod.put_comment_record(
        install_id=2, comment_id=2, repo="o/r", pr_number=1,
        review_span_context={"trace_id": "t", "span_id": "s"}, finding_tags={},
    )
    assert [r["comment_id"] for r in mod.list_comment_records(1)] == [1]
    assert [r["comment_id"] for r in mod.list_comment_records(2)] == [2]


def test_put_comment_record_sets_ttl(_ddb_table):
    """Records carry a `ttl` epoch ~30 days out so DDB auto-expires them
    (the deploy slice enables table TTL) — bounds the poll partition as
    PRs close."""
    import time as _time
    mod = _ddb_table
    before = int(_time.time())
    mod.put_comment_record(
        install_id=1, comment_id=1, repo="o/r", pr_number=1,
        review_span_context={"trace_id": "t", "span_id": "s"}, finding_tags={},
    )
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("grug-main-test")
    item = table.get_item(Key={"PK": "INST#1", "SK": "CRCOMMENT#1"})["Item"]
    ttl = int(item["ttl"])
    # ~30 days out (allow a generous window for clock + test slowness).
    assert before + 29 * 86400 < ttl < before + 31 * 86400


def test_list_comment_records_paginates(_ddb_table, monkeypatch):
    """list_comment_records must follow LastEvaluatedKey — a single DDB
    page caps at 1MB, so a busy install would silently truncate the
    poll batch without the pagination loop."""
    mod = _ddb_table
    # Two records; stub _table.query to return them across TWO pages so
    # the LastEvaluatedKey loop is exercised regardless of real sizes.
    real_items = [
        {"PK": "INST#1", "SK": "CRCOMMENT#1", "comment_id": 1, "repo": "o/r",
         "pr_number": 1, "review_span_context": {}, "finding_tags": {}},
        {"PK": "INST#1", "SK": "CRCOMMENT#2", "comment_id": 2, "repo": "o/r",
         "pr_number": 1, "review_span_context": {}, "finding_tags": {}},
    ]
    pages = [
        {"Items": real_items[:1], "LastEvaluatedKey": {"PK": "INST#1", "SK": "CRCOMMENT#1"}},
        {"Items": real_items[1:]},  # no LastEvaluatedKey → loop ends
    ]
    calls = {"n": 0}

    def fake_query(**kwargs):
        i = calls["n"]
        calls["n"] += 1
        # Second call must carry the ExclusiveStartKey from page 1.
        if i == 1:
            assert kwargs.get("ExclusiveStartKey") == {"PK": "INST#1", "SK": "CRCOMMENT#1"}
        return pages[i]

    monkeypatch.setattr(mod._table, "query", fake_query)
    recs = mod.list_comment_records(1)
    assert [r["comment_id"] for r in recs] == [1, 2]
    assert calls["n"] == 2  # both pages fetched


def test_update_comment_record_reaction_dedup_baseline(_ddb_table):
    """Recording the last-submitted verdict is the dedup baseline — the
    poller compares the current reaction against it and only submits on
    change."""
    mod = _ddb_table
    mod.put_comment_record(
        install_id=1, comment_id=9, repo="o/r", pr_number=1,
        review_span_context={"trace_id": "t", "span_id": "s"}, finding_tags={},
    )
    mod.update_comment_record_reaction(install_id=1, comment_id=9, verdict="false_positive")
    rec = mod.list_comment_records(1)[0]
    assert rec["last_verdict"] == "false_positive"


# --- list_allowlisted_installs (#247b poller batch) ---

def _put_user(mod, user_id, allowlisted):
    mod._table.put_item(Item={
        "PK": mod._user_pk(user_id), "SK": "META", "allowlisted": allowlisted,
    })


def test_list_allowlisted_installs_returns_only_allowlisted(_ddb_table):
    mod = _ddb_table
    for iid, uid in [(1, 100), (2, 200), (3, 300)]:
        mod.record_installation(
            install_id=iid, account_login=f"u{uid}", account_type="User",
            installed_by_user_id=uid,
        )
    _put_user(mod, 100, True)
    _put_user(mod, 200, False)   # installer not allowlisted
    _put_user(mod, 300, True)
    assert sorted(mod.list_allowlisted_installs()) == [1, 3]


def test_list_allowlisted_installs_empty_when_none(_ddb_table):
    assert _ddb_table.list_allowlisted_installs() == []


def test_list_allowlisted_installs_paginates(_ddb_table, monkeypatch):
    """A >1MB scan returns LastEvaluatedKey; the loop must accumulate across
    pages, not truncate at page 1."""
    mod = _ddb_table
    # Two scan pages: page1 has INST#1 + a continuation key, page2 has INST#2.
    pages = [
        {"Items": [{"PK": "INST#1"}], "LastEvaluatedKey": {"PK": "INST#1", "SK": "META"}},
        {"Items": [{"PK": "INST#2"}]},
    ]
    calls = {"n": 0}

    def _scan(**kwargs):
        i = calls["n"]
        calls["n"] += 1
        # page 2 must be requested with the continuation key
        if i == 1:
            assert kwargs.get("ExclusiveStartKey") == {"PK": "INST#1", "SK": "META"}
        return pages[i]
    monkeypatch.setattr(mod._table, "scan", _scan)
    # Filter applies ACROSS the page boundary: install 1 (page 1) not
    # allowlisted, install 2 (page 2) is → only [2] survives.
    monkeypatch.setattr(mod, "is_install_allowlisted", lambda iid: iid == 2)
    assert mod.list_allowlisted_installs() == [2]
    assert calls["n"] == 2   # both pages fetched


# ── Check verdict store (PRD #301) ──────────────────────────────────────────

def _put_cv(mod, **kw):
    # No `verdict=` — put_check_verdict DERIVES the badge from the raw facts
    # (conclusion/findings_count/degraded_reason) via review_types.verdict.
    base = dict(
        install_id=1, persona="elder", repo="o/r", pr_number=7,
        head_sha="abc123", conclusion="neutral", summary="t",
        findings_count=0, blocking=False,
        created_at="2026-06-06T00:00:00+00:00",
    )
    base.update(kw)
    mod.put_check_verdict(**base)


def test_check_verdict_put_then_list(_ddb_table):
    mod = _ddb_table
    _put_cv(mod, conclusion="neutral", findings_count=2)  # derives -> warn
    rows = mod.list_check_verdicts(1)
    assert len(rows) == 1
    assert rows[0]["persona"] == "elder"
    assert rows[0]["verdict"] == "warn"        # derived from the raw facts
    assert rows[0]["findings_count"] == 2


def test_check_verdict_idempotent_per_persona_headsha(_ddb_table):
    """Re-reviewing the SAME (persona, commit) upserts (heals) — one row,
    latest wins. A NEW commit appends."""
    mod = _ddb_table
    _put_cv(mod, head_sha="abc", degraded_reason="all_failed")  # derives -> errored
    _put_cv(mod, head_sha="abc", conclusion="success")          # heal -> pass
    rows = mod.list_check_verdicts(1)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "pass"            # healed in place
    _put_cv(mod, head_sha="def", conclusion="failure")  # new commit appends
    assert len(mod.list_check_verdicts(1)) == 2


def test_check_verdict_distinct_personas_same_commit_are_two_rows(_ddb_table):
    mod = _ddb_table
    _put_cv(mod, persona="chief", head_sha="abc")
    _put_cv(mod, persona="elder", head_sha="abc")
    assert len(mod.list_check_verdicts(1)) == 2


def test_check_verdict_newest_first_and_limit(_ddb_table):
    mod = _ddb_table
    _put_cv(mod, head_sha="s1", created_at="2026-06-01T00:00:00+00:00")
    _put_cv(mod, head_sha="s2", created_at="2026-06-03T00:00:00+00:00")
    _put_cv(mod, head_sha="s3", created_at="2026-06-02T00:00:00+00:00")
    rows = mod.list_check_verdicts(1)
    assert [r["head_sha"] for r in rows] == ["s2", "s3", "s1"]  # newest created_at first
    assert len(mod.list_check_verdicts(1, limit=2)) == 2


def test_check_verdict_ttl_set(_ddb_table):
    mod = _ddb_table
    _put_cv(mod, head_sha="ttlcheck")
    item = boto3.resource("dynamodb", region_name="us-east-1").Table(
        "grug-main-test"
    ).get_item(Key={"PK": "INST#1", "SK": "ACT#ttlcheck#elder"})["Item"]
    assert "ttl" in item and int(item["ttl"]) > 0


def test_check_verdict_degraded_reason_is_sparse(_ddb_table):
    """degraded_reason is omitted from the item when None (sparse), present
    when set — matches CommentRecord.last_verdict opaque-optional discipline."""
    mod = _ddb_table
    _put_cv(mod, head_sha="clean")  # degraded_reason default None
    _put_cv(mod, head_sha="bad", degraded_reason="all_failed")  # derives -> errored
    rows = {r["head_sha"]: r for r in mod.list_check_verdicts(1)}
    assert "degraded_reason" not in rows["clean"]
    assert rows["bad"]["degraded_reason"] == "all_failed"
