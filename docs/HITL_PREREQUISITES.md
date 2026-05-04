# HITL prerequisites — Slice 1 (githumps/grug#22)

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
| Subscribe to events | `pull_request`, `pull_request_review`, `issue_comment` *(closes #2 — `/grug recheck` slash command)* |
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

Run from your laptop with AWS CLI authenticated to the same account as somatic-scripts (`<your-aws-account-id>`, region `us-east-1`):

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
```

Verify:

```bash
aws ssm get-parameters-by-path --region us-east-1 --path /grug --recursive --query 'Parameters[].Name'
# Expected: ["/grug/github-app-id", "/grug/github-app-private-key", "/grug/github-app-webhook-secret"]
```

## 4. Reserve the OIDC role for GitHub Actions deploy

This is created by Pulumi (component `oidc_role.py`), so you don't preload it. But ensure your AWS account has the GitHub OIDC provider registered. Check:

```bash
aws iam list-open-id-connect-providers --region us-east-1
# Should include: arn:aws:iam::<your-aws-account-id>:oidc-provider/token.actions.githubusercontent.com
```

If missing (somatic-scripts deploys should have created it), Pulumi will create it.

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

## 6. Datadog notify handle (Slice 9 #30)

DD monitors fire on Lambda 5xx, sig-verify failures, etc. Notification
target lives in SSM `/grug/dd-notify-handle`.

Format: any string DD knows how to route — Discord webhook handle
(`@webhook-grug`), email mention (`@evan@grug.lol`), PagerDuty
integration name (`@pagerduty-grug-prod`), or `@slack-channel-grug`.

```bash
aws ssm put-parameter --region us-east-1 \
  --name /grug/dd-notify-handle --type String \
  --value "<handle>"
```

**Important — handle is baked into monitor messages at `pulumi up`
time.** If you change the SSM value later, you MUST re-run `pulumi up`
for the new handle to take effect on existing monitors. The Pulumi
diff will show every monitor's `message` field changing. (For more
flexible routing without re-deploys, set the SSM handle to a stable
DD notification target — e.g. `@webhook-grug-router` — and change
the routing inside DD's notification settings.)

DD also needs the App key (in addition to the API key) for monitor
creation:

```bash
aws ssm put-parameter --region us-east-1 \
  --name /shared/datadog-app-key --type SecureString --value "<app key>"
```

(API key already loaded for the Lambda extension; App key is
admin-scoped and only needed by the deploy role.)

## When done

Tell me "HITL done" and I'll run `pulumi up` to provision the dev stack.

## Recovery / rotation

- **App private key compromised:** generate a new one in GH UI, `aws ssm put-parameter --overwrite ...`, redeploy Lambda (env var read at cold start)
- **Webhook secret compromised:** generate new `openssl rand -hex 32`, update both GH App + SSM (`--overwrite`), Lambda picks up on next cold start
- **OAuth client secret compromised:** rotate in GH App UI, update SSM, redeploy
