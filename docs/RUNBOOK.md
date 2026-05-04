# Grug — Operations Runbook

Living doc for deploying, rotating secrets, debugging, and recovering grug. Updated as patterns lock in.

## First-time deploy (chicken-egg paths)

The PRD assumed a clean `pulumi up`. Reality has two bootstrap chicken-eggs:

1. **GHA OIDC role is created BY pulumi up** → CI cannot do the very first deploy. First `pulumi up` must run from a developer machine using personal AWS creds.
2. **Lambda Container `package_type=Image` rejects `public.ecr.aws/*` images** → Cannot point Lambda at a public bootstrap image. Workaround: `crane copy` the public AWS Lambda Python base image into our private ECR with `:bootstrap` tag, point Lambda there, swap to real image after CI's first build.

### Procedure

```bash
# 0. Prereqs (one-time per AWS account / Cloudflare zone / GitHub App)
#    See docs/HITL_PREREQUISITES.md for the full list.

# 1. Initialize Pulumi stack
cd infra/pulumi
pulumi stack init <pulumi-org>/grug/dev
pulumi config set aws:region us-east-1
pulumi config set grug:env dev
pulumi config set grug:domain grug.lol
uv sync

# 2. Push public Lambda base into our ECR as `:bootstrap`
#    (crane is a daemon-less docker image copier — no Docker required)
brew install crane
mkdir -p /tmp/grug-docker-config
PASS=$(aws ecr get-login-password --region us-east-1)
AUTH=$(echo -n "AWS:$PASS" | base64)
cat > /tmp/grug-docker-config/config.json <<EOF
{"auths":{"<acct>.dkr.ecr.us-east-1.amazonaws.com":{"auth":"$AUTH"}}}
EOF
DOCKER_CONFIG=/tmp/grug-docker-config crane copy \
  public.ecr.aws/lambda/python:3.13-arm64 \
  <acct>.dkr.ecr.us-east-1.amazonaws.com/grug-webhook:bootstrap

# 3. First pulumi up (creates ECR if missing, Lambda points at :bootstrap)
pulumi up

# 4. Add second Lambda Function URL policy statement
#    (Pulumi can't express the `--invoked-via-function-url` condition)
aws lambda add-permission --region us-east-1 \
  --function-name grug-webhook \
  --statement-id FunctionURLInvokeViaUrlOnly \
  --action lambda:InvokeFunction \
  --principal '*' \
  --invoked-via-function-url

# 5. Trigger CI to push the real Lambda image
gh workflow run iac.deploy.yml --repo githumps/grug \
  --ref feat/<branch> --field stack=dev

# 6. Deploy CF Worker (Pulumi-cloudflare WorkerScript is unreliable;
#    we manage out-of-band — see infra/cloudflare/deploy.sh)
bash infra/cloudflare/deploy.sh
```

Smoke test:
```bash
curl -i -X POST https://webhook.grug.lol/webhook/github \
  -H 'X-Hub-Signature-256: sha256=invalid' -d '{}'
# Expect: HTTP 401 {"detail":"invalid signature"}
```

## CF Worker re-deploy

Run after every `pulumi up` that recreates the Lambda (Function URL host changes per `reference_lambda_function_url_host_volatile` memory):

```bash
bash infra/cloudflare/deploy.sh
```

The script:
- Reads CF token + account/zone IDs from SSM
- Reads upstream Function URL from `pulumi stack output webhook_function_url`
- Templates `__UPSTREAM_HOST__` in `worker.js` → uploads to CF
- Creates route `webhook.grug.lol/*` (idempotent — swallows 409)

## Secret rotation

All secrets in SSM under `/grug/*` (per-project) and `/shared/*` (cross-cutting).

### App private key (rotate quarterly OR on suspected compromise)

1. GitHub App settings → Generate new private key → download `.pem`
2. `aws ssm put-parameter --overwrite --name /grug/github-app-private-key --type SecureString --value "$(cat <new>.pem)"`
3. `aws lambda update-function-configuration --function-name grug-webhook --environment ...` (forces cold-start cache refresh)
4. Old key auto-revoked after ~1 hr (GitHub side)

### Webhook secret (rotate annually)

1. `NEW=$(openssl rand -hex 32)`
2. Update GH App webhook secret field
3. `aws ssm put-parameter --overwrite --name /grug/github-app-webhook-secret --type SecureString --value "$NEW"`
4. Lambda picks up on next cold start (or force one with `update-function-configuration`)

### OAuth client secret (rotate on suspected compromise only)

1. GH App settings → Generate new client secret
2. `aws ssm put-parameter --overwrite --name /grug/github-app-client-secret ...`

### CF API token (rotate quarterly)

