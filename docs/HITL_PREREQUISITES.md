# HITL prerequisites — Slice 1 (quadseven/grug#22)

Before `pulumi up` succeeds, you (Evan) must complete these manual steps. They're one-time, not redone on every deploy.

## 1. Register the GitHub App

URL: <https://github.com/settings/apps/new>

| Field | Value |
|---|---|
| GitHub App name | `Grug` |
| Homepage URL | `https://grug.lol` |
| Webhook URL | `https://webhook.grug.lol/webhook/github` *(provisional — first `pulumi up` provisions the DNS; until then any placeholder URL works)* |
| Webhook secret | Generated below in step 2 |
| Repository permissions — Pull requests | **Read & write** |
| Repository permissions — Issues | **Read & write** |
| Repository permissions — Contents | **Read** |
| Repository permissions — Metadata | **Read** *(default, required)* |
| Repository permissions — Checks | **Read & write** |
| Subscribe to events | `pull_request`, `pull_request_review`, `issue_comment`, `pull_request_review_comment` *(the last one feeds reply-mined learnings, #670 / ADR-0020 — without it grug never sees a maintainer's reply to a finding)* |
| Where can this app be installed? | **Any account** |

After creating:
1. Note the **App ID** (numeric, top of the App settings page)
2. Generate a **private key** at the bottom of the App settings → download `.pem`
3. Under **OAuth credentials** section, note the **Client ID** + click "Generate a new client secret" → save the secret
4. Under **General → Identifying and authorizing users**, set the OAuth callback URL to `https://api.grug.lol/api/v1/auth/github/callback`
5. Leave "Request user authorization (OAuth) during installation" **unchecked** for now (Slice 3 wires the OAuth flow)

## 2. Generate the webhook secret

```bash
openssl rand -hex 32
```

Copy the output. You'll paste this into both:
- The GitHub App's **Webhook secret** field (step 1)
- SSM SecureString in step 3

## 3. Pre-load SSM SecureString parameters

Run from your laptop with AWS CLI authenticated to your grug AWS account (`<your-aws-account-id>`, region `us-east-1`):

```bash
# App ID — from step 1
aws ssm put-parameter --region us-east-1 \
  --name /grug/github-app-id \
  --type SecureString \
  --value "<numeric App ID>"

# App private key (PEM contents) — from step 1, the downloaded .pem
aws ssm put-parameter --region us-east-1 \
  --name /grug/github-app-private-key \
  --type SecureString \
  --value "$(cat ~/Downloads/grug.YYYY-MM-DD.private-key.pem)"

# Webhook secret — from step 2
aws ssm put-parameter --region us-east-1 \
  --name /grug/github-app-webhook-secret \
  --type SecureString \
  --value "<the openssl rand -hex 32 output>"

# Session-signing secret — dedicated HMAC key for the dashboard's session
# cookie + OAuth CSRF state, kept SEPARATE from the webhook secret so the
# two trust domains can be rotated independently (audit #5). api falls back
# to the webhook secret if this is absent, but provisioning it is what
# closes the finding. Use a fresh random value (do NOT reuse the webhook
# secret).
aws ssm put-parameter --region us-east-1 \
  --name /grug/session-signing-secret \
  --type SecureString \
  --value "$(openssl rand -hex 32)"

# LLM backend keys live at the SHARED cross-project path
# `/infra/llm/<provider>_api_key` (NOT a grug-specific copy) so each key is
# minted/rotated once across all projects. If they already exist (other
# projects use them too), skip these — grug's Pulumi just reads them.
#
# OpenRouter API key — for Elder persona LLM calls (PRD #181). Mint at
# https://openrouter.ai/settings/keys. Read by the Elder-running workloads
# (webhook + consumer + poller), loaded from SSM at startup.
aws ssm put-parameter --region us-east-1 \
  --name /infra/llm/openrouter_api_key \
  --type SecureString \
  --value "sk-or-v1-..."

# Poolside API key - the second independent Elder review pass in deep mode.
# In fast mode installation_id % 2 still selects the primary. Mint at
# https://poolside.ai/. Read by the Elder-running workloads (webhook +
# consumer + poller).
aws ssm put-parameter --region us-east-1 \
  --name /infra/llm/poolside_api_key \
  --type SecureString \
  --value "<poolside-api-key>"
```

Verify:

```bash
aws ssm get-parameters-by-path --region us-east-1 --path /grug --recursive --query 'Parameters[].Name'
# Expected: ["/grug/github-app-id", "/grug/github-app-private-key", "/grug/github-app-webhook-secret", "/grug/session-signing-secret"]
aws ssm get-parameters-by-path --region us-east-1 --path /infra/llm --recursive --query 'Parameters[].Name'
# Expected (shared): ["/infra/llm/openrouter_api_key", "/infra/llm/poolside_api_key"]
```

## 4. Reserve the OIDC role for GitHub Actions deploy

This is created by Pulumi (component `oidc_role.py`), so you don't preload it. But ensure your AWS account has the GitHub OIDC provider registered. Check:

```bash
aws iam list-open-id-connect-providers --region us-east-1
# Should include: arn:aws:iam::<your-aws-account-id>:oidc-provider/token.actions.githubusercontent.com
```

If missing (a prior deploy in the account may have created it), Pulumi will create it.

## 5. Cloudflare API token for Pulumi

Pulumi's `cloudflare` provider, the worker-deploy script, AND the
Pages-bootstrap script all read the same token from
SSM `/grug/cloudflare-api-token`. Create one at <https://dash.cloudflare.com/profile/api-tokens>
with these permissions (combine into a single token — adding a
permission to an existing token is faster than rotating):

| Scope | Permission | Why |
|---|---|---|
| Zone | DNS:Edit (grug.lol) | Pulumi creates `webhook.grug.lol` + `api.grug.lol` CNAMEs |
| Account | Workers Scripts:Edit | `infra/cloudflare/deploy.sh` PUTs the host-rewrite Workers |
| Account | Workers Routes:Edit | same script binds `webhook.grug.lol/*` + `api.grug.lol/*` to the workers |
| Account | Cloudflare Pages:Edit | `infra/cloudflare/pages-bootstrap.sh` creates the `grug-web` project + binds apex `grug.lol`; `web.deploy.yml` calls `wrangler pages deploy` |

Save under SSM SecureString:

```bash
aws ssm put-parameter --region us-east-1 \
  --name /grug/cloudflare-api-token --type SecureString --value "<token>"
aws ssm put-parameter --region us-east-1 \
  --name /grug/cloudflare-account-id --type String --value "<account-id>"
aws ssm put-parameter --region us-east-1 \
  --name /grug/cloudflare-zone-id --type String --value "<zone-id-for-grug.lol>"
```

After token is in SSM, run `infra/cloudflare/pages-bootstrap.sh` ONCE
to create the Pages project + apex domain binding (idempotent — safe
to re-run if you forget). After that, the `web.deploy.yml` workflow
handles every subsequent build/deploy via `wrangler pages deploy`.

## 6. Datadog alert routing (Slice 9 #30)

DD monitors fire on workload-not-ready, CrashLoopBackOff, sig-verify
failures, etc. (Kubernetes-State-Metrics-based since #406). Routing is
NOT a free-form handle anymore: the Pulumi program builds a Datadog
Webhook integration (`grug-discord-monitoring`) from a Discord webhook
URL in SSM, and every monitor references `@webhook-grug-discord-monitoring`.
Pre-load the Discord webhook URL (the old `/grug/dd-notify-handle`
placeholder is retired — it pointed at an undeliverable `@grug-stub`):

```bash
aws ssm put-parameter --region us-east-1 \
  --name /infra/discord/monitoring-alerts --type SecureString \
  --value "https://discord.com/api/webhooks/<id>/<token>"
```

(To route somewhere other than Discord, swap the `Webhook`/notify-handle
wiring in `infra/pulumi/__main__.py` for your target.)

DD also needs an App key (in addition to the API key) for monitor
creation. Both are the shared-infra keys (`/shared/datadog-*` were
revoked):

```bash
aws ssm put-parameter --region us-east-1 \
  --name /infra/datadog/app_key --type SecureString --value "<app key>"
```

(The API key `/infra/datadog/api_key` ships traces/logs via the
cluster's node-local Datadog agent; the App key is admin-scoped and
only needed by the deploy role for monitor/dashboard/RUM management.)

## When done

Tell me "HITL done" and I'll run `pulumi up` to provision the dev stack.

## Recovery / rotation

- **App private key compromised:** generate a new one in GH UI, `aws ssm put-parameter --overwrite ...`, then `kubectl -n grug rollout restart deploy/grug-api deploy/grug-webhook deploy/grug-consumer` (pods read the SSM value at startup)
- **Webhook secret compromised:** generate new `openssl rand -hex 32`, update both GH App + SSM (`--overwrite`), then rollout-restart the pods to pick it up
- **OAuth client secret compromised:** rotate in GH App UI, update SSM, rollout-restart the pods
