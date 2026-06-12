"""Scoped IAM user for the Kubernetes pods (#354 self-hosting migration).

The k8s pods keep using AWS services after the move (queue dispatch,
credential-envelope crypto, runtime secret loads from the parameter
store) but have no OIDC identity off-Lambda - they authenticate with a
long-lived access key delivered via the deploy workflow's secret seed.

Least privilege: read /grug/* parameters (+ the SSM decrypt path), use
the grug queues, and use the envelope KMS key. Nothing else - the pods
must not be able to widen their own access.

Rotation: taint/replace the AccessKey resource (`pulumi up` re-mints),
then re-run the deploy workflow so the k8s secret re-seeds.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pulumi
import pulumi_aws as aws


@dataclass(frozen=True, slots=True)
class K8sPodUserBundle:
    user: aws.iam.User
    access_key: aws.iam.AccessKey


def create(
    *,
    queue_arns: list[pulumi.Input[str]],
    kms_key_arn: pulumi.Input[str],
    name: str = "grug-k8s-pod",
) -> K8sPodUserBundle:
    """Provision the pod user + scoped policy + access key, landing the
    key pair in /grug/k8s-pod-aws-* SecureStrings for the deploy seed."""
    user = aws.iam.User(
        name,
        name=name,
        tags={"managed_by": "pulumi", "project": "grug", "purpose": "k8s-pod-runtime"},
    )

    policy_doc = pulumi.Output.all(
        queues=pulumi.Output.all(*queue_arns), kms=kms_key_arn
    ).apply(
        lambda args: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "ReadGrugParams",
                        "Effect": "Allow",
                        "Action": [
                            "ssm:GetParameter",
                            "ssm:GetParameters",
                            "ssm:GetParametersByPath",
                        ],
                        "Resource": [
                            "arn:aws:ssm:us-east-1:*:parameter/grug/*",
                            # Shared LLM keys the Elder backends read at
                            # runtime (paths already public in this repo).
                            "arn:aws:ssm:us-east-1:*:parameter/infra/llm/openrouter_api_key",
                            "arn:aws:ssm:us-east-1:*:parameter/infra/llm/poolside_api_key",
                        ],
                    },
                    {
                        # TRANSITIONAL (#354): the services are
                        # Postgres-only post-swap; this grant remains
                        # ONLY so the one-shot DDB->PG prestage job can
                        # re-run at cutover for data freshness. REMOVE
                        # in the retirement PR with the table itself.
                        "Sid": "TransitionalDdbAccess",
                        "Effect": "Allow",
                        "Action": [
                            "dynamodb:GetItem",
                            "dynamodb:PutItem",
                            "dynamodb:UpdateItem",
                            "dynamodb:DeleteItem",
                            "dynamodb:Query",
                            "dynamodb:Scan",
                        ],
                        "Resource": [
                            "arn:aws:dynamodb:us-east-1:*:table/grug-main",
                            "arn:aws:dynamodb:us-east-1:*:table/grug-main/index/*",
                        ],
                    },
                    {
                        "Sid": "DecryptSsmSecureStrings",
                        "Effect": "Allow",
                        "Action": ["kms:Decrypt"],
                        "Resource": "*",
                        "Condition": {
                            "StringEquals": {
                                "kms:ViaService": "ssm.us-east-1.amazonaws.com"
                            }
                        },
                    },
                    {
                        "Sid": "UseGrugQueues",
                        "Effect": "Allow",
                        "Action": [
                            "sqs:SendMessage",
                            "sqs:ReceiveMessage",
                            "sqs:DeleteMessage",
                            "sqs:GetQueueAttributes",
                        ],
                        "Resource": args["queues"],
                    },
                    {
                        "Sid": "EnvelopeCrypto",
                        "Effect": "Allow",
                        "Action": ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
                        "Resource": args["kms"],
                    },
                ],
            }
        )
    )
    aws.iam.UserPolicy(
        f"{name}-policy",
        name=f"{name}-runtime",
        user=user.name,
        policy=policy_doc,
    )

    access_key = aws.iam.AccessKey(f"{name}-key", user=user.name)

    aws.ssm.Parameter(
        f"{name}-akid-ssm",
        name="/grug/k8s-pod-aws-access-key-id",
        type="SecureString",
        value=access_key.id,
        description="k8s pod runtime AWS access key id (#354). Rotation: replace the AccessKey resource.",
    )
    aws.ssm.Parameter(
        f"{name}-secret-ssm",
        name="/grug/k8s-pod-aws-secret-access-key",
        type="SecureString",
        value=pulumi.Output.secret(access_key.secret),
        description="k8s pod runtime AWS secret access key (#354).",
    )

    return K8sPodUserBundle(user=user, access_key=access_key)
