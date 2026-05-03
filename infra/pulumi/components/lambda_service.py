"""Lambda + Function URL + log group factory.

Returns a `LambdaService` namespace-style object exposing the Function
URL string for the composition root to wire into Cloudflare DNS.

IAM scope is intentionally NARROW for Slice 1 — webhook Lambda only
needs `ssm:GetParameter` + `ssm:GetParameters` on the three pre-loaded
SecureStrings + decrypt via the AWS-managed `aws/ssm` key. NO DDB / KMS
permissions yet — those land in Slice 2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pulumi
import pulumi_aws as aws


@dataclass
class LambdaService:
    function: aws.lambda_.Function
    function_url: pulumi.Output[str]
    role: aws.iam.Role
    log_group: aws.cloudwatch.LogGroup


def create(
    name: str,
    ecr_repo: aws.ecr.Repository,
    image_tag: str,
    secrets: dict[str, aws.ssm.GetParameterResult],
    env_vars: dict[str, str],
    timeout_seconds: int = 15,
    memory_mb: int = 512,
    layers: list[str] | None = None,
    extra_ssm_secrets: list[aws.ssm.GetParameterResult] | None = None,
    cors_allow_origins: list[str] | None = None,
    cors_allow_methods: list[str] | None = None,
    cors_allow_headers: list[str] | None = None,
    cors_allow_credentials: bool = False,
) -> LambdaService:
    log_group = aws.cloudwatch.LogGroup(
        f"{name}-logs",
        name=f"/aws/lambda/{name}",
        retention_in_days=14,
        tags={"app": "grug", "service": name},
    )

    assume_role = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                },
            ],
        },
    )

    role = aws.iam.Role(
        f"{name}-role",
        name=f"{name}-role",
        assume_role_policy=assume_role,
        tags={"app": "grug", "service": name},
    )

    # CloudWatch Logs (least-privilege per log group, not *).
    aws.iam.RolePolicy(
        f"{name}-logs-policy",
        role=role.id,
        policy=log_group.arn.apply(
            lambda arn: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                            ],
                            "Resource": [arn, f"{arn}:*"],
                        },
                    ],
                },
            ),
        ),
    )

    # SSM read on the pre-loaded SecureStrings + any extras (e.g. shared
    # /shared/datadog-api-key for DD instrumentation).
    secret_arns = [s.arn for s in secrets.values()]
    if extra_ssm_secrets:
        secret_arns.extend(s.arn for s in extra_ssm_secrets)
    aws.iam.RolePolicy(
        f"{name}-ssm-policy",
        role=role.id,
        policy=pulumi.Output.all(*secret_arns).apply(
            lambda arns: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssm:GetParameter",
                                "ssm:GetParameters",
                            ],
                            "Resource": list(arns),
                        },
                        {
                            # Decrypt via AWS-managed aws/ssm key.
                            # When we move to a CMK in Slice 2 this
                            # tightens to that specific key ARN.
                            "Effect": "Allow",
                            "Action": "kms:Decrypt",
                            "Resource": "*",
                            "Condition": {
                                "StringEquals": {
                                    "kms:ViaService": (
                                        "ssm.us-east-1.amazonaws.com"
                                    ),
                                },
                            },
                        },
                    ],
                },
            ),
        ),
    )

    # Lambda Image-mode requires the source image live in a PRIVATE ECR
    # repo (rejects public.ecr.aws/* URIs). For first-ever deploy we
    # `crane copy public.ecr.aws/lambda/python:3.13-arm64 →
    # <our-ecr>/<repo>:bootstrap` so the Lambda can be created against
    # OUR repo even before CI builds anything. CI's first build pushes
    # the real image with a SHA tag and `pulumi up` swaps imageUri.
    image_uri = pulumi.Output.concat(
        ecr_repo.repository_url, ":", image_tag,
    )

    function = aws.lambda_.Function(
        name,
        name=name,
        package_type="Image",
        image_uri=image_uri,
        role=role.arn,
        timeout=timeout_seconds,
        memory_size=memory_mb,
        architectures=["arm64"],
        layers=layers or [],
        environment=aws.lambda_.FunctionEnvironmentArgs(
            variables=env_vars,
        ),
        # `env` tag is what DD monitors filter on (matches DD_ENV).
        # Lambda CloudWatch metrics propagate the resource's `env` tag
        # to DD, so without it `aws.lambda.duration{env:dev}` returns
        # zero series. Sourced from GRUG_ENV env var which Lambda always
        # gets (set in __main__.py).
        tags={
            "app": "grug",
            "service": name,
            "env": env_vars.get("GRUG_ENV", "unknown"),
            "team": "grug",
        },
    )

    # Lambda Function URL CORS — preflight handled by AWS *before* the
    # Lambda invokes (per memory `reference_lambda_cors_method_limit`),
    # so we must enumerate every method the SPA uses. Webhook defaults
    # to POST-only + wildcard origin (GitHub fires from many IPs);
    # api Lambda overrides with explicit origin + credentials so the
    # session cookie + cross-origin PUT (from grug.lol → api.grug.lol)
    # works. Browser rejects allow_origins=["*"] when credentials=True,
    # so the explicit-origin path is mandatory for the SPA.
    function_url = aws.lambda_.FunctionUrl(
        f"{name}-url",
        function_name=function.name,
        authorization_type="NONE",
        cors=aws.lambda_.FunctionUrlCorsArgs(
            allow_origins=cors_allow_origins or ["*"],
            allow_methods=cors_allow_methods or ["POST"],
            allow_headers=cors_allow_headers or [
                "content-type",
                "x-github-event",
                "x-github-delivery",
                "x-hub-signature-256",
            ],
            allow_credentials=cors_allow_credentials,
            max_age=86400,
        ),
    )

    return LambdaService(
        function=function,
        function_url=function_url.function_url,
        role=role,
        log_group=log_group,
    )
