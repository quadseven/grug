"""Composition root for Grug SaaS Pulumi project (PRD githumps/grug#21).

Per `feedback_pulumi_export_single_source` from the user's memory:
component factories return resources, this file alone exports stack outputs.

Slice 1 (#22) scope: SSM secrets, ECR repo, webhook Lambda, Function URL,
IAM, OIDC role for GHA, Cloudflare DNS for webhook.grug.lol. No DDB / KMS /
api Lambda yet — those land in Slice 2 (#23).

Single-source secrets: ALL secrets (including Cloudflare creds) live in
SSM Parameter Store. Pulumi config holds NO secrets — only env name +
domain. CF zone_id is read from SSM at preview/up time. CF API token is
read from SSM by the cloudflare provider via env var (set in this file's
provider config).
"""

from __future__ import annotations

import pulumi
import pulumi_aws as aws
import pulumi_cloudflare as cloudflare

from components import (
    cloudflare_dns,
    ddb_table,
    ecr_repo,
    kms_cmk,
    lambda_service,
    oidc_role,
    ssm_secrets,
)
# NOTE: CF Worker (grug-webhook-host-rewrite) is managed OUT OF BAND
# via infra/cloudflare/deploy.sh because pulumi-cloudflare's
# WorkerScript resource fails idempotency and main_module handling
# (mirrors the macchina-router decision in somatic-scripts).

config = pulumi.Config()
env = config.require("env")
domain = config.require("domain")

# All secrets pre-loaded in SSM by hand per docs/HITL_PREREQUISITES.md.
# Pulumi only references their ARNs to grant Lambda IAM read access; it
# never writes secret values (those stay opaque to Pulumi state).
secrets = ssm_secrets.reference_existing(
    name_prefix="/grug",
    parameters=[
        "github-app-id",
        "github-app-client-id",
        "github-app-client-secret",
        "github-app-private-key",
        "github-app-webhook-secret",
    ],
)

# Cloudflare creds — also from SSM (single-source rule). The cloudflare
# provider reads CLOUDFLARE_API_TOKEN from env when not configured
# explicitly; we configure it explicitly via SSM lookup so a fresh
# checkout doesn't need extra env-var setup.
_cf_token = aws.ssm.get_parameter(
    name="/grug/cloudflare-api-token",
    with_decryption=True,
)
_cf_zone_id = aws.ssm.get_parameter(
    name="/grug/cloudflare-zone-id",
    with_decryption=False,
)
_cf_account_id = aws.ssm.get_parameter(
    name="/grug/cloudflare-account-id",
    with_decryption=False,
)
cf_provider = cloudflare.Provider(
    "cloudflare-grug",
    api_token=_cf_token.value,
)

# Datadog API key — shared across projects per #164 SSM convention.
# Lambda extension layer reads DD_API_KEY env var to ship traces/logs.
_dd_api_key = aws.ssm.get_parameter(
    name="/shared/datadog-api-key",
    with_decryption=True,
)

# DD extension version baked into the Lambda image (per Dockerfile.lambda).
# Lambda Container package_type can't attach layers; extension binary is
# COPYd from public.ecr.aws/datadog/lambda-extension-arm:<v>. Bump here
# triggers an image rebuild via CI on next merge/dispatch.
_dd_extension_version = config.get("dd_extension_version") or "65"

# OIDC trust for GitHub Actions deploys from githumps/grug. Per
# `feedback_prefer_ssm_over_1p` — no long-lived AWS creds in the repo.
gha_deploy_role = oidc_role.create(
    name="grug-gha-deploy",
    repo="githumps/grug",
    # `main` and SaaS-conversion feature branches (epic-grug-saas).
    # Tighten back to `main` only once Slice 13 (#34) ships.
    branches=[
        "main", "feat/22-*", "feat/23-*", "feat/24-*",
        "feat/25-*", "feat/26-*", "feat/27-*",
    ],
    tags_pattern="v*",
)

# Slice 2 (#23) — DDB single-table + KMS CMK + api Lambda
grug_main_table = ddb_table.create("grug-main")
grug_tokens_cmk = kms_cmk.create("grug-tokens")

# ECR repo for the webhook Lambda image. Lifecycle: untagged images expire
# after 14 days (avoids ~$0.10/GB/mo image graveyard).
webhook_ecr = ecr_repo.create(
    name="grug-webhook",
    untagged_expire_days=14,
)

