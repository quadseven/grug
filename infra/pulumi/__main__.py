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

import json

import pulumi
import pulumi_aws as aws
import pulumi_cloudflare as cloudflare
import pulumi_datadog as _datadog

from components import (
    cf_shared_secret,
    cloudflare_dns,
    dd_dashboard,
    dd_monitors,
    dd_rum,
    ddb_table,
    ecr_repo,
    kms_cmk,
    lambda_service,
    oidc_role,
    scheduled_lambda,
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

# Datadog API key — the shared INFRA DD key (`/infra/datadog/api_key`), the
# same pair the homelab infrastructure Pulumi uses. Consolidated here (#258
# follow-up) so grug doesn't maintain its own DD credentials: the prior
# `/shared/datadog-*` keys were revoked out-of-band and grug had no reason to
# carry a separate key. Lambda extension reads DD_API_KEY to ship traces/logs.
_dd_api_key = aws.ssm.get_parameter(
    name="/infra/datadog/api_key",
    with_decryption=True,
)

# OpenRouter API key — shared cross-project SecureString at the canonical
# `/infra/llm/<provider>_api_key` path (not a grug-specific copy), so the
# key is minted/rotated once. Consumed by the webhook Lambda only — Elder
# persona dispatches the LLM call from the same path that handles
# `pull_request:opened`. api Lambda never needs it; scoping to webhook
# keeps the IAM blast radius small.
_openrouter_api_key = aws.ssm.get_parameter(
    name="/infra/llm/openrouter_api_key",
    with_decryption=True,
)

# Poolside API key — same shared-path + webhook-only rationale as OpenRouter.
# The Elder persona round-robins via `installation_id % 2`; both keys
# must be present for the round-robin to work without permanent fallback
# to the other backend.
_poolside_api_key = aws.ssm.get_parameter(
    name="/infra/llm/poolside_api_key",
    with_decryption=True,
)

# DD extension version baked into the Lambda image (per Dockerfile.lambda).
# Lambda Container package_type can't attach layers; extension binary is
# COPYd from public.ecr.aws/datadog/lambda-extension-arm:<v>. Bump here
# triggers an image rebuild via CI on next merge/dispatch.
_dd_extension_version = config.get("dd_extension_version") or "65"

# OIDC trust for GitHub Actions deploys from githumps/grug. Per
# `feedback_prefer_ssm_over_1p` — no long-lived AWS creds in the repo.
_deploy_role_bundle = oidc_role.create(
    name="grug-gha-deploy",
    repo="githumps/grug",
    # `main` and SaaS-conversion feature branches (epic-grug-saas).
    # Tighten back to `main` only once Slice 13 (#34) ships.
    # Permissive `feat/*` + `fix/*` during the SaaS conversion (closes #64).
    # Tighten back to `main` only at Slice 13 cutover (#34). Earlier
    # per-slice patterns required local pulumi up after every new branch
    # AND silently narrowed back whenever main re-deployed without the
    # wider list.
    branches=["main", "feat/*", "fix/*", "hotfix/*"],
    tags_pattern="v*",
)
gha_deploy_role = _deploy_role_bundle.role
# Sleep waiter that gates KMS-using Lambda Function ops on the deploy
# role's IAM policy having propagated. Closes #88 — replaces workflow-
# layer retry hack with in-IaC dependency edge.
_iam_propagation_wait = _deploy_role_bundle.iam_propagation_wait

# Slice 2 (#23) — DDB single-table + KMS CMK + api Lambda
grug_main_table = ddb_table.create(
    "grug-main", iam_propagation_wait=_iam_propagation_wait,
)
grug_tokens_cmk = kms_cmk.create("grug-tokens")

# CF→AWS auth boundary (parent #173). Provisioned before the Lambdas so
# both can receive the SSM param name via env var on first creation —
# avoids a second `pulumi up` to wire the env var after the secret lands.
cf_secret = cf_shared_secret.create()

# ECR repo for the webhook Lambda image. Lifecycle: untagged images expire
# after 14 days (avoids ~$0.10/GB/mo image graveyard).
webhook_ecr = ecr_repo.create(
    name="grug-webhook",
    untagged_expire_days=14,
    # dev needs force_delete=True for `make rebuild` (Slice 10 #31).
    # prod stays False so `pulumi destroy --stack prod` cannot wipe
    # production images. Greptile P2 PR #59.
    force_delete=(env == "dev"),
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
# Full 40-char commit SHA — separate from image_tag (8-char short)
# because DD source-code linking requires the full SHA. Greptile P1
# PR #81. CI passes via config; bootstrap deploys fall back to short.
_full_commit_sha = config.get("full_commit_sha") or webhook_image_tag
# Elder prompt A/B experiment mode (#191). A plain String (NOT SecureString —
# it's a non-secret operational toggle): one of "off" | "split" | "all_v2".
# Pulumi OWNS the parameter's existence + the "off" default (all-Pulumi rule),
# but `ignore_changes=["value"]` hands the *value* to the operator so they can
# flip arms from the console/CLI during the experiment WITHOUT a redeploy and
# WITHOUT Pulumi reverting it on the next `up`. The webhook reads it
# fallback-safe (secrets_loader.get_prompt_experiment_mode → "off" on any
# error), so a missing/garbage value degrades to the shipped v1 prompt.
_prompt_experiment = aws.ssm.Parameter(
    "grug-elder-prompt-experiment",
    name="/grug/elder-prompt-experiment",
    type="String",
    value="off",
    description="Elder code-review prompt A/B arm: off | split | all_v2 (#191).",
    opts=pulumi.ResourceOptions(ignore_changes=["value"]),
)

webhook = lambda_service.create(
    name="grug-webhook",
    ecr_repo=webhook_ecr,
    image_tag=webhook_image_tag,
    secrets=secrets,
    # Mixed refs: cf_secret.ssm_parameter is `aws.ssm.Parameter` (created
    # resource, Output[str] arns) while the others are `GetParameterResult`
    # (sync data lookup, plain-str arns). Both satisfy the `SsmSecretRef`
    # Protocol (components/_types.py) — they expose `.arn`/`.name`, and the
    # consuming IAM policy wraps the arn list in `Output.all(...).apply()`,
    # so either arm resolves correctly (#235 named this contract).
    extra_ssm_secrets=[_dd_api_key, cf_secret.ssm_parameter, _openrouter_api_key, _poolside_api_key, _prompt_experiment],
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
        # OAuth refs — webhook itself doesn't read them; included so a
        # future merged webhook+api Lambda has them available.
        "GITHUB_APP_CLIENT_ID_SSM": secrets["github-app-client-id"].name,
        "GITHUB_APP_CLIENT_SECRET_SSM": secrets["github-app-client-secret"].name,
        # DDB allowlist gate (Slice 5 #26). Webhook reads INST# + USER#
        # rows directly (no KMS — token blobs are api-Lambda-only).
        "GRUG_DDB_TABLE": grug_main_table.name,
        # Elder persona LLM client — webhook-only (api never calls LLMs).
        "GRUG_OPENROUTER_API_KEY_SSM": _openrouter_api_key.name,
        "GRUG_POOLSIDE_API_KEY_SSM": _poolside_api_key.name,
        # Elder prompt A/B arm selector (#191). Carries the param NAME only;
        # the value (off|split|all_v2) is read fallback-safe at cold start.
        "GRUG_PROMPT_EXPERIMENT_SSM": _prompt_experiment.name,
        # CF→AWS auth boundary — middleware reads at cold start (#173).
        "GRUG_CF_SHARED_SECRET_SSM": cf_secret.ssm_parameter.name,
        # Datadog APM (datadog_lambda wrapper finds real handler via
        # DD_LAMBDA_HANDLER; layer adds the trace agent + log forwarder).
        "DD_LAMBDA_HANDLER": "lambda_handler.handler",
        "DD_SITE": "datadoghq.com",
        "DD_API_KEY": _dd_api_key.value,
        "DD_ENV": env,
        "DD_SERVICE": "grug-webhook",
        "DD_VERSION": webhook_image_tag,
        # Closes #70 — set DD source-code link via runtime env vars
        # instead of --build-arg so commit SHA churn doesn't bust
        # buildx layer cache (which made every deploy a cold rebuild).
        "DD_GIT_REPOSITORY_URL": "https://github.com/githumps/grug",
        "DD_GIT_COMMIT_SHA": _full_commit_sha,
        "DD_TRACE_ENABLED": "true",
        "DD_LOGS_INJECTION": "true",
        # DD LLM Observability for the Elder code-reviewer persona.
        # Webhook-only: api Lambda never makes LLM calls so these env
        # vars are omitted from the api section below.
        "DD_LLMOBS_ENABLED": "true",
        "DD_LLMOBS_ML_APP": "grug-elder",
        # Disable noisy ASGI integration that collapses every FastAPI
        # request into a single "ASGI request" trace span (per memory
        # `reference_dd_apm_asgi_resource_grouping`).
        "DD_PATCH_MODULES": "asgi:false",
        "DD_TRACE_ASGI_ENABLED": "false",
        # Kill DD Lambda Extension's inferred-spans feature. Without
        # this, the Extension synthesizes an `aws.lambda.url` root
        # span per Function URL invocation tagged with the lambda-url
        # FQDN as `service`, creating a phantom DD APM service entry
        # like `<id>.lambda-url.us-east-1.on.aws` alongside the real
        # `grug-webhook` / `grug-api` entries. Canonical kill-switch
        # per https://docs.datadoghq.com/tracing/services/inferred_services/.
        # Verified via DD spans 2026-05-07: 64 phantom spans/2h all
        # carry `service:jikcel...lambda-url.us-east-1.on.aws` AND
        # `base_service:grug-webhook` — same shape pasto-api faced
        # before PR somatic-scripts#235 fixed it.
        "DD_TRACE_MANAGED_SERVICES": "false",
    },
    # 420s (#272 — was 60s in #252). The Elder LLM review is now OFFLOADED
    # off the ACK path: the sync invocation does HMAC + TPM + a fire-and-
    # forget self-invoke (returns in ~1–2s), and a SEPARATE async invocation
    # of THIS function runs the Elder dispatch. This one timeout governs both
    # shapes, so it must cover Elder's worst case: review_diff retries
    # `_RETRY_ATTEMPTS`(3) per backend × 2 backends × `_TIMEOUT_SECONDS`(30) +
    # backoff + the judge call ≈ 300s. 420s gives headroom. The sync ACK path
    # is unaffected by the high ceiling — its httpx calls are each individually
    # bounded (≤30s), so it provably can't run 420s; the ceiling only ever
    # bites the async Elder invocation. Lambda bills actual duration, so the
    # headroom is free except for the rare hung-call concurrency hold.
    timeout_seconds=420,
    memory_mb=512,
    # Grant lambda:InvokeFunction on its own ARN so the sync handler can
    # self-invoke the async Elder worker (#272).
    allow_self_invoke=True,
    # Encrypt env vars (DD_API_KEY in particular) at rest so a reader
    # with `lambda:GetFunctionConfiguration` alone can't recover the
    # plaintext API key. Closes #60. Webhook role granted kms:Decrypt
    # via the additional inline policy below.
    env_vars_kms_key_arn=grug_tokens_cmk.arn,
    # Wait 45s after deploy-role policy update so AWS auth-checks see
    # `kms:Encrypt` + `kms:GenerateDataKey` before this Function update
    # runs. Closes #88.
    iam_propagation_wait=_iam_propagation_wait,
)

# Webhook role gains kms:Decrypt on the grug-tokens CMK — required so
# the Lambda runtime can unwrap encrypted env vars at cold start
# BEFORE the DD extension or our handler boots. Scoped via
# `kms:ViaService = lambda` so this perm can't be reused for other
# KMS-protected resources. Closes #60.
# Region resolved from the active aws provider so a future multi-region
# deploy doesn't silently break this condition. Greptile P2 PR #79.
_aws_region = aws.get_region().name
aws.iam.RolePolicy(
    "grug-webhook-envvar-kms-policy",
    role=webhook.role.id,
    policy=pulumi.Output.all(grug_tokens_cmk.arn, _aws_region).apply(
        lambda args: json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": "kms:Decrypt",
                "Resource": args[0],
                "Condition": {
                    "StringEquals": {
                        "kms:ViaService": f"lambda.{args[1]}.amazonaws.com",
                    },
                },
            }],
        }),
    ),
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
    force_delete=(env == "dev"),  # See webhook_ecr above.
)

