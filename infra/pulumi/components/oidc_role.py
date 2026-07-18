"""GitHub Actions OIDC trust + deploy role.

Per `feedback_prefer_ssm_over_1p`: no long-lived AWS access keys in repo
secrets. GHA assumes this role via OIDC each deploy.

Trust is scoped to a specific repo + ref pattern (branches + tags) so a
fork or PR from a different repo can't assume the role.

IAM eventual-consistency: when this role's RolePolicy is updated AND in
the same `pulumi up` run a Lambda is configured to use the new perms
(e.g. kms_key_arn requires kms:Encrypt + kms:GenerateDataKey on the
caller), AWS auth checks may still see the old policy for 10-30s. Closes
issue #88 — replaces the workflow-layer retry hack with an in-IaC
`pulumiverse_time.Sleep` whose triggers re-fire on policy content
change. Local + CI pulumi-up share the same waiter.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pulumi_aws as aws
import pulumiverse_time as ptime


def _ensure_oidc_provider() -> str:
    """Return ARN of the well-known GitHub OIDC provider.

    The token.actions.githubusercontent.com provider is account-wide
    (only one allowed per AWS account). ARN is deterministic from the
    account ID — no SDK lookup needed. A prior Pulumi stack in this
    account created this resource already; we only reference it here.

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


@dataclass
class DeployRole:
    """Bundle returned by `create()` so the composition root can wire the
    `iam_propagation_wait` resource into Lambda Function `depends_on`."""

    role: aws.iam.Role
    iam_propagation_wait: ptime.Sleep