# Webhook Lambda + Function URL. Image tag wired in via CI build step
# (`pulumi up` consumes config value `webhook_image_tag`). Special tag
# "bootstrap" means: use the public AWS Lambda Python 3.13 arm64 image
# so the Lambda CREATES successfully even before our private ECR repo
# has any pushed images. Lambda invocations will error (handler not
# found in the base image) but the resource exists, the Function URL
# is live, CF DNS resolves. CI's first build replaces the image tag
# with the real SHA, second `pulumi up` swaps imageUri.
webhook_image_tag = config.get("webhook_image_tag") or "bootstrap"
webhook = lambda_service.create(
    name="grug-webhook",
    ecr_repo=webhook_ecr,
    image_tag=webhook_image_tag,
    secrets=secrets,
    extra_ssm_secrets=[_dd_api_key],
    # NOTE: DD extension is BAKED into the Lambda container image
    # (services/webhook/Dockerfile.lambda copies from
    # public.ecr.aws/datadog/lambda-extension-arm:<v>). Lambda Container
    # package_type rejects `layers`, so layer-attachment is unavailable.
    env_vars={
        "GRUG_ENV": env,
        "GRUG_LOG_LEVEL": "INFO",
        "GITHUB_APP_ID_SSM": secrets["github-app-id"].name,
        "GITHUB_APP_PRIVATE_KEY_SSM": secrets["github-app-private-key"].name,
        "GITHUB_APP_WEBHOOK_SECRET_SSM": secrets["github-app-webhook-secret"].name,
        # OAuth refs included now (Slice 3 #24 will consume them).
        # Lambda has IAM read on the params already; safe to inject.
        "GITHUB_APP_CLIENT_ID_SSM": secrets["github-app-client-id"].name,
        "GITHUB_APP_CLIENT_SECRET_SSM": secrets["github-app-client-secret"].name,
        # DDB allowlist gate (Slice 5 #26). Webhook reads INST# + USER#
        # rows directly (no KMS — token blobs are api-Lambda-only).
        "GRUG_DDB_TABLE": grug_main_table.name,
        # Datadog APM (datadog_lambda wrapper finds real handler via
        # DD_LAMBDA_HANDLER; layer adds the trace agent + log forwarder).
        "DD_LAMBDA_HANDLER": "lambda_handler.handler",
        "DD_SITE": "datadoghq.com",
        "DD_API_KEY": _dd_api_key.value,
        "DD_ENV": env,
        "DD_SERVICE": "grug-webhook",
        "DD_VERSION": webhook_image_tag,
        "DD_TRACE_ENABLED": "true",
        "DD_LOGS_INJECTION": "true",
        # Disable noisy ASGI integration that collapses every FastAPI
        # request into a single "ASGI request" trace span (per memory
        # `reference_dd_apm_asgi_resource_grouping`).
        "DD_PATCH_MODULES": "asgi:false",
        "DD_TRACE_ASGI_ENABLED": "false",
    },
    timeout_seconds=15,
    memory_mb=512,
)

# Cloudflare DNS — webhook.grug.lol → Lambda Function URL host
# (proxied through CF for TLS termination + WAF). Per memory
# `reference_lambda_function_url_host_volatile` the upstream URL changes
# on every recreate — single-source via Pulumi output below.
# Worker route + script managed via infra/cloudflare/deploy.sh (curl
# against CF API). Pulumi just creates the proxied DNS record so the
# Worker can intercept. Run the deploy.sh script after every Lambda
# Function URL host change (memory: reference_lambda_function_url_host_volatile).
cloudflare_dns.create_proxied_cname(
    zone_id=_cf_zone_id.value,
    name="webhook",
    domain=domain,
    target_url=webhook.function_url,
    provider=cf_provider,
    # Proxied=True so the Worker route intercepts and rewrites Host.
    proxied=True,
)

# API Lambda — separate ECR + Function URL from webhook (per Q10
# topology decision; independent concurrency reservations so webhook
# bursts can't starve user-interactive API).
api_ecr = ecr_repo.create(
    name="grug-api",
    untagged_expire_days=14,
)

