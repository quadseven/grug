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

# Rebuild from clean (re-do first-time-deploy steps 2-6)
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
- `service:grug-webhook` (or `grug-api` for the API Lambda once Slice 2 ships)
- `env:dev` or `env:prod`
- `version:<image-tag>` (typically commit SHA)
- `app:grug` (resource tag on AWS resources)
