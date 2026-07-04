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
import pulumi_datadog as _datadog

from components import (
    cf_shared_secret,
    k8s_pod_user,
    k8s_rotator,
    dd_dashboard,
    dd_monitors,
    dd_rum,
    ddb_table,
    kms_cmk,
    oidc_role,
    ssm_secrets,
)
# NOTE: CF Worker (grug-webhook-host-rewrite) is managed OUT OF BAND
# via infra/cloudflare/deploy.sh because pulumi-cloudflare's
# WorkerScript resource fails idempotency and main_module handling
# (mirrors the same out-of-band host-rewrite Worker decision the operator
# made for other services).

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

# Cloudflare: this stack manages NOTHING in CF anymore. Both grug.lol
# DNS records are hand-owned pending the infra zone migration
# (infra#239) - the stack token kept Workers perms (deploy.sh) but
# lost zone DNS. The Worker scripts themselves deploy via
# infra/cloudflare/deploy.sh (CF API), not Pulumi.

# Datadog API key — the shared INFRA DD key (`/infra/datadog/api_key`), the
# same pair the operator's shared infrastructure Pulumi uses. Consolidated here
# (#258 follow-up) so grug doesn't maintain its own DD credentials: the prior
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
    # Trust ONLY `main` (+ `v*` tags below). The SaaS conversion is past
    # cutover, so the permissive `feat/*` / `fix/*` / `hotfix/*` patterns
    # (closes #64) are retired: they let anyone who could push such a
    # branch to githumps/grug assume the deploy role and act in the AWS
    # account. `pulumi up` only runs from `main` anyway (the deploy job is
    # gated on it), so a feature branch never legitimately needs the role.
    # (Audit #2.) Forks are already excluded — the trust is repo-scoped to
    # githumps/grug, so this only ever governed first-party branches.
    branches=["main"],
    # deploy.k8s runs inside the k8s-prod environment (#354).
    environments=["k8s-prod"],
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


# Full 40-char commit SHA — DD source-code linking requires the full
# SHA (review-bot P1 PR #81). CI passes via config; falls back to the api
# image tag (the webhook Lambda retired at the #354 k8s cutover).
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

# --- Elder cave fallback — SQS airlock to the self-hosted LLM (ADR-0005, #310) ---
# Feature flag. Pulumi OWNS the param + "false" default (all-Pulumi rule);
# `ignore_changes=["value"]` hands the toggle to the operator (no redeploy). The
# webhook reads it fallback-safe (secrets_loader.get_fallback_enabled → False on
# any error), so the fallback can never turn ITSELF on by accident.
_fallback_enabled = aws.ssm.Parameter(
    "grug-elder-fallback-enabled",
    name="/grug/elder-fallback-enabled",
    type="String",
    value="false",
    description="Enable Elder's owned cave fallback when both cloud LLMs fail (ADR-0005, #310).",
    opts=pulumi.ResourceOptions(ignore_changes=["value"]),
)

# --- Smasher Trial global master switch (ADR-0013, #469) ---
# Pulumi OWNS the param + "false" default (all-Pulumi rule); `ignore_changes`
# hands the toggle to the operator (no redeploy). The webhook + consumer read it
# fallback-safe (secrets_loader.get_smasher_enabled → False on any error), so
# Smasher can never turn ITSELF on. Enabling also requires per-repo opt-in AND a
# policy-enforcing CNI (see docs/SELF_HOST.md § Smasher).
_smasher_enabled = aws.ssm.Parameter(
    "grug-smasher-enabled",
    name="/grug/smasher-enabled",
    type="String",
    value="false",
    description="Global master switch for the Smasher Trial mutation testing (ADR-0013, #469).",
    opts=pulumi.ResourceOptions(ignore_changes=["value"]),
)