def create(
    name: str,
    repo: str,
    branches: list[str],
    tags_pattern: str | None = None,
    environments: list[str] | None = None,
    immutable_repo: str | None = None,
) -> DeployRole:
    provider_arn = _ensure_oidc_provider()

    # `immutable_repo`: the ID-anchored `<owner>@<owner_id>/<repo>@<repo_id>`
    # form of the SAME repo. GitHub's default OIDC sub now embeds immutable
    # owner+repo numeric IDs (`repo:quadseven@59060157/grug@1227364190:...`),
    # a platform change confirmed via CloudTrail during the githumps ->
    # quadseven account rename (2026-07-18) - the plain name-based subs no
    # longer match ANY token. We emit BOTH shapes (name for any legacy token
    # form, ID-anchored for the current default) so a live AWS patch of this
    # role isn't reverted the next time this stack runs `pulumi up`. The
    # ID-anchored form is also rename-proof: owner_id/repo_id never change.
    def _subs_for(r: str) -> list[str]:
        out = [f"repo:{r}:ref:refs/heads/{b}" for b in branches]
        if tags_pattern:
            out.append(f"repo:{r}:ref:refs/tags/{tags_pattern}")
        # Jobs that declare a GitHub `environment:` present a DIFFERENT OIDC
        # sub shape (`repo:<repo>:environment:<name>`), NOT the ref-based one.
        # Without an explicit entry STS rejects with "Not authorized to
        # perform sts:AssumeRoleWithWebIdentity" - hit live on the first
        # deploy.k8s dispatch (#354), same trap the infra repo documented.
        for env in environments or []:
            out.append(f"repo:{r}:environment:{env}")
        return out

    sub_patterns = _subs_for(repo)
    if immutable_repo:
        sub_patterns.extend(_subs_for(immutable_repo))

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

    # Least-privilege deploy permissions, scoped to what the prod stack
    # ACTUALLY manages (audit #2). Verified against the live stack
    # (`pulumi stack export`): IAM principals are grug-gha-deploy (role) +
    # grug-cave-connector / grug-k8s-pod (users) — all `grug-*` prefixed;
    # and NO Lambda / ECR / EventBridge / CloudWatch-Logs resources are
    # deployed (retired at the #354 k8s cutover; the container registry is
    # self-hosted, not ECR). Those service grants were therefore dead and
    # are removed. SSM reads include `/shared/*` so CI can fetch the
    # cross-repo Pulumi access token (`/shared/<token>` is the operator's
    # cross-cutting namespace); writes stay scoped to `/grug/*` below.
    deploy_policy_doc = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        # IAM lifecycle for the stack's OWN principals only.
                        # Replaces the former `iam:*` on `*`, which was
                        # privilege-escalation-complete (CreateAccessKey /
                        # AttachUserPolicy / CreateRole on ANY principal ->
                        # self-grant admin). Scoped to role/grug-* + user/grug-*.
                        "Effect": "Allow",
                        "Action": [
                            "iam:CreateRole", "iam:GetRole", "iam:DeleteRole",
                            "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
                            "iam:TagRole", "iam:UntagRole",
                            "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
                            "iam:ListRoleTags",
                            "iam:PutRolePolicy", "iam:GetRolePolicy",
                            "iam:DeleteRolePolicy",
                            "iam:CreateUser", "iam:GetUser", "iam:DeleteUser",
                            "iam:TagUser", "iam:UntagUser",
                            "iam:ListUserPolicies", "iam:ListAttachedUserPolicies",
                            "iam:ListUserTags",
                            "iam:PutUserPolicy", "iam:GetUserPolicy",
                            "iam:DeleteUserPolicy",
                            "iam:CreateAccessKey", "iam:DeleteAccessKey",
                            "iam:ListAccessKeys", "iam:GetAccessKeyLastUsed",
                            # #389 teardown: the AWS provider lists (and
                            # would remove) group memberships before
                            # DeleteUser - the retirement up died here,
                            # the audit-6 serial-denial class.
                            "iam:ListGroupsForUser", "iam:RemoveUserFromGroup",
                        ],
                        "Resource": [
                            "arn:aws:iam::*:role/grug-*",
                            "arn:aws:iam::*:user/grug-*",
                        ],
                    },
                    {
                        # SSM reads. The stack's own `/grug/*`, the cross-repo
                        # Pulumi token at `/shared/*`, AND the shared `/infra/*`
                        # params the PROGRAM reads at invoke time: the Datadog
                        # provider creds (/infra/datadog/api_key + app_key), the
                        # LLM keys (/infra/llm/*), and the Discord alert handle
                        # (/infra/discord/*). Missing /infra here 403s every
                        # `pulumi up` at program-eval before any apply - the
                        # first deploy that RUNS under the scoped role dies (the
                        # initial scope-down deploy passed only because it ran
                        # under the old broad policy and applied the narrow one
                        # last). Tested via simulate-custom-policy incl. these.
                        "Effect": "Allow",
                        "Action": ["ssm:GetParameter*"],
                        "Resource": [
                            "arn:aws:ssm:*:*:parameter/grug/*",
                            "arn:aws:ssm:*:*:parameter/shared/*",
                            "arn:aws:ssm:*:*:parameter/infra/datadog/*",
                            "arn:aws:ssm:*:*:parameter/infra/llm/*",
                            "arn:aws:ssm:*:*:parameter/infra/discord/*",
                            # Roles Anywhere tenant ARNs (#388): deploy.k8s.yml
                            # seeds the trust-anchor/profile/role ARNs into the
                            # grug-aws-config ConfigMap from these paths.
                            # Read-only; the values are ARNs, not secrets.
                            "arn:aws:ssm:*:*:parameter/infra/roles-anywhere/*",
                        ],
                    },
                    {
                        # Account-global actions that have no resource-level
                        # scoping.
                        "Effect": "Allow",
                        "Action": [
                            "sts:GetCallerIdentity",
                            "ssm:DescribeParameters",
                        ],
                        "Resource": "*",
                    },
                    {
                        # KMS for the Pulumi awskms secrets provider (decrypts
                        # the stack's encrypted config on every op) + drift
                        # reads on the grug-tokens CMK. Real key access is
                        # gated by each key's KEY POLICY, so `*` here is the
                        # key-policy-gated minimum, not an escalation vector.
                        # The former Encrypt / CreateGrant / GenerateDataKey
                        # grants existed only for Lambda env-var encryption
                        # (retired) and are removed.
                        "Effect": "Allow",
                        "Action": ["kms:Decrypt", "kms:DescribeKey"],
                        "Resource": "*",
                    },
                    {
                        # Pulumi-managed SSM writes. Scoped tighter than
                        # the general policy above: only `/grug/*` paths
                        # the grug stack owns, never the cross-cutting
                        # `/shared/*` namespace (which is read-only from
                        # this role's POV — Pulumi for `/shared/*` lives
                        # in infrastructure/pulumi/aws-cicd-bootstrap).
                        #
                        # Spec 0013 (RumInstrumentation) needs PutParameter
                        # so the dd_rum component can persist the
                        # `datadog.RumApplication` ID + client token to
                        # SSM after creation. Caught when Pulumi #166's
                        # iac.deploy got past the DD scope check but
                        # failed with `AccessDeniedException: ssm:PutParameter`.
                        "Effect": "Allow",
                        "Action": [
                            "ssm:PutParameter",
                            "ssm:DeleteParameter",
                            "ssm:AddTagsToResource",
                            "ssm:RemoveTagsFromResource",
                            "ssm:ListTagsForResource",
                            "ssm:LabelParameterVersion",
                        ],
                        "Resource": "arn:aws:ssm:*:*:parameter/grug/*",
                    },
                    {
                        # Pulumi-managed DynamoDB table lifecycle. The deploy
                        # role had NO dynamodb perms — the grug-main table's
                        # create/PITR/GSI predate this role (broader bootstrap
                        # principal), so the gap stayed latent until #272's
                        # TTL-enable became the FIRST table MUTATION this role
                        # attempted → AccessDeniedException: UpdateTimeToLive
                        # (deploy run 26735432997; preview can't catch an
                        # apply-time auth check). Scoped to the grug-main table
                        # + its sub-resources (indexes/streams), not "*".
                        "Effect": "Allow",
                        "Action": "dynamodb:*",
                        "Resource": [
                            "arn:aws:dynamodb:*:*:table/grug-main",
                            "arn:aws:dynamodb:*:*:table/grug-main/*",
                        ],
                    },
                    {
                        # SQS for the cave-fallback airlock (#310) + the future
                        # rerun queue (#305). `pulumi preview` passes with admin
                        # creds, but the SCOPED deploy role 403'd on
                        # sqs:CreateQueue at apply time (deploy run 27137871238 —
                        # an apply-time auth check preview can't see). Scoped to
                        # grug-* queues, not "*". (lambda:* above already covers
                        # the event-source mapping; iam:* covers the connector
                        # user + policy.)
                        "Effect": "Allow",
                        "Action": "sqs:*",
                        "Resource": "arn:aws:sqs:*:*:grug-*",
                    },
                    {
                        # S3 for the cave spilled-diff bucket (#311). Same
                        # apply-time-auth lesson as the sqs grant above —
                        # preview (admin creds) passes, the scoped CI role 403s
                        # on create at apply. Scoped to grug-* buckets + their
                        # objects, not "*".
                        "Effect": "Allow",
                        "Action": "s3:*",
                        "Resource": [
                            "arn:aws:s3:::grug-*",
                            "arn:aws:s3:::grug-*/*",
                        ],
                    },
                    {
                        # The deploy.k8s secret-seed discovers the cave-diff
                        # bucket's (random-suffixed) name via `aws s3api
                        # list-buckets`. ListAllMyBuckets is ACCOUNT-level and
                        # only accepts Resource "*" - the grug-* bucket scope
                        # above can't grant it, so the seed silently got an
                        # empty GRUG_CAVE_DIFF_BUCKET under the scoped role
                        # (caught by the seed's empty-value guard). List bucket
                        # NAMES only; no data access. (Cleaner future: export
                        # the bucket name to SSM and drop this.)
                        "Effect": "Allow",
                        "Action": "s3:ListAllMyBuckets",
                        "Resource": "*",
                    },
                ],
            },
        )
    deploy_policy = aws.iam.RolePolicy(
        f"{name}-policy",
        role=role.id,
        policy=deploy_policy_doc,
    )

    # Hash the policy doc so a content change re-triggers the Sleep
    # (which forces a 45s wait before any depends_on Lambda update).
    # Per pulumiverse_time docs, `triggers` change → resource replace →
    # `create_duration` re-fires.
    policy_hash = hashlib.sha256(deploy_policy_doc.encode()).hexdigest()
    iam_propagation_wait = ptime.Sleep(
        f"{name}-iam-propagation",
        create_duration="45s",
        triggers={
            "role_policy_id": deploy_policy.id,
            # Content hash so policy edits (not just resource id) refresh
            # the wait. Without this, in-place policy.update wouldn't
            # cause the Sleep to refire.
            "policy_sha256": policy_hash,
        },
    )

    return DeployRole(role=role, iam_propagation_wait=iam_propagation_wait)
