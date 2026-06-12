"""Real-Postgres tests for the pg_* store port (#354).

These run against a REAL Postgres (CI: the workflow's postgres service
container; locally: any reachable instance) via GRUG_TEST_DATABASE_URL.
SQLite stand-ins are banned for this suite - the semantics under test
(ON CONFLICT atomicity, jsonb merge, concurrent claims) are exactly the
ones a fake gets wrong.

Skips LOUDLY when GRUG_TEST_DATABASE_URL is unset so a local run without
a database cannot silently pass as coverage.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_TEST_DB = os.environ.get("GRUG_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _TEST_DB,
    reason=(
        "GRUG_TEST_DATABASE_URL unset - pg store tests REQUIRE a real "
        "Postgres (CI provides a service container; this is not optional "
        "coverage, do not fake it with sqlite)"
    ),
)


@pytest.fixture()
def pg(monkeypatch):
    """Fresh schema per test: point the pool at the test DB and truncate."""
    # This fixture TRUNCATEs grug_kv. Refuse anything that doesn't look
    # like a test database so a mis-exported URL can't wipe live data
    # (same guard as conftest.pg_store).
    if "test" not in _TEST_DB.rsplit("/", 1)[-1]:
        pytest.fail(
            "GRUG_TEST_DATABASE_URL database name must contain 'test' "
            f"(got {_TEST_DB.rsplit('/', 1)[-1]!r}) - this fixture TRUNCATEs it"
        )
    monkeypatch.setenv("GRUG_DATABASE_URL", _TEST_DB)
    from adapters import pg_base

    pg_base.reset_pool_for_tests()
    pool = pg_base.get_pool()
    with pool.connection() as conn:
        conn.execute("TRUNCATE grug_kv")
    yield pg_base
    pg_base.reset_pool_for_tests()


# ---------------------------------------------------------------------------
# install store
# ---------------------------------------------------------------------------


def test_record_then_lookup_roundtrip(pg):
    from adapters import pg_install_store as store

    store.record_installation(
        install_id=42,
        account_login="cavetown",
        account_type="Organization",
        installed_by_user_id=7,
    )
    item = store.get_installation(42)
    assert item is not None
    assert item["account_login"] == "cavetown"
    assert item["PK"] == "INST#42"
    assert item["installed_at"]


def test_duplicate_delivery_preserves_installed_at(pg):
    from adapters import pg_install_store as store

    store.record_installation(
        install_id=1, account_login="a", account_type="User", installed_by_user_id=9
    )
    first = store.get_installation(1)["installed_at"]
    time.sleep(0.01)
    store.record_installation(
        install_id=1, account_login="a2", account_type="User", installed_by_user_id=9
    )
    again = store.get_installation(1)
    assert again["installed_at"] == first  # if_not_exists parity
    assert again["account_login"] == "a2"  # other fields DO update


def test_delete_installation(pg):
    from adapters import pg_install_store as store

    store.record_installation(
        install_id=5, account_login="x", account_type="User", installed_by_user_id=2
    )
    store.delete_installation(5)
    assert store.get_installation(5) is None


def test_allowlist_two_hop(pg):
    from adapters import pg_install_store as store
    from adapters import pg_user_store as users

    store.record_installation(
        install_id=10, account_login="org", account_type="Org", installed_by_user_id=77
    )
    assert store.is_install_allowlisted(10) is False  # no USER row
    _seed_user(pg, "77", allowlisted=False)
    assert store.is_install_allowlisted(10) is False
    users.update_user_fields("77", {"allowlisted": True})
    assert store.is_install_allowlisted(10) is True
    assert store.list_allowlisted_installs() == [10]


def test_list_user_installations_via_gsi(pg):
    from adapters import pg_install_store as store

    for iid in (1, 2):
        store.record_installation(
            install_id=iid,
            account_login=f"a{iid}",
            account_type="User",
            installed_by_user_id=55,
        )
    store.record_installation(
        install_id=3, account_login="other", account_type="User", installed_by_user_id=66
    )
    rows = store.list_user_installations("55")
    assert sorted(r["PK"] for r in rows) == ["INST#1", "INST#2"]


def test_repo_config_defaults_sparse_update_and_enforcement_remove(pg):
    from adapters import pg_install_store as store

    cfg = store.get_repo_config(1, 2)
    assert cfg["tpm_enabled"] is True and cfg["enforcement_ruleset_id"] is None

    store.set_repo_config(
        install_id=1,
        repo_id=2,
        repo_full_name="o/r",
        tpm_enabled=False,
        updated_by_user_id="9",
    )
    store.set_enforcement_id(1, 2, 123)
    cfg = store.get_repo_config(1, 2)
    # sparse update preserved the enforcement id set by the other writer
    assert cfg["tpm_enabled"] is False
    assert cfg["enforcement_ruleset_id"] == 123
    # code_reviewer_* untouched -> defaults
    assert cfg["code_reviewer_enabled"] is True

    store.set_enforcement_id(1, 2, None)  # REMOVE semantics
    assert store.get_enforcement_id(1, 2) is None
    # ...without nuking the rest of the row
    assert store.get_repo_config(1, 2)["tpm_enabled"] is False


def test_check_verdicts_upsert_order_limit_and_derived_verdict(pg):
    from adapters import pg_install_store as store

    for i, sha in enumerate(["aaa", "bbb", "ccc"]):
        store.put_check_verdict(
            install_id=1,
            persona="elder",
            repo="o/r",
            pr_number=1,
            head_sha=sha,
            conclusion="neutral",
            summary="s",
            findings_count=0,
            blocking=False,
            created_at=f"2026-06-12T0{i}:00:00+00:00",
        )
    # re-review of bbb upserts (heals), not appends
    store.put_check_verdict(
        install_id=1,
        persona="elder",
        repo="o/r",
        pr_number=1,
        head_sha="bbb",
        conclusion="neutral",
        summary="healed",
        findings_count=2,
        blocking=False,
        created_at="2026-06-12T05:00:00+00:00",
    )
    rows = store.list_check_verdicts(1)
    assert len(rows) == 3
    assert rows[0]["head_sha"] == "bbb" and rows[0]["summary"] == "healed"
    assert [r["head_sha"] for r in rows] == ["bbb", "ccc", "aaa"]  # newest-first
    assert store.list_check_verdicts(1, limit=2)[1]["head_sha"] == "ccc"
    # verdict is DERIVED, never trusted from a caller
    assert all(r["verdict"] for r in rows)

    degraded = store.list_check_verdicts(1, limit=None)
    assert "degraded_reason" not in degraded[0]  # sparse when falsy


def test_claim_delivery_win_once_and_expired_takeover(pg):
    from adapters import pg_install_store as store

    did = str(uuid.uuid4())
    assert store.claim_delivery(did) is True
    assert store.claim_delivery(did) is False  # redelivery loses
    assert store.claim_delivery("") is True  # fails OPEN by contract

    # Expire the claim manually -> next claim must WIN (DDB's TTL would
    # have deleted the row; PG must treat expired as free).
    with store.get_pool().connection() as conn:
        conn.execute(
            "UPDATE grug_kv SET ttl = EXTRACT(EPOCH FROM now())::bigint - 10 "
            "WHERE pk = %s",
            (f"DELIVERY#{did}",),
        )
    assert store.claim_delivery(did) is True


def test_claim_delivery_concurrent_exactly_one_winner(pg):
    """The reason this suite needs REAL Postgres: N racing claimants,
    exactly one True."""
    from adapters import pg_install_store as store

    did = str(uuid.uuid4())
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        results.append(store.claim_delivery(did))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_claim_delivery_concurrent_expired_takeover_exactly_one_winner(pg):
    """The takeover WHERE clause's race: N claimants see the SAME expired
    row; ON CONFLICT re-evaluation against the winner's committed tuple
    must yield exactly one True (audit M7 - the arm the fresh-pk race
    never exercises)."""
    from adapters import pg_install_store as store

    did = str(uuid.uuid4())
    assert store.claim_delivery(did) is True
    with store.get_pool().connection() as conn:
        conn.execute(
            "UPDATE grug_kv SET ttl = EXTRACT(EPOCH FROM now())::bigint - 10 "
            "WHERE pk = %s",
            (f"DELIVERY#{did}",),
        )

    results: list[bool] = []
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        results.append(store.claim_delivery(did))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_comment_records_roundtrip_and_ttl_filtering(pg):
    from adapters import pg_install_store as store

    store.put_comment_record(
        install_id=1,
        comment_id=100,
        repo="o/r",
        pr_number=5,
        review_span_context={"trace": "t"},
        finding_tags={"k": "v"},
    )
    recs = store.list_comment_records(1)
    assert recs[0]["comment_id"] == 100 and recs[0]["last_verdict"] is None

    store.update_comment_record_reaction(install_id=1, comment_id=100, verdict="up")
    assert store.list_comment_records(1)[0]["last_verdict"] == "up"

    # Expired records vanish from reads even before any purge runs.
    with store.get_pool().connection() as conn:
        conn.execute(
            "UPDATE grug_kv SET ttl = EXTRACT(EPOCH FROM now())::bigint - 10 "
            "WHERE sk = 'CRCOMMENT#100'"
        )
    assert store.list_comment_records(1) == []


# ---------------------------------------------------------------------------
# user store (KMS faked - the envelope is out of scope, storage is not)
# ---------------------------------------------------------------------------


def _seed_user(pg, user_id: str, *, allowlisted: bool) -> None:
    from adapters.pg_base import encode_attrs, get_pool

    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO grug_kv (pk, sk, data) VALUES (%s, 'META', %s) "
            "ON CONFLICT (pk, sk) DO UPDATE SET data = excluded.data",
            (
                f"USER#{user_id}",
                encode_attrs(
                    {
                        "login": f"u{user_id}",
                        "role": "user",
                        "tier": "free",
                        "allowlisted": allowlisted,
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
            ),
        )


@pytest.fixture()
def fake_kms(monkeypatch):
    """Reversible fake envelope: ciphertext = b'enc:' + plaintext bytes.
    Exercises the bytes-through-jsonb codec end-to-end."""
    import crypto.kms_envelope as kms

    monkeypatch.setattr(
        kms,
        "encrypt_for_user",
        lambda *, plaintext, user_id, item_type: b"enc:" + plaintext.encode(),
    )
    monkeypatch.setattr(
        kms,
        "decrypt_for_user",
        lambda *, blob, user_id, item_type: blob[4:].decode(),
    )
    return kms


def test_oauth_upsert_defaults_then_preserves_admin_changes(pg, fake_kms):
    from adapters import pg_user_store as users

    ident = users.upsert_oauth_user(
        github_user_id="9", login="grug", oauth_access_token="tok-1"
    )
    assert (ident.role, ident.tier, ident.allowlisted) == ("user", "free", False)
    created = ident.created_at

    # Admin flips fields between logins...
    users.update_user_fields("9", {"allowlisted": True, "role": "admin"})
    # ...and a re-auth must NOT revert them (lost-update parity).
    ident2 = users.upsert_oauth_user(
        github_user_id="9", login="grug", oauth_access_token="tok-2"
    )
    assert ident2.allowlisted is True and ident2.role == "admin"
    assert ident2.created_at == created

    got = users.get_user_with_tokens("9")
    assert got.oauth_access_token == "tok-2"
    assert got.oauth_refresh_token is None


def test_refresh_blob_preserved_when_not_rotated(pg, fake_kms):
    from adapters import pg_user_store as users

    users.upsert_oauth_user(
        github_user_id="3",
        login="x",
        oauth_access_token="a1",
        oauth_refresh_token="r1",
    )
    # Re-auth WITHOUT a refresh token must keep the old one (Sentry HIGH #39).
    users.upsert_oauth_user(github_user_id="3", login="x", oauth_access_token="a2")
    got = users.get_user_with_tokens("3")
    assert got.oauth_access_token == "a2"
    assert got.oauth_refresh_token == "r1"


def test_delete_user_state_preserves_identity(pg, fake_kms):
    from adapters import pg_user_store as users

    users.upsert_oauth_user(github_user_id="4", login="y", oauth_access_token="a")
    users.update_user_fields("4", {"role": "admin", "tier": "lifetime"})
    users.delete_user_state("4")
    ident = users.get_user("4")
    assert ident.role == "admin" and ident.tier == "lifetime"  # CRITICAL 4x parity
    got = users.get_user_with_tokens("4")
    assert got.oauth_access_token == ""  # blobs gone


def test_corrupt_blob_purges_and_returns_none(pg, fake_kms, monkeypatch):
    import crypto.kms_envelope as kms
    from adapters import pg_user_store as users

    users.upsert_oauth_user(github_user_id="6", login="z", oauth_access_token="a")

    def boom(*, blob, user_id, item_type):
        raise kms.CredentialBlobCorrupt("bad blob")

    monkeypatch.setattr(kms, "decrypt_for_user", boom)
    assert users.get_user_with_tokens("6") is None
    # Identity survives the purge; blobs are gone.
    assert users.get_user("6").login == "z"
    with users.get_pool().connection() as conn:
        (data,) = conn.execute(
            "SELECT data FROM grug_kv WHERE pk = 'USER#6'"
        ).fetchone()
    assert "oauth_access_token_blob" not in data


def test_admin_scan_and_update(pg, fake_kms):
    from adapters import pg_user_store as users
    from adapters import pg_install_store as store

    users.upsert_oauth_user(github_user_id="1", login="a", oauth_access_token="t")
    store.record_installation(
        install_id=9, account_login="o", account_type="Org", installed_by_user_id=1
    )
    assert [i["PK"] for i in users.scan_meta_items(pk_prefix="USER#")] == ["USER#1"]
    assert [i["PK"] for i in users.scan_meta_items(pk_prefix="INST#")] == ["INST#9"]
    new = users.update_user_fields("1", {"tier": "paid"})
    assert new["tier"] == "paid"
    assert users.get_user_item("1")["tier"] == "paid"


# ---------------------------------------------------------------------------
# stage-7 coverage round (PR #366): assertions the deleted DDB-fake suites
# carried that nothing else asserted against the REAL store
# ---------------------------------------------------------------------------


def test_is_persona_enabled_matrix(pg):
    """The webhook gate every event passes through - every dispatcher test
    mocks it, so this is its ONLY non-mocked execution."""
    from adapters import pg_install_store as store

    # Row-less repo: every persona defaults enabled.
    assert store.is_persona_enabled(1, 2, "tpm") is True
    assert store.is_persona_enabled(1, 2, "code_reviewer") is True

    store.set_repo_config(
        install_id=1, repo_id=2, repo_full_name="o/r",
        tpm_enabled=False, updated_by_user_id="9",
    )
    assert store.is_persona_enabled(1, 2, "tpm") is False
    assert store.is_persona_enabled(1, 2, "code_reviewer") is True  # untouched
    # Unknown persona (no <name>_enabled key) fails open to enabled.
    assert store.is_persona_enabled(1, 2, "release-manager") is True


def test_set_repo_config_code_reviewer_fields_round_trip(pg):
    """The dashboard's Elder enable/blocking toggles - the route tests mock
    set_repo_config entirely, so the kwarg branches run only here."""
    from adapters import pg_install_store as store

    store.set_repo_config(
        install_id=1, repo_id=2, repo_full_name="o/r",
        tpm_enabled=True, updated_by_user_id="9",
        code_reviewer_enabled=False, code_reviewer_blocking=True,
    )
    cfg = store.get_repo_config(1, 2)
    assert cfg["code_reviewer_enabled"] is False
    assert cfg["code_reviewer_blocking"] is True
    assert cfg["tpm_enabled"] is True
    assert store.is_persona_enabled(1, 2, "code_reviewer") is False

    # Re-enable without the blocking kwarg flips ONLY the enabled bit;
    # blocking persists (sparse merge).
    store.set_repo_config(
        install_id=1, repo_id=2, repo_full_name="o/r",
        tpm_enabled=True, updated_by_user_id="9", code_reviewer_enabled=True,
    )
    cfg = store.get_repo_config(1, 2)
    assert cfg["code_reviewer_enabled"] is True
    assert cfg["code_reviewer_blocking"] is True


def test_re_auth_with_new_refresh_replaces(pg, fake_kms):
    """The overshoot direction of refresh-preserve: rotation must REPLACE,
    not keep the stale blob (the failure mode a 'preserve' fix invites)."""
    from adapters import pg_user_store as users

    users.upsert_oauth_user(
        github_user_id="3", login="x",
        oauth_access_token="a1", oauth_refresh_token="r1",
    )
    users.upsert_oauth_user(
        github_user_id="3", login="x",
        oauth_access_token="a2", oauth_refresh_token="r2",
    )
    got = users.get_user_with_tokens("3")
    assert got.oauth_access_token == "a2"
    assert got.oauth_refresh_token == "r2"


def test_check_verdict_degraded_reason_round_trips_as_errored(pg):
    """/activity's errored filter depends on the degraded write arm; the
    sparse-when-absent arm alone leaves it unexecuted."""
    from adapters import pg_install_store as store

    store.put_check_verdict(
        install_id=1, persona="elder", repo="o/r", pr_number=1,
        head_sha="abc", conclusion="neutral", summary="s",
        findings_count=0, blocking=False,
        created_at="2026-06-12T00:00:00+00:00",
        degraded_reason="all_backends_failed",
    )
    row = store.list_check_verdicts(1)[0]
    assert row["degraded_reason"] == "all_backends_failed"
    assert row["verdict"] == "errored"  # never 'pass' on a degraded run


def test_put_paths_write_future_ttl(pg):
    """The TTL_LIVE filter tests manually expire rows - which never proves
    the put paths WRITE a ttl. A NULL-ttl regression would accumulate rows
    forever while every filter test stays green."""
    import time as _time

    from adapters import pg_install_store as store

    store.put_comment_record(
        install_id=1, comment_id=100, repo="o/r", pr_number=5,
        review_span_context={}, finding_tags={},
    )
    store.put_check_verdict(
        install_id=1, persona="elder", repo="o/r", pr_number=1,
        head_sha="abc", conclusion="neutral", summary="s",
        findings_count=0, blocking=False,
        created_at="2026-06-12T00:00:00+00:00",
    )
    assert store.claim_delivery("ttl-probe") is True

    now = _time.time()
    day = 86400
    with store.get_pool().connection() as conn:
        rows = dict(
            conn.execute("SELECT sk, ttl FROM grug_kv WHERE ttl IS NOT NULL").fetchall()
        )
    assert now + 29 * day < rows["CRCOMMENT#100"] <= now + 31 * day
    assert now + 89 * day < rows["ACT#abc#elder"] <= now + 91 * day
    assert now + 23 * 3600 < rows["META"] <= now + 25 * 3600  # DELIVERY# claim


def test_comment_record_span_and_tags_round_trip(pg):
    """The reaction poller's span attribution rides these two dicts."""
    from adapters import pg_install_store as store

    store.put_comment_record(
        install_id=1, comment_id=100, repo="o/r", pr_number=5,
        review_span_context={"trace_id": "t1", "span_id": "s1"},
        finding_tags={"rule": "no-bare-except", "file": "x.py"},
    )
    rec = store.list_comment_records(1)[0]
    assert rec["review_span_context"] == {"trace_id": "t1", "span_id": "s1"}
    assert rec["finding_tags"] == {"rule": "no-bare-except", "file": "x.py"}


def test_allowlist_edges_missing_row_and_mixed_set(pg):
    from adapters import pg_install_store as store
    from adapters import pg_user_store as users

    assert store.is_install_allowlisted(404) is False  # no INST row at all
    assert store.list_allowlisted_installs() == []

    for iid, uid, allowed in ((1, "11", True), (2, "22", False), (3, "33", True)):
        store.record_installation(
            install_id=iid, account_login=f"a{iid}", account_type="User",
            installed_by_user_id=int(uid),
        )
        _seed_user(pg, uid, allowlisted=False)
        if allowed:
            users.update_user_fields(uid, {"allowlisted": True})
    assert store.list_allowlisted_installs() == [1, 3]


def test_scan_meta_items_excludes_ttl_expired(pg):
    from adapters import pg_user_store as users

    _seed_user(pg, "1", allowlisted=False)
    _seed_user(pg, "2", allowlisted=False)
    with users.get_pool().connection() as conn:
        conn.execute(
            "UPDATE grug_kv SET ttl = EXTRACT(EPOCH FROM now())::bigint - 10 "
            "WHERE pk = 'USER#2'"
        )
    assert [i["PK"] for i in users.scan_meta_items(pk_prefix="USER#")] == ["USER#1"]
