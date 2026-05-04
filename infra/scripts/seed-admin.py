#!/usr/bin/env python3
"""Seed an admin user row in grug-main DDB.

Usage:
    AWS_PROFILE=... python3 infra/scripts/seed-admin.py \\
        --github-user-id 59060157 --login githumps

Idempotent: re-running on an existing row preserves OAuth blobs and
created_at, only flips role + tier + allowlisted to admin/lifetime/true.
Defaults match locked PRD: admin = Evan + GF; tier = lifetime.

Slice 5 #26 — required to unblock the webhook allowlist gate. Without
this, the webhook will no_op every PR until at least one admin row
exists with allowlisted=true.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import boto3


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--github-user-id", required=True, type=int,
                   help="GitHub numeric user ID (curl https://api.github.com/users/<login> | jq .id)")
    p.add_argument("--login", required=True, help="GitHub login (e.g. githumps)")
    p.add_argument("--table", default="grug-main")
    p.add_argument("--region", default="us-east-1")
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

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    pk = f"USER#{args.github_user_id}"
    now = datetime.now(timezone.utc).isoformat()

    existing = table.get_item(Key={"PK": pk, "SK": "META"}).get("Item") or {}

    item = {
        **existing,
        "PK": pk,
        "SK": "META",
        "login": args.login,
        "role": args.role,
        "tier": args.tier,
        "allowlisted": True,
        "allowlisted_at": existing.get("allowlisted_at") or now,
        "allowlisted_by": existing.get("allowlisted_by") or args.allowlisted_by,
    }
    if "created_at" not in item:
        item["created_at"] = now

    table.put_item(Item=item)
    print(f"seeded USER#{args.github_user_id} ({args.login}) role={args.role} "
          f"tier={args.tier} allowlisted=True")

    if args.install_id is not None:
        inst_pk = f"INST#{args.install_id}"
        existing_inst = table.get_item(
            Key={"PK": inst_pk, "SK": "META"}
        ).get("Item") or {}
        inst_item = {
            **existing_inst,
            "PK": inst_pk,
            "SK": "META",
            "account_login": args.account_login or args.login,
            "account_type": args.account_type,
            "installed_at": existing_inst.get("installed_at") or now,
            "installed_by_user_id": str(args.github_user_id),
            "GSI1PK": str(args.github_user_id),
            "GSI1SK": inst_pk,
        }
        table.put_item(Item=inst_item)
        print(f"seeded INST#{args.install_id} "
              f"installed_by_user_id={args.github_user_id}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
