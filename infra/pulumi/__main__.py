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
    ecr_repo,
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
    branches=["main", "feat/22-slice1-bare-webhook-receiver"],
    tags_pattern="v*",
)

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

pulumi.export("webhook_function_url", webhook.function_url)
pulumi.export("webhook_public_url", f"https://webhook.{domain}/webhook/github")
pulumi.export("gha_deploy_role_arn", gha_deploy_role.arn)
pulumi.export("ecr_webhook_repo_url", webhook_ecr.repository_url)