# Fail-closed egress precondition (codex peer-review PR #494): Smasher refuses to
# run author pytest unless the operator affirms a policy-enforcing CNI here.
# Default false so a config mistake can't run author code with unrestricted
# egress on flannel. Operator flips it once a policy CNI (Calico/Cilium) is live.
_smasher_netpol_enforced = aws.ssm.Parameter(
    "grug-smasher-network-policy-enforced",
    name="/grug/smasher-network-policy-enforced",
    type="String",
    value="false",
    description="Operator affirmation that a policy-enforcing CNI is present (Smasher egress gate, #469).",
    opts=pulumi.ResourceOptions(ignore_changes=["value"]),
)
# DLQs (#312): a poison message that fails maxReceiveCount times lands here
# instead of vanishing or looping forever — the operator-visible "stuck" signal.
# FIFO source → FIFO DLQ. The jobs DLQ is the meaningful one (a job the connector
# can't process); the results DLQ is mostly inert (the webhook handler never
# raises) but added for symmetry + hygiene.
_cave_jobs_dlq = aws.sqs.Queue(
    "grug-cave-jobs-dlq",
    name="grug-cave-jobs-dlq.fifo",
    fifo_queue=True,
    message_retention_seconds=1209600,  # 14d to inspect a poison job
    tags={"app": "grug", "service": "grug-cave"},
)
_cave_results_dlq = aws.sqs.Queue(
    "grug-cave-results-dlq",
    name="grug-cave-results-dlq.fifo",
    fifo_queue=True,
    message_retention_seconds=1209600,
    tags={"app": "grug", "service": "grug-cave"},
)
# The airlock: two FIFO queues. Grug and the Cave never connect — they only ever
# touch these. `jobs`: webhook → connector. `results`: connector → webhook (ESM).
_cave_jobs_queue = aws.sqs.Queue(
    "grug-cave-jobs",
    name="grug-cave-jobs.fifo",
    fifo_queue=True,
    # Producer supplies MessageDeduplicationId (head-scoped: a new push is a
    # DISTINCT fallback, unlike the rerun's head-less dedup) — NOT content-based.
    content_based_deduplication=False,
    message_retention_seconds=1209600,  # 14d: hold a backlog while the connector is down
    visibility_timeout_seconds=420,     # ≥ the connector's per-job review budget
    receive_wait_time_seconds=20,       # long polling (#312 free-tier guard)
    redrive_policy=_cave_jobs_dlq.arn.apply(
        lambda arn: json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": 5})
    ),
    tags={"app": "grug", "service": "grug-cave"},
)
_cave_results_queue = aws.sqs.Queue(
    "grug-cave-results",
    name="grug-cave-results.fifo",
    fifo_queue=True,
    content_based_deduplication=True,   # results keyed by content; connector needn't mint an id
    message_retention_seconds=1209600,
    receive_wait_time_seconds=20,       # long polling (#312 free-tier guard)
    redrive_policy=_cave_results_dlq.arn.apply(
        lambda arn: json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": 3})
    ),
    # AWS requires an SQS→Lambda event-source-mapping queue's visibility timeout
    # to be >= the consuming function's timeout. The webhook Lambda is 420s
    # (shared with the Elder async path), so this MUST be >= 420 or
    # CreateEventSourceMapping 400s (InvalidParameterValueException). The result
    # handler itself is fast; this ceiling just satisfies the ESM constraint.
    visibility_timeout_seconds=420,
    tags={"app": "grug", "service": "grug-cave"},
)
# Spilled-diff bucket (#311): a diff over the SQS 256 KB cap is written here and
# the job carries only an S3 pointer (DiffRef). Ephemeral — a 7-day lifecycle
# reaps them (a fallback job is consumed in minutes). Auto-named (globally
# unique) but `grug-`-prefixed so the deploy role's `grug-*` S3 scope matches.
# Private: all public access blocked (the connector reads it via IAM creds).
_cave_diff_bucket = aws.s3.BucketV2(
    "grug-cave-diffs",
    tags={"app": "grug", "service": "grug-cave"},
)
aws.s3.BucketPublicAccessBlock(
    "grug-cave-diffs-pab",
    bucket=_cave_diff_bucket.id,
    block_public_acls=True,
    block_public_policy=True,
    ignore_public_acls=True,
    restrict_public_buckets=True,
)
aws.s3.BucketLifecycleConfigurationV2(
    "grug-cave-diffs-lifecycle",
    bucket=_cave_diff_bucket.id,
    rules=[
        {
            "id": "expire-spilled-diffs",
            "status": "Enabled",
            "filter": {"prefix": "diffs/"},
            "expiration": {"days": 7},
        }
    ],
)
# Connector principal (grug-cave-connector, #316): consume jobs + send results,
# nothing more. The access key is minted out-of-band by #316 — NOT in Pulumi
# state — so this just establishes the permission boundary.
_cave_connector_user = aws.iam.User(
    "grug-cave-connector",
    name="grug-cave-connector",
    tags={"app": "grug", "service": "grug-cave"},
)
aws.iam.UserPolicy(
    "grug-cave-connector-policy",
    user=_cave_connector_user.name,
    policy=pulumi.Output.all(
        _cave_jobs_queue.arn, _cave_results_queue.arn, _cave_diff_bucket.arn
    ).apply(
        lambda a: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                        "Resource": a[0],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["sqs:SendMessage", "sqs:GetQueueAttributes"],
                        "Resource": a[1],
                    },
                    {
                        # Read spilled diffs (#311) — GetObject only, scoped to
                        # the diffs/ prefix. Never write, never list the bucket.
                        "Effect": "Allow",
                        "Action": ["s3:GetObject"],
                        "Resource": f"{a[2]}/diffs/*",
                    },
                ],
            }
        )
    ),
)

