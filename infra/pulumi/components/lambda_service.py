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

from components._types import SsmSecretRef


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
    extra_ssm_secrets: list[SsmSecretRef] | None = None,
    cors_allow_origins: list[str] | None = None,
    cors_allow_methods: list[str] | None = None,
    cors_allow_headers: list[str] | None = None,
    cors_allow_credentials: bool = False,
    env_vars_kms_key_arn: pulumi.Input[str] | None = None,
    iam_propagation_wait: pulumi.Resource | None = None,
    allow_self_invoke: bool = False,
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

    # Self-invoke grant (#272): the webhook offloads the Elder LLM review
    # off the ACK path by invoking ITSELF asynchronously
    # (InvocationType="Event"). The execution role therefore needs
    # `lambda:InvokeFunction` on its own ARN. We construct the ARN from
    # the function NAME (== `name`, set below) + account + region rather
    # than referencing the Function resource, which would be a circular
    # dependency (role → policy → function → role). The runtime targets
    # the same name via the auto-set `AWS_LAMBDA_FUNCTION_NAME` env.
    self_invoke_policy: aws.iam.RolePolicy | None = None
    if allow_self_invoke:
        _ident = aws.get_caller_identity()
        _region = aws.get_region()
        _self_arn = f"arn:aws:lambda:{_region.name}:{_ident.account_id}:function:{name}"
        self_invoke_policy = aws.iam.RolePolicy(
            f"{name}-self-invoke-policy",
            role=role.id,
            policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "lambda:InvokeFunction",
                            "Resource": _self_arn,
                        },
                    ],
                },
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

    # When env_vars_kms_key_arn is set, Lambda encrypts ALL env vars
    # with that CMK at rest. `aws lambda get-function-configuration`
    # returns ciphertext blobs instead of plaintext, so a reader needs
    # BOTH `lambda:GetFunctionConfiguration` AND `kms:Decrypt` on the
    # CMK to recover values. Closes #60 — DD_API_KEY was previously
    # plaintext-visible to anyone with default ReadOnlyAccess Lambda
    # perms. Lambda runtime decrypts before extension boot, so DD ext
    # still reads DD_API_KEY normally.
    # `iam_propagation_wait` (issue #88) defers Lambda create/update
    # until the GHA deploy role's RolePolicy has propagated through
    # AWS IAM (10-30s typical). Without this, an in-same-plan addition
    # of `kms:Encrypt` + `kms:GenerateDataKey` to the deploy role +
    # Lambda kms_key_arn-touching update collides on AWS auth-check.
    # Gate the Function create/update behind both the IAM-propagation wait
    # (#88) AND the self-invoke RolePolicy (#272) — so new image code is never
    # live before the lambda:InvokeFunction-on-self grant lands (else the
    # first self-invokes AccessDeny → dropped reviews until the policy
    # propagates). codex peer-review WARN-4.
    _function_deps = [
        d for d in (iam_propagation_wait, self_invoke_policy) if d is not None
    ]
    function_opts = (
        pulumi.ResourceOptions(depends_on=_function_deps)
        if _function_deps
        else None
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
        kms_key_arn=env_vars_kms_key_arn,
        environment=aws.lambda_.FunctionEnvironmentArgs(
            variables=env_vars,
        ),
        # `env` tag is what DD monitors filter on (matches DD_ENV).
        # Lambda CloudWatch metrics propagate the resource's `env` tag
        # to DD, so without it `aws.lambda.duration{env:dev}` returns
        # zero series. Greptile P2 PR #48 — silent fallback to
        # `unknown` would have caused all DD monitors to flip to
        # `No Data` without visible error. Fail-fast at deploy time.
        tags={
            "app": "grug",
            "service": name,
            "env": env_vars["GRUG_ENV"],
            "team": "grug",
        },
        opts=function_opts,
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

    # Disable AWS's default async-invoke retries when this function
    # self-invokes (#272). The Elder worker already has its own
    # idempotency (delivery_id claim) + advisory-degrade contract, so an
    # AWS retry of a deterministically-failing async invocation adds no
    # value and risks a retry-storm. `EventInvokeConfig` governs ALL async
    # invocations of the function — safe here because the webhook's only
    # async invocations ARE these self-invokes (GitHub traffic is sync via
    # the Function URL). Belt-and-suspenders alongside `run_elder_job`'s
    # never-reraise guard.
    if allow_self_invoke:
        aws.lambda_.FunctionEventInvokeConfig(
            f"{name}-async-no-retry",
            function_name=function.name,
            maximum_retry_attempts=0,
        )

    return LambdaService(
        function=function,
        function_url=function_url.function_url,
        role=role,
        log_group=log_group,
    )
