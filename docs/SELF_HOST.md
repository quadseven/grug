# Self-host Grug

Grug is AGPL-3.0 — you can deploy your own instance against your own AWS
account. The hosted SaaS at https://grug.lol is the convenience path; this
doc covers running it yourself.

## What you'll need

- AWS account with an IAM user that has admin or PowerUser access (used
  once for Pulumi bootstrap; Pulumi-provisioned OIDC role takes over after)
- Cloudflare account (for proxied DNS + Workers + Pages)
- Pulumi Cloud account (free tier OK; alternative is self-managed S3
  backend — see `infra/pulumi/__main__.py` + `docs/NETWORK-TOPOLOGY.md` for the layout)
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

# Datadog (optional — skip if you removed dd_monitors). The Pulumi program
# reads these shared-infra paths (NOT the retired /shared/datadog-* paths).
aws ssm put-parameter --name /infra/datadog/api_key --value "<dd_api_key>" --type SecureString
aws ssm put-parameter --name /infra/datadog/app_key --value "<dd_app_key>" --type SecureString

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

### 4. Provision the Postgres store

Grug stores everything in a single Postgres table `grug_kv` (the #354
store swap retired DynamoDB). Create a database on any Postgres you run
(the hosted instance uses CloudNativePG on Kubernetes), create the
`grug_kv` table, and put the connection string in SSM:

```bash
aws ssm put-parameter --name /grug/database-url \
  --value "postgresql://user:pass@host:5432/grug" --type SecureString
```

Schema (`pk`, `sk`, `data jsonb`, `gsi1pk`, `gsi1sk`, `ttl`) and the lazy
pool live in `services/{api,webhook}/adapters/pg_base.py`.

### 5. Deploy the AWS infra (Pulumi)

```bash
pulumi up --stack dev --yes
```

Creates the AWS-side infra (no Lambda, no ECR — those retired at #354):
- KMS CMK `alias/grug-tokens` (OAuth-token envelope encryption)
- 3 SQS FIFO queues (`grug-rerun-jobs`, `grug-cave-jobs`, `grug-cave-results`) + DLQs
- S3 cave-diff bucket (`grug-cave-diffs*`)
- IAM OIDC role for GitHub Actions + the `grug-k8s-pod` and rotator IAM users
- DD monitors + dashboard + RUM app
- the legacy `grug-main` DynamoDB table (unused — kept in state pending a separate removal)

### 6. Deploy the app to Kubernetes

The services run as pods, not Lambdas. The reference deploy is
`.github/workflows/deploy.k8s.yml`: it builds the arm64 images, pushes
them to your registry, seeds the in-cluster Secrets from SSM/SQS/S3, and
`kubectl apply -k k8s/`. You need:
- a Kubernetes cluster (arm64 nodes) + an image registry
- a Cloudflare tunnel routing `api`/`webhook.<your-domain>` to the
  in-cluster Services on `:8080`, with the `*-host-rewrite` Workers
  deployed via `infra/cloudflare/deploy.sh` (sets `X-Grug-CF-Secret` +
  the `/grug/{api,webhook}-upstream-host` upstream)

See [`docs/RUNBOOK.md`](RUNBOOK.md) and
[`docs/NETWORK-TOPOLOGY.md`](NETWORK-TOPOLOGY.md) for the full deploy and
topology details.

### 7. Configure your GitHub App webhook URL

Go back to your App settings on GitHub:
- Webhook URL = `https://webhook.<your-domain>/webhook/github`
- Webhook secret = the value you put into SSM above

### 8. Bootstrap the admin USER# row in Postgres

```bash
GRUG_ADMIN_USER_ID=<your_gh_user_id> \
GRUG_ADMIN_LOGIN=<your_gh_login> \
GRUG_ADMIN_INSTALL_ID=<install_id_after_first_install> \
make seed-admin
```

## Day-2 ops

- **Tear-down + rebuild round-trip:** `pulumi destroy && pulumi up`
  reproduces the AWS infra from code alone; the app redeploys by
  re-running `deploy.k8s.yml` (rebuilds the images + re-applies `k8s/`).
  See RUNBOOK.md "Tear-down + rebuild". The Postgres `grug_kv` data is
  the only state that must be backed up/restored separately.
- **Allowlist new users:** sign in to your hosted dashboard
  (`https://<your-domain>/dashboard`) as admin, navigate to /admin,
  toggle the user.
- **Per-repo persona toggles:** install on the target repo via the
  GitHub App's install URL, then toggle in the dashboard.
- **Monitors + alerts:** the Datadog monitors + dashboard wire
  automatically via `infra/pulumi/components/dd_monitors.py`
  (Kubernetes-State-Metrics-based since #406). Notifications route to a
  Datadog Webhook the Pulumi program builds from SSM
  `/infra/discord/monitoring-alerts` (the old `/grug/dd-notify-handle`
  placeholder is retired).

## Differences from the hosted SaaS

The hosted instance at grug.lol carries:
- Pre-allowlisted admin (the operator + collaborators)
- Pre-configured DD alert routing (a Discord webhook)
- A node-local Datadog agent on the cluster for APM/logs (DD_AGENT_HOST
  per pod); the app images are instrumented with `ddtrace`

For self-hosters:
- You set your own admin via the seed-admin make target
- You control your own DD alert routing (or remove DD entirely)
- You run your own cluster's Datadog agent (or drop `DD_*` from the manifests)

## Smasher Trial (mutation testing) — enablement + preconditions (#469)

The Smasher persona runs diff-scoped MUTATION TESTING: it launches a
locked-down Kubernetes Job that checks out the PR at its head SHA, mutates the
added lines, and runs the repo's own test suite per mutant. A mutant the tests
still pass on ("survived") is an executable proof of a coverage gap.

Because the Job executes PR-author-controlled code (the repo's tests + the code
under test), Smasher is OFF by default and gated behind a TWO-KEY enable, and it
has a hard cluster precondition:

1. **Policy-enforcing CNI is REQUIRED.** The Trial pod's network isolation
   (`allow-egress-trial` NetworkPolicy) is only enforced by a policy-capable CNI
   (Calico, Cilium, ...). On a non-policy CNI (e.g. flannel) the policy is inert
   and the test phase could reach the cluster network. The load-bearing
   isolation is credential-denial (the test phase gets NO ServiceAccount token
   and NO secrets), but the network jail needs the policy CNI. DO NOT enable
   Smasher on a flannel-only cluster.
2. **Global master switch** — set the SSM String `/grug/smasher-enabled` to
   `true`. Absent/false keeps Smasher globally off regardless of per-repo config.
3. **Per-repo opt-in** — enable `smasher_enabled` on the repo (config API).
   Default OFF.
4. **Trust framing** — only enable Smasher on repos whose PR authors you trust
   at the level of "may run code in the sandbox." The sandbox bounds credential
   theft and resource use, not intent.

RBAC: applying `k8s/smasher-rbac.yaml` creates the minimal `grug-smasher-launcher`
ServiceAccount (Jobs + Pods verbs only). The webhook + consumer deployments run
as this SA so they can launch Trial Jobs; the SA cannot read Secrets or escalate.
See `docs/adr/0013-smasher-trial-sandbox.md` for the full boundary design.

## Compliance with AGPL-3.0

If you modify Grug and offer the modified version as a network
service, you must publish your source. See
https://www.gnu.org/licenses/agpl-3.0.html for the full text.

## Getting help

- File issues at https://github.com/githumps/grug/issues
- Hosted SaaS questions: https://grug.lol
