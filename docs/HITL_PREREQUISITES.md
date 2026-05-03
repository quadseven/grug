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
| Subscribe to events | `pull_request`, `pull_request_review` |
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

Pulumi's `cloudflare` provider needs a token. Create one at <https://dash.cloudflare.com/profile/api-tokens>:

- Permissions: `Zone:DNS:Edit` for `grug.lol`
- Account resources: include the account that owns `grug.lol`

Save it locally (do NOT commit) and set as Pulumi config:

```bash
cd infra/pulumi
pulumi config set --secret cloudflare:apiToken <token>
pulumi config set cloudflare_zone_id <zone-id-for-grug.lol>
```

## When done

Tell me "HITL done" and I'll run `pulumi up` to provision the dev stack.

## Recovery / rotation

- **App private key compromised:** generate a new one in GH UI, `aws ssm put-parameter --overwrite ...`, redeploy Lambda (env var read at cold start)
- **Webhook secret compromised:** generate new `openssl rand -hex 32`, update both GH App + SSM (`--overwrite`), Lambda picks up on next cold start
- **OAuth client secret compromised:** rotate in GH App UI, update SSM, redeploy