api_image_tag = config.get("api_image_tag") or "bootstrap"
api_lambda = lambda_service.create(
    name="grug-api",
    ecr_repo=api_ecr,
    image_tag=api_image_tag,
    secrets=secrets,
    extra_ssm_secrets=[_dd_api_key],
    env_vars={
        "GRUG_ENV": env,
        "GRUG_LOG_LEVEL": "INFO",
        "GRUG_BUILD_SHA": api_image_tag,
        "GRUG_DOMAIN": domain,
        "GRUG_DDB_TABLE": grug_main_table.name,
        "GRUG_KMS_CMK_ARN": grug_tokens_cmk.arn,
        "GITHUB_APP_WEBHOOK_SECRET_SSM": secrets["github-app-webhook-secret"].name,
        # OAuth (Slice 3 #24 consumes)
        "GITHUB_APP_CLIENT_ID_SSM": secrets["github-app-client-id"].name,
        "GITHUB_APP_CLIENT_SECRET_SSM": secrets["github-app-client-secret"].name,
        # GitHub App auth for personas dispatch (Slice 4 #25 consumes)
        "GITHUB_APP_ID_SSM": secrets["github-app-id"].name,
        "GITHUB_APP_PRIVATE_KEY_SSM": secrets["github-app-private-key"].name,
        # DD APM
        "DD_LAMBDA_HANDLER": "lambda_handler.handler",
        "DD_SITE": "datadoghq.com",
        "DD_API_KEY": _dd_api_key.value,
        "DD_ENV": env,
        "DD_SERVICE": "grug-api",
        "DD_VERSION": api_image_tag,
        "DD_TRACE_ENABLED": "true",
        "DD_LOGS_INJECTION": "true",
        "DD_PATCH_MODULES": "asgi:false",
        "DD_TRACE_ASGI_ENABLED": "false",
    },
    timeout_seconds=15,
    memory_mb=512,
)

# Per IAM split (Slice 2 acceptance criterion): api Lambda CAN decrypt
# user OAuth tokens via KMS envelope. Webhook Lambda cannot — it never
# reads user tokens (uses GitHub App JWT instead).
kms_cmk.grant_use_to_role(
    cmk=grug_tokens_cmk,
    role=api_lambda.role,
    statement_id="grug-api-kms-policy",
)

# DDB IAM for api Lambda (read+write for v1; tighten per access pattern
# in later slices if needed).
import json as _json
import pulumi_aws as _aws
_aws.iam.RolePolicy(
    "grug-api-ddb-policy",
    role=api_lambda.role.id,
    policy=grug_main_table.arn.apply(
        lambda arn: _json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "dynamodb:GetItem",
                            "dynamodb:PutItem",
                            "dynamodb:UpdateItem",
                            "dynamodb:DeleteItem",
                            "dynamodb:Query",
                            "dynamodb:BatchGetItem",
                            "dynamodb:BatchWriteItem",
                        ],
                        "Resource": [arn, f"{arn}/index/*"],
                    },
                ],
            },
        ),
    ),
)

# DDB IAM for webhook Lambda (Slice 5 #26 allowlist gate). Tighter
# scope than api: GetItem + PutItem + DeleteItem only (no Query, no
# UpdateItem — webhook only records install rows + reads INST/USER for
# allowlist checks). Explicitly NO KMS perms — token blobs stay
# api-Lambda-only per locked encryption decision.
_aws.iam.RolePolicy(
    "grug-webhook-ddb-policy",
    role=webhook.role.id,
    policy=grug_main_table.arn.apply(
        lambda arn: _json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "dynamodb:GetItem",
                            "dynamodb:PutItem",
                            "dynamodb:DeleteItem",
                        ],
                        "Resource": [arn],
                    },
                ],
            },
        ),
    ),
)

# CF DNS — api.grug.lol → api Lambda Function URL (proxied; Worker
# rewrites Host header just like webhook). Worker deployed via
# infra/cloudflare/deploy.sh.
cloudflare_dns.create_proxied_cname(
    zone_id=_cf_zone_id.value,
    name="api",
    domain=domain,
    target_url=api_lambda.function_url,
    provider=cf_provider,
    proxied=True,
)

pulumi.export("webhook_function_url", webhook.function_url)
pulumi.export("webhook_public_url", f"https://webhook.{domain}/webhook/github")
pulumi.export("api_function_url", api_lambda.function_url)
pulumi.export("api_public_url", f"https://api.{domain}")
pulumi.export("gha_deploy_role_arn", gha_deploy_role.arn)
pulumi.export("ecr_webhook_repo_url", webhook_ecr.repository_url)
pulumi.export("ecr_api_repo_url", api_ecr.repository_url)
pulumi.export("ddb_table_name", grug_main_table.name)
pulumi.export("ddb_table_arn", grug_main_table.arn)
pulumi.export("kms_cmk_arn", grug_tokens_cmk.arn)
pulumi.export("kms_cmk_alias", grug_tokens_cmk.alias.name)
