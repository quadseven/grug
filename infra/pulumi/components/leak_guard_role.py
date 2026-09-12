"""Read-only GitHub Actions role for the private-leaks guard (grug#921).

WHY A SECOND ROLE AT ALL
------------------------
`guard.private-leaks.yml` needs exactly one thing from AWS: the value of one
SSM SecureString holding the terms that have no generic SHAPE - people,
products, host codenames. Everything else the guard does is offline.

It deliberately does NOT reuse `grug-gha-deploy`. That role can write SSM,
create IAM principals and read/write every `grug-*` bucket and table, and the
guard runs on `pull_request` - so trusting the deploy role from a pull_request
subject would hand full deploy rights to anyone who can open a PR against this
repo. That is the same escalation the deploy role's `feat/*` branch trust was
retired for (audit #2, see oidc_role.create's docstring in __main__.py).

TRUST
-----
Only the `pull_request` OIDC subject, which is the ONLY event this workflow
runs on. A fork PR cannot reach this role at all: GitHub gives fork-triggered
`pull_request` runs read-only token permissions, so `id-token: write` is never
granted and no OIDC token can be minted. The workflow says so explicitly and
fails rather than running with its people/product layer off.

Both the name-based and the ID-anchored subject shapes are trusted, for the
same reason oidc_role.py emits both: GitHub's default subject embeds immutable
owner/repo numeric IDs, and those are the ones a real token carries today.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pulumi_aws as aws


@dataclass
class LeakGuardRole:
    """Role plus its inline policy.

    The policy is returned, not swallowed, so a test can await ITS urn: the
    RolePolicy registers after the Role, and awaiting only the role captures a
    stack where the policy does not exist yet (a vacuously-passing scope test
    is exactly the kind of thing this repo keeps finding).
    """

    role: aws.iam.Role
    policy: aws.iam.RolePolicy


def create(
    name: str,
    deny_list_param: str,
    repos: list[str],
) -> LeakGuardRole:
    """A role that can read exactly one SSM SecureString and nothing else.

    `deny_list_param` is the SSM parameter NAME (leading slash), e.g.
    `/grug/leak-guard-deny-list`. Only the name is ever in this public repo -
    the VALUES live in the SecureString and are the thing being protected.
    `repos` holds every OIDC repo shape to trust (name-based + ID-anchored).
    """
    account_id = aws.get_caller_identity().account_id
    provider_arn = (
        f"arn:aws:iam::{account_id}:oidc-provider/"
        f"token.actions.githubusercontent.com"
    )

    assume = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Federated": provider_arn},
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {
                        # StringEquals, not StringLike: there is no wildcard to
                        # express here, and an exact match cannot be widened by
                        # a stray `*` in a future edit.
                        "StringEquals": {
                            "token.actions.githubusercontent.com:aud": (
                                "sts.amazonaws.com"
                            ),
                            "token.actions.githubusercontent.com:sub": [
                                f"repo:{r}:pull_request" for r in repos
                            ],
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
        tags={"app": "grug", "purpose": "leak-guard"},
    )

    policy_doc = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    # ONE parameter. Not `/grug/*` - that path holds the
                    # GitHub App private key and the database URL, and this
                    # role is assumable from any pull_request in the repo.
                    "Effect": "Allow",
                    "Action": ["ssm:GetParameter"],
                    "Resource": [
                        f"arn:aws:ssm:*:{account_id}:parameter{deny_list_param}"
                    ],
                },
                {
                    # The deny-list is a SecureString under the AWS-managed
                    # `alias/aws/ssm` key, so GetParameter --with-decryption
                    # needs kms:Decrypt. Constrained to calls that arrive
                    # THROUGH SSM: this cannot be used to decrypt anything
                    # else in the account.
                    "Effect": "Allow",
                    "Action": ["kms:Decrypt"],
                    "Resource": "*",
                    "Condition": {
                        "StringLike": {"kms:ViaService": "ssm.*.amazonaws.com"},
                    },
                },
            ],
        },
    )

    policy = aws.iam.RolePolicy(f"{name}-policy", role=role.id, policy=policy_doc)
    return LeakGuardRole(role=role, policy=policy)
