"""Scheduled (cron) Lambda factory — the repo's first EventBridge-triggered
Lambda (#247b). Establishes the pattern siblings can reuse.

Differs from `lambda_service` (the HTTP factory) in three ways that matter:
  1. NO Function URL. A cron Lambda with a public `AuthType=NONE` URL would
     let anyone HTTP-invoke the job — the EventBridge-shaped handler would run
     on any unauthenticated hit. The ONLY trigger is the IAM-gated EventBridge
     rule created here.
  2. DynamoDB read+update perms (Scan/Query/GetItem/UpdateItem) — the reaction
     poller enumerates installs (Scan), lists CommentRecords (Query), and
     advances `last_verdict` (UpdateItem). No Put/Delete.
  3. An `aws.cloudwatch.EventRule` (rate schedule) + `EventTarget` + the
     `aws.lambda_.Permission` that lets `events.amazonaws.com` invoke it.

Handler selection reuses the webhook container image as-is: the image CMD is
the `datadog_lambda` wrapper, which dispatches to whatever `DD_LAMBDA_HANDLER`
names — so the caller passes `DD_LAMBDA_HANDLER=poller_handler.handler` in
`env_vars` (and a distinct `DD_SERVICE`) and gets DD APM tracing for free. No
separate image build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pulumi
import pulumi_aws as aws

from components._types import SsmSecretRef


@dataclass
class ScheduledLambda:
    function: aws.lambda_.Function
    role: aws.iam.Role
    log_group: aws.cloudwatch.LogGroup
    rule: aws.cloudwatch.EventRule


def create(
    name: str,
    *,
    ecr_repo: aws.ecr.Repository,
    image_tag: str,
    schedule_expression: str,
    env: str,
    env_vars: dict[str, str],
    table_arn: pulumi.Input[str],
    extra_ssm_secrets: list[SsmSecretRef],
    timeout_seconds: int = 120,
    memory_mb: int = 512,
    env_vars_kms_key_arn: pulumi.Input[str] | None = None,
    iam_propagation_wait: pulumi.Resource | None = None,
) -> ScheduledLambda:
    """Build a cron-triggered Lambda from the (shared webhook) image +
    its EventBridge schedule. `schedule_expression` is an EventBridge rate
    or cron expr (e.g. `rate(15 minutes)`). `table_arn` is granted DDB
    read+update; `extra_ssm_secrets` are granted `ssm:GetParameter`."""
    log_group = aws.cloudwatch.LogGroup(
        f"{name}-logs",
        name=f"/aws/lambda/{name}",
        retention_in_days=14,
        tags={"app": "grug", "service": name},
    )

    role = aws.iam.Role(
        f"{name}-role",
        name=f"{name}-role",
        assume_role_policy=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        }),
        tags={"app": "grug", "service": name},
    )

    aws.iam.RolePolicy(
        f"{name}-logs-policy",
        role=role.id,
        policy=log_group.arn.apply(lambda arn: json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": [arn, f"{arn}:*"],
            }],
        })),
    )

    # SSM read on the GitHub-App SecureStrings the poller needs for install
    # token minting (+ decrypt via the AWS-managed aws/ssm key).
    secret_arns = [s.arn for s in extra_ssm_secrets]
    aws.iam.RolePolicy(
        f"{name}-ssm-policy",
        role=role.id,
        policy=pulumi.Output.all(*secret_arns).apply(lambda arns: json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["ssm:GetParameter", "ssm:GetParameters"],
                    "Resource": list(arns),
                },
                {
                    "Effect": "Allow",
                    "Action": "kms:Decrypt",
                    "Resource": "*",
                    "Condition": {"StringEquals": {
                        "kms:ViaService": "ssm.us-east-1.amazonaws.com",
                    }},
                },
            ],
        })),
    )

    # DDB read+update — Scan (list_allowlisted_installs), Query
    # (list_comment_records), GetItem (is_install_allowlisted), UpdateItem
    # (update_comment_record_reaction). No Put/Delete (the poller never
    # creates or removes rows).
    aws.iam.RolePolicy(
        f"{name}-ddb-policy",
        role=role.id,
        policy=pulumi.Output.from_input(table_arn).apply(lambda arn: json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:UpdateItem",
                ],
                "Resource": [arn, f"{arn}/index/*"],
            }],
        })),
    )

    image_uri = pulumi.Output.concat(ecr_repo.repository_url, ":", image_tag)
    function_opts = (
        pulumi.ResourceOptions(depends_on=[iam_propagation_wait])
        if iam_propagation_wait is not None else None
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
        kms_key_arn=env_vars_kms_key_arn,
        environment=aws.lambda_.FunctionEnvironmentArgs(variables=env_vars),
        tags={"app": "grug", "service": name, "env": env, "team": "grug"},
        opts=function_opts,
    )

    # EventBridge schedule → invoke the Lambda on a fixed cadence.
    rule = aws.cloudwatch.EventRule(
        f"{name}-schedule",
        name=f"{name}-schedule",
        schedule_expression=schedule_expression,
        description=f"grug {name} cron ({schedule_expression})",
        tags={"app": "grug", "service": name},
    )
    aws.cloudwatch.EventTarget(
        f"{name}-target",
        rule=rule.name,
        arn=function.arn,
    )
    # Without this, EventBridge silently fails to invoke (AccessDenied) — the
    # rule fires but the Lambda never runs, with no error surfaced to the rule.
    aws.lambda_.Permission(
        f"{name}-allow-eventbridge",
        action="lambda:InvokeFunction",
        function=function.name,
        principal="events.amazonaws.com",
        source_arn=rule.arn,
    )

    return ScheduledLambda(
        function=function, role=role, log_group=log_group, rule=rule,
    )
