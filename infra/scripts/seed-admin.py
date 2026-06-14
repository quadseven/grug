#!/usr/bin/env python3
"""Seed an admin user row in the grug_kv Postgres store (#354 swap).

Usage:
    GRUG_DATABASE_URL=postgresql://... python3 infra/scripts/seed-admin.py \\
        --github-user-id <YOUR_GITHUB_USER_ID> --login <YOUR_LOGIN>

Supply your OWN GitHub identity — there is no default, by design. A fork
that seeded the upstream maintainer's id would hand that account lifetime
admin of the fork's database.

Idempotent: re-running on an existing row preserves OAuth blobs and
created_at; it (re)writes login, role, tier, allowlisted (always True)
and backfills the allowlisted_at/by audit pair if absent.

Slice 5 #26 — required to unblock the webhook allowlist gate. Without
this, the webhook will no_op every PR until at least one admin row
exists with allowlisted=true. (The DDB version this replaced lives in
git history; post-swap it would "succeed" into a table nothing reads.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import psycopg


def _fetch_meta(conn: psycopg.Connection, pk: str) -> dict:
    row = conn.execute(
        "SELECT data FROM grug_kv WHERE pk = %s AND sk = 'META'", (pk,)
    ).fetchone()
    return row[0] if row else {}


def _upsert_meta(
    conn: psycopg.Connection,
    pk: str,
    item: dict,
    gsi1pk: str | None = None,
    gsi1sk: str | None = None,
) -> None:
    """Write a fully-merged META row. The caller read-merged against the
    existing data (preserving OAuth blobs etc.); single transaction is
    fine for a manual one-shot seed."""
    conn.execute(
        """
        INSERT INTO grug_kv (pk, sk, data, gsi1pk, gsi1sk)
        VALUES (%(pk)s, 'META', %(data)s, %(gsi1pk)s, %(gsi1sk)s)
        ON CONFLICT (pk, sk) DO UPDATE
            SET data = EXCLUDED.data,
                gsi1pk = COALESCE(EXCLUDED.gsi1pk, grug_kv.gsi1pk),
                gsi1sk = COALESCE(EXCLUDED.gsi1sk, grug_kv.gsi1sk)
        """,
        {"pk": pk, "data": json.dumps(item), "gsi1pk": gsi1pk, "gsi1sk": gsi1sk},
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--github-user-id", required=True, type=int,
                   help="GitHub numeric user ID (curl https://api.github.com/users/<login> | jq .id)")
    p.add_argument("--login", required=True, help="GitHub login (e.g. githumps)")
    p.add_argument("--database-url", default=os.environ.get("GRUG_DATABASE_URL", ""),
                   help="Postgres URL (defaults to $GRUG_DATABASE_URL)")
    p.add_argument("--role", default="admin", choices=["admin", "user"])
    p.add_argument("--tier", default="lifetime",
                   choices=["lifetime", "free", "paid"])
    p.add_argument("--allowlisted-by", default="seed-admin.py")
    p.add_argument("--install-id", type=int, default=None,
                   help="Optional: also backfill INST#<id> META row for "
                        "Apps installed BEFORE Slice 5 shipped (no `installation:created` "
                        "event was processed). Use the install_id from the post-install "
                        "redirect URL.")
    p.add_argument("--account-login", default=None,
                   help="Account login for --install-id row (defaults to --login)")
    p.add_argument("--account-type", default="User",
                   choices=["User", "Organization"])
    args = p.parse_args()

    if not args.database_url:
        print("FATAL: no database URL (set GRUG_DATABASE_URL or --database-url)",
              file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).isoformat()

    with psycopg.connect(args.database_url) as conn:
        existing = _fetch_meta(conn, f"USER#{args.github_user_id}")
        item = {
            **existing,
            "login": args.login,
            "role": args.role,
            "tier": args.tier,
            "allowlisted": True,
            "allowlisted_at": existing.get("allowlisted_at") or now,
            "allowlisted_by": existing.get("allowlisted_by") or args.allowlisted_by,
        }
        if "created_at" not in item:
            item["created_at"] = now
        _upsert_meta(conn, f"USER#{args.github_user_id}", item)
        print(f"seeded USER#{args.github_user_id} ({args.login}) role={args.role} "
              f"tier={args.tier} allowlisted=True")

        if args.install_id is not None:
            inst_pk = f"INST#{args.install_id}"
            existing_inst = _fetch_meta(conn, inst_pk)
            inst_item = {
                **existing_inst,
                "account_login": args.account_login or args.login,
                "account_type": args.account_type,
                "installed_at": existing_inst.get("installed_at") or now,
                "installed_by_user_id": str(args.github_user_id),
            }
            _upsert_meta(
                conn, inst_pk, inst_item,
                gsi1pk=str(args.github_user_id), gsi1sk=inst_pk,
            )
            print(f"seeded INST#{args.install_id} "
                  f"installed_by_user_id={args.github_user_id}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
