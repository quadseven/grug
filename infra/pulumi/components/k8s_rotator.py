"""Scoped IAM user for the interim k8s key-rotator CronJob (#386).

Throwaway interim until Roles Anywhere lands (#388/#389). The rotator
CronJob mints a fresh access key for the grug-k8s-pod user, swaps it into
the grug-secrets Secret, rolls the consumers onto it, then deletes the old
key. THIS user is the AWS identity that does ONLY that:
CreateAccessKey / DeleteAccessKey / ListAccessKeys on the grug-k8s-pod
user, nothing else - it cannot widen its own access, touch any other
principal, or read data. Its own access key lands in
/grug/k8s-rotator-aws-* for the deploy seed (grug-rotator-secret).

When Roles Anywhere lands, this user + the CronJob are removed (#389).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pulumi
import pulumi_aws as aws


@dataclass(frozen=True, slots=True)
class K8sRotatorBundle:
    user: aws.iam.User
    access_key: aws.iam.AccessKey


def create(
    *,
    pod_user_arn: pulumi.Input[str],
    name: str = "grug-k8s-rotator",
) -> K8sRotatorBundle:
    """Provision the rotator user + a policy scoped to access-key ops on the
    pod user ONLY + the rotator's own access key in /grug/k8s-rotator-aws-*."""
    user = aws.iam.User(
        name,
        name=name,
        tags={"managed_by": "pulumi", "project": "grug", "purpose": "k8s-key-rotator"},
    )

    policy_doc = pulumi.Output.from_input(pod_user_arn).apply(
        lambda arn: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        # ONLY access-key lifecycle on the ONE pod user. No
                        # other IAM verb, no other resource - this principal
                        # cannot escalate or touch anything else.
                        "Sid": "RotatePodAccessKeyOnly",
                        "Effect": "Allow",
                        "Action": [
                            "iam:CreateAccessKey",
                            "iam:DeleteAccessKey",
                            "iam:ListAccessKeys",
                        ],
                        "Resource": arn,
                    }
                ],
            }
        )
    )
    aws.iam.UserPolicy(
        f"{name}-policy",
        name=f"{name}-rotate",
        user=user.name,
        policy=policy_doc,
    )

    access_key = aws.iam.AccessKey(f"{name}-key", user=user.name)

    aws.ssm.Parameter(
        f"{name}-akid-ssm",
        name="/grug/k8s-rotator-aws-access-key-id",
        type="SecureString",
        value=access_key.id,
        description="k8s key-rotator AWS access key id (#386 interim). Rotation: replace the AccessKey resource.",
    )
    aws.ssm.Parameter(
        f"{name}-secret-ssm",
        name="/grug/k8s-rotator-aws-secret-access-key",
        type="SecureString",
        value=pulumi.Output.secret(access_key.secret),
        description="k8s key-rotator AWS secret access key (#386 interim).",
    )

    return K8sRotatorBundle(user=user, access_key=access_key)
