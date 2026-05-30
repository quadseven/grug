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
