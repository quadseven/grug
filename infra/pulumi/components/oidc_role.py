"""GitHub Actions OIDC trust + deploy role.

Per `feedback_prefer_ssm_over_1p`: no long-lived AWS access keys in repo
secrets. GHA assumes this role via OIDC each deploy.

Trust is scoped to a specific repo + ref pattern (branches + tags) so a
fork or PR from a different repo can't assume the role.
"""

from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws


def _ensure_oidc_provider() -> str:
    """Return ARN of the well-known GitHub OIDC provider.

    The token.actions.githubusercontent.com provider is account-wide
    (only one allowed per AWS account). ARN is deterministic from the
    account ID — no SDK lookup needed. somatic-scripts pulumi created
    this resource already; we only reference it here.

    If the provider doesn't exist (fresh account), the assume-role
    policy will be created but the trust will reject all OIDC calls
    until the provider is registered. Per docs/HITL_PREREQUISITES.md
    step 4, verify with `aws iam list-open-id-connect-providers`.
    """
    account_id = aws.get_caller_identity().account_id
    return (
        f"arn:aws:iam::{account_id}:oidc-provider/"
        f"token.actions.githubusercontent.com"
    )


def create(
    name: str,
    repo: str,
    branches: list[str],
    tags_pattern: str | None = None,
) -> aws.iam.Role:
    provider_arn = _ensure_oidc_provider()

    sub_patterns = [f"repo:{repo}:ref:refs/heads/{b}" for b in branches]
    if tags_pattern:
        sub_patterns.append(f"repo:{repo}:ref:refs/tags/{tags_pattern}")

    assume = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Federated": provider_arn},
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {
                        "StringEquals": {
                            "token.actions.githubusercontent.com:aud": (
                                "sts.amazonaws.com"
                            ),
                        },
                        "StringLike": {
                            "token.actions.githubusercontent.com:sub": (
                                sub_patterns
                            ),
                        },
                    },
                },
            ],
        },
    )

    role = aws.iam.Role(
        name,
        name=name,
        assume_role_policy=assume,
        max_session_duration=3600,
        tags={"app": "grug", "purpose": "gha-deploy"},
    )

    # Permissions: PowerUser-equivalent for the resources Pulumi creates,
    # scoped down later. For Slice 1 — Lambda + ECR + IAM (for role
    # creation) + SSM read + CloudWatch + Cloudflare-via-API.
    #
    # SSM read explicitly includes `/shared/*` so CI can fetch the
    # cross-repo Pulumi access token (per githumps/infrastructure#164
    # SSM convention — `/shared/<token>` is the cross-cutting namespace).
    aws.iam.RolePolicy(
        f"{name}-policy",
        role=role.id,
        policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "lambda:*",
                            "ecr:*",
                            "iam:*",
                            "logs:*",
                            "ssm:GetParameter*",
                            "ssm:DescribeParameters",
                            "cloudwatch:*",
                            "sts:GetCallerIdentity",
                            "kms:Decrypt",
                            # Lambda eagerly encrypts env vars on the
                            # CALLING principal's behalf when kms_key_arn
                            # is set on the function. Without kms:Encrypt
                            # on the deployer role, UpdateFunctionConfig
                            # 403s. Closes #60.
                            #
                            # kms:CreateGrant — required by AWS docs
                            # for "configuring a customer managed key on
                            # a Lambda function". Lambda needs the grant
                            # to encrypt/decrypt during invocations.
                            # Greptile P1 PR #79 (defensive: pulumi up
                            # works without it, but AWS docs are
                            # explicit it should be present).
                            "kms:Encrypt",
                            "kms:CreateGrant",
                            "kms:DescribeKey",
                            # Lambda's UpdateFunctionConfiguration on a
                            # CMK-protected function performs an upfront
                            # GenerateDataKey check using the calling
                            # principal's perms. Verified mid-loop on
                            # run 25310220972 (failed step 10, succeeded
                            # step 10c after IAM-retry sleep). Adding
                            # defensively so cold-account first deploys
                            # don't repeat the chicken-egg.
                            "kms:GenerateDataKey",
                        ],
                        # NOTE: tightening to specific resource ARNs is a
                        # follow-up. Slice 1 prioritizes "deploy works".
                        "Resource": "*",
                    },
                ],
            },
        ),
    )
    return role
