"""api-side test fixtures (NOT a mirrored file - webhook has its own).

Post-#354 store swap: route/store tests run against the REAL Postgres
test database (CI service container). moto stays for the KMS envelope
only - mock_aws intercepts boto3, psycopg is untouched. Skips loudly
without GRUG_TEST_DATABASE_URL (same posture as test_pg_stores.py).
"""

from __future__ import annotations

import importlib
import os

import boto3
import pytest


def seed_meta(pk, attrs, *, gsi1pk=None, gsi1sk=None):
    """Seed one raw META row through the adapter's own codec — the same
    write shape record_installation/upsert_oauth_user produce. The single
    place tests are allowed to hand-write the grug_kv row shape; seed
    through this, not inline SQL, so schema drift has one home."""
    from adapters import pg_base

    with pg_base.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO grug_kv (pk, sk, data, gsi1pk, gsi1sk) "
            "VALUES (%s, 'META', %s, %s, %s) "
            "ON CONFLICT (pk, sk) DO UPDATE SET data = EXCLUDED.data, "
            "gsi1pk = EXCLUDED.gsi1pk, gsi1sk = EXCLUDED.gsi1sk",
            (pk, pg_base.encode_attrs(attrs), gsi1pk, gsi1sk),
        )


@pytest.fixture
def pg_store(monkeypatch):
    test_db = os.environ.get("GRUG_TEST_DATABASE_URL", "")
    if not test_db:
        pytest.skip(
            "GRUG_TEST_DATABASE_URL unset - store-backed tests REQUIRE the "
            "real Postgres test database (CI provides it)"
        )
    # This fixture TRUNCATEs grug_kv. Refuse anything that doesn't look
    # like a test database so a mis-exported URL can't wipe live data.
    if "test" not in test_db.rsplit("/", 1)[-1]:
        pytest.fail(
            "GRUG_TEST_DATABASE_URL database name must contain 'test' "
            f"(got {test_db.rsplit('/', 1)[-1]!r}) - this fixture TRUNCATEs it"
        )
    from moto import mock_aws

    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        kms = boto3.client("kms", region_name="us-east-1")
        cmk = kms.create_key(Description="test-grug-tokens")
        monkeypatch.setenv("GRUG_KMS_CMK_ARN", cmk["KeyMetadata"]["Arn"])
        monkeypatch.setenv("GRUG_DATABASE_URL", test_db)

        import crypto.kms_envelope as kms_mod

        importlib.reload(kms_mod)
        from adapters import pg_base

        # Setup-truncate is the isolation boundary: each pg_store test
        # starts clean but leaves its rows behind for the NEXT setup to
        # clear. Any store-touching test must use this fixture (or one
        # depending on it) or it will see a sibling's leftovers.
        pg_base.reset_pool_for_tests()
        with pg_base.get_pool().connection() as conn:
            conn.execute("TRUNCATE grug_kv")

        import adapters.install_store as inst
        import adapters.user_store as us

        yield {"user_store": us, "install_store": inst}
        pg_base.reset_pool_for_tests()