api_image_tag = config.get("api_image_tag") or "bootstrap"
api_lambda = lambda_service.create(
    name="grug-api",
    ecr_repo=api_ecr,
    image_tag=api_image_tag,
    secrets=secrets,
    extra_ssm_secrets=[_dd_api_key, cf_secret.ssm_parameter],
    env_vars={
        "GRUG_ENV": env,
        "GRUG_LOG_LEVEL": "INFO",
        "GRUG_BUILD_SHA": api_image_tag,
        "GRUG_DOMAIN": domain,
        "GRUG_DDB_TABLE": grug_main_table.name,
        "GRUG_KMS_CMK_ARN": grug_tokens_cmk.arn,
        "GRUG_CF_SHARED_SECRET_SSM": cf_secret.ssm_parameter.name,
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
        # See webhook above — DD_GIT_* moved from build-arg to runtime
        # env var so layer cache survives commit-SHA churn. Closes #70.
        "DD_GIT_REPOSITORY_URL": "https://github.com/githumps/grug",
        "DD_GIT_COMMIT_SHA": _full_commit_sha,
        "DD_TRACE_ENABLED": "true",
        "DD_LOGS_INJECTION": "true",
        "DD_PATCH_MODULES": "asgi:false",
        "DD_TRACE_ASGI_ENABLED": "false",
        # Kill DD Lambda Extension's inferred-spans feature. Without
        # this, the Extension synthesizes an `aws.lambda.url` root
        # span per Function URL invocation tagged with the lambda-url
        # FQDN as `service`, creating a phantom DD APM service entry
        # like `<id>.lambda-url.us-east-1.on.aws` alongside the real
        # `grug-webhook` / `grug-api` entries. Canonical kill-switch
        # per https://docs.datadoghq.com/tracing/services/inferred_services/.
        # Verified via DD spans 2026-05-07: 64 phantom spans/2h all
        # carry `service:jikcel...lambda-url.us-east-1.on.aws` AND
        # `base_service:grug-webhook` — same shape pasto-api faced
        # before PR somatic-scripts#235 fixed it.
        "DD_TRACE_MANAGED_SERVICES": "false",
    },
    timeout_seconds=15,
    memory_mb=512,
    # SPA at https://grug.lol calls api.grug.lol with credentials cookie.
    # Browser rejects wildcard origin when credentials=True, so the
    # apex must be enumerated. Methods enumerated to match every verb
    # FastAPI routers expose (Slice 7 #28 added PUT for repo config).
    cors_allow_origins=[f"https://{domain}"],
    cors_allow_methods=["GET", "POST", "PUT", "DELETE"],
    cors_allow_headers=["content-type", "authorization"],
    cors_allow_credentials=True,
    # Encrypt env vars at rest — api role already has kms:Decrypt on
    # this CMK via kms_cmk.grant_use_to_role below (envelope-encrypted
    # OAuth tokens), so the additional Lambda-runtime decrypt at cold
    # start is covered by the existing grant. Closes #60.
    env_vars_kms_key_arn=grug_tokens_cmk.arn,
    # See webhook above. Closes #88.
    iam_propagation_wait=_iam_propagation_wait,
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
                            # Scan backs the admin endpoints (_scan_all over
                            # USER#/INST# rows). Its absence 500'd
                            # GET /admin/users + /admin/installations with
                            # AccessDeniedException — admin never worked.
                            "dynamodb:Scan",
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

# Reaction-poll scheduled Lambda (#247b). Reuses the WEBHOOK image — the
# datadog_lambda CMD dispatches to DD_LAMBDA_HANDLER, so pointing it at
# `poller_handler.handler` runs the cron entrypoint (#266) from the same
# image with DD APM tracing. NO Function URL (EventBridge is the only
# trigger). Needs the GitHub-App SecureStrings (install-token minting) +
# DDB read/update; NOT the LLM keys (the reaction annotation is a DD-SDK
# call, no inference). DD_SERVICE=grug-poller for a distinct o11y surface.
poller = scheduled_lambda.create(
    "grug-poller",
    ecr_repo=webhook_ecr,
    image_tag=webhook_image_tag,
    schedule_expression="rate(15 minutes)",
    env=env,
    env_vars={
        "GRUG_ENV": env,
        "GRUG_LOG_LEVEL": "INFO",
        "GRUG_DDB_TABLE": grug_main_table.name,
        "GITHUB_APP_ID_SSM": secrets["github-app-id"].name,
        "GITHUB_APP_PRIVATE_KEY_SSM": secrets["github-app-private-key"].name,
        "DD_LAMBDA_HANDLER": "poller_handler.handler",
        "DD_SITE": "datadoghq.com",
        "DD_API_KEY": _dd_api_key.value,
        "DD_ENV": env,
        "DD_SERVICE": "grug-poller",
        "DD_VERSION": _full_commit_sha,
        # Parity with the webhook for trace/log correlation (the poller's
        # structured logs carry trace_id; spans enabled).
        "DD_TRACE_ENABLED": "true",
        "DD_LOGS_INJECTION": "true",
        # human_verdict evals attach to the Elder review spans, so the
        # poller submits under the SAME ML app as the webhook.
        "DD_LLMOBS_ENABLED": "true",
        "DD_LLMOBS_ML_APP": "grug-elder",
    },
    table_arn=grug_main_table.arn,
    extra_ssm_secrets=[
        secrets["github-app-id"],
        secrets["github-app-private-key"],
    ],
    env_vars_kms_key_arn=grug_tokens_cmk.arn,
    iam_propagation_wait=_iam_propagation_wait,
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

# Datadog monitors (Slice 9 #30). Provider reads DD creds from SSM — the
# shared INFRA DD app key (`/infra/datadog/app_key`), paired with the infra
# API key above. One DD key pair for the homelab rather than a grug-specific
# one (the old per-project `/shared/datadog-app-key/github-grug` was revoked
# out-of-band; consolidating removes a key grug had no reason to own).
_dd_app_key = aws.ssm.get_parameter(
    name="/infra/datadog/app_key", with_decryption=True,
)
_dd_provider = _datadog.Provider(
    "datadog-grug",
    api_key=pulumi.Output.secret(_dd_api_key.value),
    app_key=pulumi.Output.secret(_dd_app_key.value),
    api_url="https://api.datadoghq.com/",
)

# Notification handle. SSM param `/grug/dd-notify-handle` MUST exist
# before first pulumi up — pre-load it via HITL_PREREQUISITES.md §6.
_dd_notify = aws.ssm.get_parameter(name="/grug/dd-notify-handle").value

monitors = dd_monitors.create_all(
    env=env,
    notify_handle=_dd_notify,
    webhook_public_url=f"https://webhook.{domain}/webhook/github",
    api_public_url=f"https://api.{domain}",
    provider=_dd_provider,
)

# DD RUM Application for grug.lol (spec 0013 RumInstrumentation).
# `name="grug-web"` is the canonical service tag the browser SDK must
# pass to DD_RUM.init(...) — same name used in attest_rum_*.py.
# Application ID + client token exported to SSM so web.deploy.yml can
# substitute them into the build at deploy time without committing
# either value to the repo.
rum = dd_rum.create(
    name="grug-web", provider=_dd_provider,
    iam_propagation_wait=_iam_propagation_wait,
)

# Elder code-review health dashboard (#192). LLM Obs span metrics +
# dispatch-log outcomes; eval-based surfaces deep-link to the LLM Obs
# explorer (evaluations aren't dashboard metrics — see component docstring).
elder_dashboard = dd_dashboard.create_elder_health(env=env, provider=_dd_provider)

# Poller error monitor (#247b, Phase 3.6 — a cron with no monitor isn't
# "done"). poller_handler is best-effort (never raises), so any
# aws.lambda.errors means a real crash (import error, OOM, timeout). Defined
# here (not dd_monitors) to stay next to the poller + reuse _dd_provider /
# _dd_notify which are created just above.
monitor_poller_errors = _datadog.Monitor(
    "grug-poller-errors",
    type="metric alert",
    name="[grug-poller] Lambda errors > 0 (15min)",
    query=(
        f"sum(last_15m):sum:aws.lambda.errors{{functionname:grug-poller,env:{env}}}"
        ".as_count() > 0"
    ),
    message=(
        "grug-poller (reaction-poll cron) is erroring — the handler is "
        f"best-effort, so a Lambda error is a real crash. {_dd_notify}"
    ),
    tags=[f"env:{env}", "service:grug-poller", "team:grug"],
    opts=pulumi.ResourceOptions(provider=_dd_provider),
)
# The handler SWALLOWS per-install failures (returns success → aws.lambda.errors
# stays 0), so the metric monitor above can't see a SYSTEMIC outage. The
# all-installs-failed path logs `reaction_poll_all_installs_failed` at error
# (#266) precisely so a LOG monitor can catch it — this is that monitor.
monitor_poller_all_failed = _datadog.Monitor(
    "grug-poller-all-failed",
    type="log alert",
    name="[grug-poller] all installs failed (systemic)",
    query=(
        f'logs("service:grug-poller env:{env} reaction_poll_all_installs_failed")'
        '.index("*").rollup("count").last("15m") > 0'
    ),
    message=(
        "grug-poller: EVERY install failed to poll this cycle — systemic "
        f"auth/config/GitHub failure, the human-verdict feed is dark. {_dd_notify}"
    ),
    tags=[f"env:{env}", "service:grug-poller", "team:grug"],
    opts=pulumi.ResourceOptions(provider=_dd_provider),
)
pulumi.export("poller_function_name", poller.function.name)
pulumi.export("monitor_poller_errors_id", monitor_poller_errors.id)
pulumi.export("monitor_poller_all_failed_id", monitor_poller_all_failed.id)

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
pulumi.export("elder_dashboard_url", elder_dashboard.url)
pulumi.export("monitor_webhook_5xx_id", monitors.webhook_5xx.id)
pulumi.export("monitor_api_5xx_id", monitors.api_5xx.id)
pulumi.export("monitor_sig_verify_fail_id", monitors.sig_verify_fail.id)
pulumi.export("monitor_cold_start_p99_id", monitors.cold_start_p99.id)
pulumi.export("monitor_enforcement_gap_id", monitors.enforcement_gap.id)
pulumi.export("monitor_cf_secret_mismatch_id", monitors.cf_secret_mismatch.id)
pulumi.export("synthetic_uptime_id", monitors.uptime.id)

# DD RUM (spec 0013). Outputs are non-sensitive references — the actual
# values live in SSM SecureStrings managed by the same component.
pulumi.export("rum_application_id_ssm_name", rum.ssm_application_id.name)
pulumi.export("rum_client_token_ssm_name", rum.ssm_client_token.name)

# CF→AWS auth boundary (issue #231 / parent #173). Sibling slice #232
# (infra/cloudflare/deploy.sh) reads this SSM param name and PUTs the
# value as the GRUG_CF_SECRET binding on both host-rewrite Workers.
pulumi.export("cf_shared_secret_ssm_name", cf_secret.ssm_parameter.name)
