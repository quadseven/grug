"""DynamoDB single-table factory.

PRD #21 single-table schema:
  USER#<gh_id>     META            (login, role, tier, allowlisted, oauth_*)
  INST#<id>        META            (account_login, account_type, installed_at)
  INST#<id>        REPO#<repo_id>  (roles_enabled, persona_overrides)

GSI1 lets us query "list installations for user" without scanning:
  GSI1PK = installed_by_user_id, GSI1SK = INST#<install_id>

CRITICAL — NO `server_side_encryption` block:
Per memory `reference_aws_default_paid_kms_pitfall`: omitting the SSE
block keeps the table on free AWS-OWNED encryption. Setting
`enabled=True` without `kms_key_arn` flips to paid AWS-MANAGED SSE
(~$1.50/mo). Real per-row encryption happens app-side via the KMS
envelope adapter (services/api/crypto/kms_envelope.py) which encrypts
the oauth_access_token blob before it ever reaches DDB.
"""

from __future__ import annotations

import pulumi
import pulumi_aws as aws


def create(name: str = "grug-main") -> aws.dynamodb.Table:
    return aws.dynamodb.Table(
        name,
        name=name,
        billing_mode="PAY_PER_REQUEST",
        hash_key="PK",
        range_key="SK",
        attributes=[
            aws.dynamodb.TableAttributeArgs(name="PK", type="S"),
            aws.dynamodb.TableAttributeArgs(name="SK", type="S"),
            aws.dynamodb.TableAttributeArgs(name="GSI1PK", type="S"),
            aws.dynamodb.TableAttributeArgs(name="GSI1SK", type="S"),
        ],
        global_secondary_indexes=[
            aws.dynamodb.TableGlobalSecondaryIndexArgs(
                name="GSI1",
                hash_key="GSI1PK",
                range_key="GSI1SK",
                projection_type="ALL",
            ),
        ],
        # PITR for daily snapshots — users table compromise = catastrophic.
        point_in_time_recovery=aws.dynamodb.TablePointInTimeRecoveryArgs(
            enabled=True,
        ),
        # DDB TTL on the `ttl` attribute (epoch seconds). Bounds the
        # ever-growing partitions whose rows write a `ttl`: `CRCOMMENT#`
        # (reaction-poll comment records, #247) and `DELIVERY#` (async-Elder
        # idempotency claims, #272). Without this, those `ttl` attributes are
        # inert and the partitions grow unbounded (runtime-trace audit on
        # #272: TTL was asserted-but-never-enabled — live table was DISABLED).
        # Enabling is an in-place UpdateTimeToLive — non-destructive. Free.
        ttl=aws.dynamodb.TableTtlArgs(attribute_name="ttl", enabled=True),
        tags={"app": "grug", "managed-by": "pulumi"},
    )