1. <https://dash.cloudflare.com/profile/api-tokens> → Roll
2. `aws ssm put-parameter --overwrite --name /grug/cloudflare-api-token --type SecureString --value "<new>"`
3. Re-run `bash infra/cloudflare/deploy.sh`

### DD API key (`/shared/datadog-api-key`)

Shared across all projects. Coordinate with somatic-scripts before rotating.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Webhook returns 403 `AccessDeniedException` | Lambda Function URL missing dual-policy statement 2 | Re-run the `aws lambda add-permission --invoked-via-function-url` from first-time-deploy step 4 |
| Webhook returns 502 | CF Worker errored (check upstream URL is current) | Re-run `bash infra/cloudflare/deploy.sh` |
| Webhook returns 403 with `{"Message":null}` (CF) | CF Worker proxied the request but origin Lambda Function URL rejected mismatched Host header | DNS proxied=False (loses CF) OR Worker upload broken — re-run `deploy.sh` |
| Lambda invocation fails with `entrypoint requires the handler name to be the first argument` | Lambda image is bootstrap (bare AWS Python base) | CI didn't push image OR Pulumi rolled back to `:bootstrap` config. Trigger `gh workflow run iac.deploy.yml` |
| Pulumi up fails: `script already exists` (CF) | CF Worker manually uploaded but not in Pulumi state | Drop `cloudflare:WorkerScript`/`WorkerRoute` from `__main__.py` (we now manage Worker via `infra/cloudflare/deploy.sh`) |
| CI fails: `aws: command not found` on srv-unraid-gha | Runner missing tooling | Re-run `ansible-playbook production/playbooks/gha_runner_tooling.yml --limit srv-unraid-gha` from `githumps/infrastructure` |
| CF Worker upload fails: `code 10021: No such module: worker.js` | Upload form-field name doesn't match metadata.main_module | Use `/tmp/worker.js` as path (basename `worker.js` matches main_module) — `deploy.sh` does this |

## Tear-down + rebuild