# --- Re-run queue (#305, ADR-0004): operator backfill for `errored` rows -----
# api → enqueue, webhook → consume (event-source mapping). FIFO content-dedup on
# (install,repo,pr,persona) drops a double-click; MessageGroupId=install_id
# rate-controls a batch backfill. DLQ + redrive (maxReceiveCount=3) so a stuck
# re-run pages instead of vanishing.
_rerun_dlq = aws.sqs.Queue(
    "grug-rerun-jobs-dlq",
    name="grug-rerun-jobs-dlq.fifo",
    fifo_queue=True,
    message_retention_seconds=1209600,  # 14d to inspect a stuck re-run
    tags={"app": "grug", "service": "grug-rerun"},
)
_rerun_jobs_queue = aws.sqs.Queue(
    "grug-rerun-jobs",
    name="grug-rerun-jobs.fifo",
    fifo_queue=True,
    content_based_deduplication=False,  # api supplies MessageDeduplicationId
    # >= the webhook Lambda timeout (the SQS→Lambda ESM rule, learned #310).
    visibility_timeout_seconds=420,
    redrive_policy=_rerun_dlq.arn.apply(
        lambda arn: json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": 3})
    ),
    tags={"app": "grug", "service": "grug-rerun"},
)

# Scoped IAM user for the Kubernetes pods (#354): queue + envelope-KMS +
# /grug/* parameter access, key pair landed in /grug/k8s-pod-aws-* for
# the deploy workflow's secret seed. Retires with nothing - the same
# user serves webhook/api/poller pods.
_k8s_pod = k8s_pod_user.create(
    queue_arns=[_cave_jobs_queue.arn, _cave_results_queue.arn, _rerun_jobs_queue.arn],
    kms_key_arn=grug_tokens_cmk.arn,
    cave_diff_bucket_arn=_cave_diff_bucket.arn,
)

# Interim AWS key-rotator user (#386) - scoped to access-key ops on the pod
# user ONLY. Throwaway until Roles Anywhere (#388/#389). Its key lands in
# /grug/k8s-rotator-aws-* for the deploy seed.
_k8s_rotator = k8s_rotator.create(pod_user_arn=_k8s_pod.user.arn)


# Cloudflare DNS — webhook.grug.lol is NO LONGER managed by this stack.
# The stack's CF token verifies active but 10000s on every grug.lol
# DNS-records call (kept Workers perms, lost zone DNS), which wedged the
# retirement up twice (a failed update replays forever). The record
# (id 04a6530d7067a563ffd2eb23962946ef) lives on in the zone - content
# hand-PATCHed to the tunnel origin with the infra-zone token - and was
# `pulumi state delete`d from this stack 2026-06-12. The infra stack
# adopts BOTH grug.lol records (webhook + api) at the zone migration
# (infra#239). The api record was likewise hand-PATCHed to its tunnel
# origin + state-deleted at the api cutover (#1172).

