"""Customer-managed KMS key for grug user-token envelope encryption.

Annual rotation enabled. Key policy grants the AWS account root
admin rights (canonical) plus the api Lambda role explicit Generate
+ Decrypt — webhook Lambda intentionally NOT granted (it never reads
user OAuth tokens; uses GitHub App JWT instead, per PRD).

Per memory `feedback_aws_default_paid_kms_pitfall` — table SSE stays
default-default (AWS-OWNED, free); per-row real crypto happens via the
envelope adapter at services/api/crypto/kms_envelope.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pulumi
import pulumi_aws as aws


@dataclass
class GrugTokensCmk:
    key: aws.kms.Key
    alias: aws.kms.Alias
    arn: pulumi.Output[str]


def create(name: str = "grug-tokens") -> GrugTokensCmk:
    account_id = aws.get_caller_identity().account_id

    key = aws.kms.Key(
        name,
        description="Per-user envelope DEK wrapping for grug OAuth tokens (PRD #21)",
        enable_key_rotation=True,
        rotation_period_in_days=365,
        deletion_window_in_days=14,
        policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AccountRootAdmin",
                        "Effect": "Allow",
                        "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                        "Action": "kms:*",
                        "Resource": "*",
                    },
                ],
            },
        ),
        tags={"app": "grug", "purpose": "user-token-envelope"},
    )

    alias = aws.kms.Alias(
        f"{name}-alias",
        name=f"alias/{name}",
        target_key_id=key.id,
    )

    return GrugTokensCmk(key=key, alias=alias, arn=key.arn)


def grant_use_to_role(
    cmk: GrugTokensCmk,
    role: aws.iam.Role,
    statement_id: str,
) -> aws.iam.RolePolicy:
    """Grant a Lambda role kms:GenerateDataKey + kms:Decrypt on this CMK."""
    return aws.iam.RolePolicy(
        statement_id,
        role=role.id,
        policy=cmk.arn.apply(
            lambda arn: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "kms:GenerateDataKey",
                                "kms:Decrypt",
                            ],
                            "Resource": arn,
                        },
                    ],
                },
            ),
        ),
    )