Verify-as-acceptance criterion per PRD (Slice 10 #31). Should reproduce in <15 min.

```bash
# Destroy
cd infra/pulumi && pulumi destroy --yes

# Rebuild from clean — 7-step round trip (Makefile `rebuild` target):
#   1. tear-down
#   2. pulumi up --target ECR repos only (Lambda image-mode prereq)
#   3. bootstrap-images (crane copy public python:3.13 → private ECR)
#   4. pulumi up — full stack (Lambdas resolve image_uri)
#   5. trigger CI to build + push real images, swap imageUri
#   6. CF Workers re-deploy + admin re-seed (Function URL host churn)
#   7. smoke test
make rebuild
```

State that lives outside Pulumi (and persists across destroy):
- All SSM params under `/grug/*` and `/shared/*`
- ECR images (lifecycle expires untagged after 14d)
- CF Worker + Route (managed via `deploy.sh`)
- GitHub App registration (manual one-time)

## Observability

- **DD APM:** <https://app.datadoghq.com/apm/services?service=grug-webhook>
- **DD Logs:** <https://app.datadoghq.com/logs?query=service%3Agrug-webhook>
- **CloudWatch Logs:** `aws logs tail /aws/lambda/grug-webhook --region us-east-1 --since 5m`
- **Pulumi state:** <https://app.pulumi.com/pulumi_ehumps_me/grug/dev>
- **CF dashboard:** <https://dash.cloudflare.com/<your-cf-account-id>/workers/services/view/grug-webhook-host-rewrite>

## Service tags

All grug DD entities tagged:
- `service:grug-webhook` and `service:grug-api`
- `env:dev` or `env:prod`
- `version:<image-tag>` (typically commit SHA)
- `app:grug` (resource tag on AWS resources)

## Disaster recovery — full tear-down + cold rebuild

Slice 10 (#31) acceptance proof. The "ready to tear down + build" requirement is load-bearing for AWS-org migration AND any-region failover. Verified via `make rebuild` round-trip against dev.

### What survives `pulumi destroy`

| Persists | Where |
|---|---|
| App registration (App ID, slug, OAuth client ID/secret) | github.com/settings/apps |
| Webhook URL setting | github.com (matches DNS recreated by Pulumi) |
| App webhook secret + private key | SSM SecureString `/grug/github-app-{webhook-secret,private-key}` |
| OAuth client secret | SSM `/grug/github-app-client-secret` |
| Existing installations | github.com (install_id 129256114 etc.) |
| Branch protection on installed repos | github.com (referencing check-run name `Grug — Definition of Ready`) |
| Datadog API + App keys | SSM `/shared/datadog-{api,app}-key` |
| Cloudflare API token | SSM `/grug/cloudflare-api-token` |

### What gets destroyed + recreated

- Lambdas (`grug-webhook`, `grug-api`)
- ECR repositories (untagged-14d lifecycle policy)
- DDB table `grug-main` — incl. INST# rows + USER# rows (admin row + per-repo configs ALL LOST)
- KMS CMK `alias/grug-tokens` (7-day deletion delay; new key takes same alias but different KeyID)
- CF DNS records (`webhook.grug.lol`, `api.grug.lol`)
- CF Workers (`grug-webhook-host-rewrite`, `grug-api-host-rewrite`)
- DD monitors + synthetic uptime test
- IAM roles + policies
- CloudWatch log groups (14d retention)

### Procedure

```bash
make rebuild
```

The target chains:

1. `make tear-down` — `pulumi destroy --yes` (~5min)
2. `pulumi up --yes` — recreates ECR + Lambdas with `:bootstrap` python:3.13 base
3. `gh workflow run iac.deploy.yml` + `gh run watch` — CI builds + pushes real images, then re-runs `pulumi up` to swap imageUri (~5-7min)
4. `bash infra/cloudflare/deploy.sh` — re-deploys Workers (Function URL host changes on recreate per `reference_lambda_function_url_host_volatile`)
5. `python infra/scripts/seed-admin.py` — re-creates admin USER# + INST# rows (without these, allowlist gate no_ops every PR)
6. `make smoke` — asserts `webhook.grug.lol/livez`, `api.grug.lol/livez`, `api.grug.lol/api/v1/health`, `grug.lol`, and `POST /webhook/github` (no-sig) all respond as expected

Wall-clock: ~12-15 min. PR check-runs queue + retry post-rebuild (GitHub auto-retries 3× over ~30min on 5xx/connection-refused).

### Tear-down only

When you want to stop incurring AWS costs (e.g. before a long break) without rebuild:

```bash
make tear-down
```

Note: KMS CMK enters 7-day pending-deletion. Within that window, `pulumi up` against the same stack will recreate the alias on a new key (the old DEKs become unrecoverable, but no DDB data depends on them after destroy).

### What `make rebuild` does NOT recover

- **Encrypted OAuth tokens in old DDB rows** — if a user OAuth'd before tear-down, their tokens were encrypted with the OLD KMS DEK. Rows deleted; new sign-in re-encrypts with new DEK. Acceptable: zero impact for v1 admin-only user base.
- **Per-repo persona overrides** — REPO# rows under INST# get nuked. Admins reconfigure via dashboard or via DDB CLI from a saved snapshot.
- **In-flight CI runs** — workflows that triggered against the destroyed Lambda will 5xx. Re-trigger after rebuild.

### Recovery override variables

`make rebuild` reads these env vars (defaults match Evan's GitHub identity):

| Var | Default | Use |
|---|---|---|
| `GRUG_ADMIN_USER_ID` | 59060157 | Numeric GitHub user ID for admin USER# row |
| `GRUG_ADMIN_LOGIN` | githumps | login for the row |
| `GRUG_ADMIN_INSTALL_ID` | 129256114 | INST# row to backfill (skip on org-account migrations where install_id changed) |

Override per-recovery: `GRUG_ADMIN_USER_ID=123 make rebuild`

## Architecture decisions

### Sync-vs-async route handlers

Webhook handler `receive_github_webhook` is **sync `def`**, NOT `async def`.

**Why**: every downstream call is sync I/O (boto3 DynamoDB, sync httpx GitHub posts, sync KMS via `crypto.kms_envelope`). An `async def` handler would block the event loop on each ~30-500ms call. FastAPI runs sync `def` handlers in a threadpool via Starlette's `run_in_threadpool`, so concurrent invocations don't starve each other.

**Re-evaluate when**: we add genuine concurrent-fan-out (e.g. parallel GitHub calls for multi-repo PR scans via `asyncio.gather`). At that point migrate the relevant section to `httpx.AsyncClient` + `aioboto3` and switch the handler back to `async def`. Closes #68.

### Mirrored files between services/api/ + services/webhook/

Both Lambda services duplicate ~12 modules (adapters, ports, personas,
github_app_auth, etc.) until the v1.5 shared-package extraction lands.

`.github/workflows/drift-lint.yml` runs `scripts/check-mirrored-files.sh`
on every PR touching either service. The script byte-compares each file
in `MIRRORED_FILES` and fails with a clear diff when they diverge.

When you patch a mirrored file, ALWAYS apply the same change to both
copies. The lint catches misses at PR time.

Files that intentionally diverge (FastAPI app, Lambda handler entrypoint,
logger name) are simply not in `MIRRORED_FILES` — the allowlist is
opt-in by omission. Closes #66.
