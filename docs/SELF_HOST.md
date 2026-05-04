# Self-host Grug

Grug is AGPL-3.0 — you can deploy your own instance against your own AWS
account. The hosted SaaS at https://grug.lol is the convenience path; this
doc covers running it yourself.

## What you'll need

- AWS account with an IAM user that has admin or PowerUser access (used
  once for Pulumi bootstrap; Pulumi-provisioned OIDC role takes over after)
- Cloudflare account (for proxied DNS + Workers + Pages)
- Pulumi Cloud account (free tier OK; alternative is self-managed S3
  backend — see `infra/pulumi/README.md` for the migration recipe)
- Datadog account (free tier OK; or skip the dd_monitors component
  entirely by removing it from `infra/pulumi/__main__.py`)
- A registered GitHub App (you create this once via UI)
- Python 3.13, uv, Docker, gh CLI

## One-time setup

### 1. Register your GitHub App

GitHub Apps are personal — you can't share ours. Walk through
`docs/HITL_PREREQUISITES.md` step-by-step. The output of this section is:

- App ID
- Client ID + Client Secret
- Webhook secret
- Private key file (`.pem`)

### 2. Pre-load secrets into AWS SSM

Run from a shell with AWS creds for the target account:

```bash
aws ssm put-parameter --name /grug/github-app-id --value "<your_app_id>" --type SecureString
aws ssm put-parameter --name /grug/github-app-client-id --value "<your_client_id>" --type SecureString
aws ssm put-parameter --name /grug/github-app-client-secret --value "<your_client_secret>" --type SecureString
aws ssm put-parameter --name /grug/github-app-webhook-secret --value "<your_webhook_secret>" --type SecureString
aws ssm put-parameter --name /grug/github-app-private-key --value "$(cat path/to/key.pem)" --type SecureString

# Cloudflare
aws ssm put-parameter --name /grug/cloudflare-api-token --value "<your_cf_token>" --type SecureString
aws ssm put-parameter --name /grug/cloudflare-zone-id --value "<your_zone_id>" --type String
aws ssm put-parameter --name /grug/cloudflare-account-id --value "<your_account_id>" --type String

# Datadog (optional — skip if you removed dd_monitors)
aws ssm put-parameter --name /shared/datadog-api-key --value "<dd_api_key>" --type SecureString
aws ssm put-parameter --name /shared/datadog-app-key --value "<dd_app_key>" --type SecureString

# Pulumi access token
aws ssm put-parameter --name /shared/pulumi-access-token --value "<pulumi_token>" --type SecureString
```

CF token needs: Zone:DNS:Edit, Workers Scripts:Edit, Workers Routes:Edit,
**Account → Cloudflare Pages: Edit** (this last one is easy to miss).

### 3. Configure your Pulumi stack

```bash
cd infra/pulumi
pulumi login                    # if using Pulumi Cloud
pulumi stack init dev
pulumi config set env dev
pulumi config set domain <your-domain>   # e.g. grug.example.com
```

### 4. Bootstrap the Lambda image

Lambda Image-mode rejects `public.ecr.aws/*` URIs, so we need a private
ECR copy of the AWS Python base image:

```bash
# Provision ECR first (this fails Lambda creation but creates the repo)
pulumi up --stack dev --target 'urn:pulumi:dev::grug::aws:ecr/repository:Repository::grug-webhook'
pulumi up --stack dev --target 'urn:pulumi:dev::grug::aws:ecr/repository:Repository::grug-api'

# Then bootstrap the base image into both repos
make bootstrap-images   # uses crane to copy public python:3.13-arm64 into your private ECR
```

### 5. First full deploy

```bash
pulumi up --stack dev --yes
```

Roughly 5-7 min cold deploy. Should create:
- 2 Lambda Functions (grug-webhook, grug-api) on Function URLs
- DynamoDB single-table `grug-main`
- KMS CMK `alias/grug-tokens`
- IAM OIDC role for GitHub Actions
- Cloudflare DNS records pointing webhook/api subdomains at the
  Function URL hosts (proxied through CF Workers for host-rewrite)

### 6. Configure your GitHub App webhook URL

Go back to your App settings on GitHub:
- Webhook URL = `https://webhook.<your-domain>/webhook/github`
- Webhook secret = the value you put into SSM above

### 7. Bootstrap admin USER# row in DDB

```bash
GRUG_ADMIN_USER_ID=<your_gh_user_id> \
GRUG_ADMIN_LOGIN=<your_gh_login> \
GRUG_ADMIN_INSTALL_ID=<install_id_after_first_install> \
make seed-admin
```

## Day-2 ops

- **Tear-down + rebuild round-trip:** `make rebuild` (see RUNBOOK.md
  "Tear-down + rebuild" section). Should reproduce in ~15 min from a
  clean slate. This is the load-bearing acceptance criterion that
  `pulumi destroy && pulumi up` reproduces from code alone — covered
  by Slice 10 #31.
- **Allowlist new users:** sign in to your hosted dashboard
  (`https://<your-domain>/dashboard`) as admin, navigate to /admin,
  toggle the user.
- **Per-repo persona toggles:** install on the target repo via the
  GitHub App's install URL, then toggle in the dashboard.
- **Monitors + alerts:** Datadog dashboard wires automatically (5
  monitors per stack via `infra/pulumi/components/dd_monitors.py`).
  Notifications go to whatever handle you put in
  `/grug/dd-notify-handle` SSM parameter.

## Differences from the hosted SaaS

The hosted instance at grug.lol carries:
- Pre-allowlisted admin (Evan + collaborators)
- Pre-configured DD monitor handle (#grug Discord channel)
- DD APM extension layer baked into Lambda images at the published tag

For self-hosters:
- You set your own admin via the seed-admin make target
- You control your own DD notify handle (or remove DD entirely)
- DD extension version is configurable via Pulumi
  (`pulumi config set dd_extension_version 65`)

## Compliance with AGPL-3.0

If you modify Grug and offer the modified version as a network
service, you must publish your source. See
https://www.gnu.org/licenses/agpl-3.0.html for the full text.

## Getting help

- File issues at https://github.com/githumps/grug/issues
- Hosted SaaS questions: https://grug.lol