# Datadog monitors (Slice 9 #30). Provider reads DD creds from SSM — the
# shared INFRA DD app key (`/infra/datadog/app_key`), paired with the infra
# API key above. One shared DD key pair rather than a grug-specific one (the
# old per-project `/shared/datadog-app-key/github-grug` was revoked
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

# Notification routing → a Discord webhook the operator already runs. The old
# `/grug/dd-notify-handle` was the placeholder `@grug-stub`, which Datadog does
# not recognise as any target — so EVERY monitor notified into the void (the
# Elder LLM outage ran ~5 days with no page). Register a real DD webhook
# integration that POSTs to the pre-existing Discord webhook secret, and route
# all monitors at it. `@webhook-<name>` is how a DD monitor references a webhook
# integration entry. The webhook URL is an SSM SecureString (set out-of-band,
# like the other `/infra/*` shared params) — one param, all stacks.
_discord_webhook_url = pulumi.Output.secret(
    aws.ssm.get_parameter(
        name="/infra/discord/monitoring-alerts",
        with_decryption=True,
    ).value
)
_dd_discord = _datadog.Webhook(
    "grug-discord-monitoring",
    name="grug-discord-monitoring",
    url=_discord_webhook_url,
    encode_as="json",
    # Discord's native webhook body. DD substitutes the $-variables at send
    # time; keep it short — Discord rejects content > 2000 chars. `\n` in the
    # source becomes a JSON newline escape Discord renders as a line break.
    payload='{"content": "$EVENT_TITLE\\n$EVENT_MSG\\n$LINK"}',
    opts=pulumi.ResourceOptions(provider=_dd_provider),
)
_dd_notify = "@webhook-grug-discord-monitoring"

monitors = dd_monitors.create_all(
    env=env,
    notify_handle=_dd_notify,
    webhook_public_url=f"https://webhook.{domain}/webhook/github",
    api_public_url=f"https://api.{domain}",
    provider=_dd_provider,
)

# Cave fallback (#310, ADR-0005): jobs queue backing up = the grug-cave-connector
# isn't draining (down, or can't reach the Cave) → fallback reviews stay
# `errored`. Informational (P4) until the fallback is live (#313); until then the
# queue is empty and this never fires. (DLQ + age/depth hardening is #312.)
_cave_jobs_age_monitor = _datadog.Monitor(
    "grug-cave-jobs-age",
    type="metric alert",
    name="[grug-webhook] Cave fallback jobs queue backing up",
    message=(
        f"{_dd_notify}\n"
        "grug-cave-jobs (Elder cave fallback) has messages older than 10min — the "
        "grug-cave-connector isn't draining (down, or can't reach the Cave). "
        "Fallback reviews stay `errored` until it recovers.\n"
        "Runbook: docs/RUNBOOK.md#elder-async-offload"
    ),
    query=(
        "max(last_15m):max:aws.sqs.approximate_age_of_oldest_message"
        "{queuename:grug-cave-jobs.fifo} > 600"
    ),
    tags=[f"env:{env}", "service:grug-webhook", "team:grug"],
    notify_no_data=False,
    priority=4,
    opts=pulumi.ResourceOptions(provider=_dd_provider),
)

# Re-run DLQ depth (#305): a job that failed maxReceiveCount times (GitHub fetch
# failing, or a malformed job) lands here — the operator's re-run didn't
# complete. Any message is worth a look.
_rerun_dlq_monitor = _datadog.Monitor(
    "grug-rerun-dlq-depth",
    type="metric alert",
    name="[grug] Re-run DLQ has messages",
    message=(
        f"{_dd_notify}\n"
        "grug-rerun-jobs-dlq has messages — a re-run job exhausted its retries "
        "(GitHub fetch failing, or a malformed job). The operator's re-run did "
        "not complete; inspect the DLQ message.\n"
        "Runbook: docs/RUNBOOK.md#elder-async-offload"
    ),
    query=(
        "max(last_15m):max:aws.sqs.approximate_number_of_messages_visible"
        "{queuename:grug-rerun-jobs-dlq.fifo} > 0"
    ),
    tags=[f"env:{env}", "service:grug-api", "team:grug"],
    notify_no_data=False,
    priority=3,
    opts=pulumi.ResourceOptions(provider=_dd_provider),
)

# Cave DLQ depth (#312): a job/result that exhausted maxReceiveCount landed in a
# cave DLQ — a poison message the connector (or webhook) couldn't process.
# Covers both cave DLQs (the jobs one is the meaningful path; results is mostly
# inert since the handler never raises).
_cave_dlq_monitor = _datadog.Monitor(
    "grug-cave-dlq-depth",
    type="metric alert",
    name="[grug] Cave airlock DLQ has messages",
    message=(
        f"{_dd_notify}\n"
        "A cave airlock DLQ (grug-cave-jobs-dlq / grug-cave-results-dlq) has "
        "messages — a poison job/result exhausted its retries. The fallback "
        "review for that PR did not complete; inspect the DLQ message.\n"
        "Runbook: docs/RUNBOOK.md#elder-async-offload"
    ),
    query=(
        "max(last_15m):max:aws.sqs.approximate_number_of_messages_visible"
        "{queuename:grug-cave-jobs-dlq.fifo OR queuename:grug-cave-results-dlq.fifo} > 0"
    ),
    tags=[f"env:{env}", "service:grug-webhook", "team:grug"],
    notify_no_data=False,
    priority=3,
    opts=pulumi.ResourceOptions(provider=_dd_provider),
)

# Fallback-fired rate (#312): the operator's awareness signal that the owned
# backstop actually activated (both cloud LLMs were down and a job was enqueued).
# Informational (P4) — it won't fire until the fallback is enabled (#313). Built
# from the producer's `elder_fallback_enqueued` log.
_cave_fallback_fired_monitor = _datadog.Monitor(
    "grug-cave-fallback-fired",
    type="log alert",
    name="[grug-webhook] Cave fallback fired (1h)",
    message=(
        f"{_dd_notify}\n"
        "Elder's owned cave fallback was enqueued >= 1 time in the last hour — "
        "both cloud LLM backends were down and the backstop kicked in. Expected "
        "to be rare; sustained firing means the SaaS backends are persistently "
        "out (which is by design, ADR-0005)."
    ),
    query=(
        f'logs("service:grug-webhook env:{env} elder_fallback_enqueued")'
        '.index("*").rollup("count").last("1h") >= 1'
    ),
    tags=[f"env:{env}", "service:grug-webhook", "team:grug"],
    notify_no_data=False,
    priority=4,
    opts=pulumi.ResourceOptions(provider=_dd_provider),
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


pulumi.export("webhook_public_url", f"https://webhook.{domain}/webhook/github")
pulumi.export("api_public_url", f"https://api.{domain}")
pulumi.export("gha_deploy_role_arn", gha_deploy_role.arn)
pulumi.export("ddb_table_name", grug_main_table.name)
pulumi.export("ddb_table_arn", grug_main_table.arn)
pulumi.export("kms_cmk_arn", grug_tokens_cmk.arn)
pulumi.export("kms_cmk_alias", grug_tokens_cmk.alias.name)
pulumi.export("elder_dashboard_url", elder_dashboard.url)
pulumi.export("monitor_workload_not_ready_id", monitors.workload_not_ready.id)
pulumi.export("monitor_crashloop_id", monitors.crashloop.id)
pulumi.export("monitor_restart_spike_id", monitors.restart_spike.id)
pulumi.export("monitor_poller_cronjob_id", monitors.poller_cronjob.id)
pulumi.export("monitor_sig_verify_fail_id", monitors.sig_verify_fail.id)
pulumi.export("monitor_enforcement_gap_id", monitors.enforcement_gap.id)
pulumi.export("monitor_persona_dispatch_unhandled_id", monitors.persona_dispatch_unhandled.id)
pulumi.export("monitor_elder_llm_degraded_id", monitors.elder_llm_degraded.id)
pulumi.export("dd_discord_webhook_name", _dd_discord.name)
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
