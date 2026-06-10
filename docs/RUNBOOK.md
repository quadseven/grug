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
| CI fails: `aws: command not found` on the self-hosted CI runner | Runner missing tooling | Re-provision the self-hosted runner's tooling from your private infra repo |
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
- **Pulumi state:** <https://app.pulumi.com/<pulumi-org>/grug/dev>
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

Webhook handler `receive_github_webhook` is **`async def`** (it `await
request.body()` so HMAC verifies the raw wire bytes — a sync `def` with
`Body(...)` lets Pydantic JSON-decode before bytes-validation and 422 before
HMAC runs; see the comment in `main.py`). Its downstream calls — boto3
DynamoDB, sync httpx GitHub posts, sync KMS, and the #272
`lambda.invoke` self-invoke — are all **sync I/O running directly on the
event loop** (an `async def` handler does NOT get Starlette's
`run_in_threadpool` offload; only sync `def` handlers do).

**Why that's safe today**: AWS Lambda runs ONE invocation per warm
container, so while the handler runs there are no peer request-coroutines to
starve — the sync calls block a loop that has nothing else to do. This is an
invariant of the execution model, NOT a threadpool guarantee.

**Re-evaluate when**: anything introduces concurrent coroutines on that loop
— `asyncio.gather` fan-out (e.g. parallel multi-repo GitHub calls), a
streaming response, or a background task. At that point wrap the sync calls
in `await asyncio.to_thread(...)` (the pattern `cf_auth.py`'s middleware
already uses for its sync `ssm.get_parameter`), or migrate to
`httpx.AsyncClient` + `aioboto3`. Closes #68.

### Mirrored files between services/api/ + services/webhook/

Both Lambda services duplicate ~12 modules (adapters, ports, personas,
github_app_auth, etc.) until the v1.5 shared-package extraction lands.

`.github/workflows/check.drift-lint.yml` runs `scripts/check-mirrored-files.sh`
on every PR touching either service. The script byte-compares each file
in `MIRRORED_FILES` and fails with a clear diff when they diverge.

When you patch a mirrored file, ALWAYS apply the same change to both
copies. The lint catches misses at PR time.

Files that intentionally diverge (FastAPI app, Lambda handler entrypoint,
logger name) are simply not in `MIRRORED_FILES` — the allowlist is
opt-in by omission. Closes #66.

## Elder (code-reviewer) persona — end-to-end verification

The Elder persona ships in advisory mode by default. After a deploy
that includes a new dispatcher/dispatch.py/llm_client.py change, run
this verification to confirm the full pipeline works on a real PR.

### Prerequisites

1. SSM parameters loaded per `docs/HITL_PREREQUISITES.md` step 3:
   - `/infra/llm/poolside_api_key` (SecureString, shared cross-project)
   - `/infra/llm/openrouter_api_key` (SecureString, shared cross-project)
2. `pulumi up` against the target stack — confirm the webhook Lambda
   environment has both keys mounted.
3. `code_reviewer_enabled=True` and `code_reviewer_blocking=False` on
   the test repo's RepoConfig (defaults, no action needed for a new
   install).

### Steps

1. Open a small test PR on a Grug-installed repo (e.g. githumps/grug
   itself). Touch one file with a trivially-reviewable diff (e.g. add
   a function that swallows a broad `except Exception: pass`).

2. Wait ~30 seconds for the webhook → diff fetch → LLM round-trip.

3. **Verify the check-run** — in the PR's "Checks" tab, look for:
   - `Grug — Code Review` (separate from `Grug — Definition of Ready`)
   - Conclusion: `neutral` (advisory mode)
   - Summary: a Markdown table with at least one finding row OR
     "Elder reviewed the diff and found nothing actionable."

4. **Verify the inline review** — in the "Files changed" tab, look
   for at least one `Grug` review comment pinned to a specific
   `(file, line)`. Comment body should include severity, rule name,
   and the LLM's message.

5. **Verify the structured log** — in DD logs, query
   `service:grug-webhook @event:code_reviewer_dispatched`. The most
   recent entry should carry: `installation_id`, `pr`, `head_sha`,
   `backend` (poolside or openrouter), `model`, `findings_count`,
   `result` (pass/fail/skipped).

6. **Verify both backends fire** (round-robin by `installation_id %
   2`): open one PR from an even-`install_id` repo + one from an
   odd-`install_id` repo. DD logs should show `backend:poolside` for
   the even one and `backend:openrouter` for the odd one.

### Failure-mode checks

- **No check-run appears at all** → query DD for
  `@event:code_review_fetch_or_parse_failed` or
  `@event:code_review_check_run_publish_failed` or
  `@event:code_review_degraded_publish_failed`. Each names the
  specific surface that failed.
- **Check-run appears with conclusion=neutral + "skipped" title** →
  LLM degraded. Query DD for
  `@event:code_review_llm_degraded` to see which backend kind
  (`no_diff` / `all_failed` / `parse_failed`) triggered.
- **Check-run shows findings but no inline review** → review-post
  failed; see `@event:code_review_review_publish_failed`. Check-run
  conclusion is unaffected by review-post failure — independent
  surface by design.
- **Webhook 500s entirely** → query Sentry for `tpm_dispatch_unhandled`
  or `code_review_dispatch_unhandled`. Both are last-resort guards
  that should be empty in steady-state.

### Elder async offload

<a id="elder-async-offload"></a>
Since #272 the Elder LLM review runs **off** the webhook ACK path: the
sync handler ACKs GitHub (<10s) and self-invokes the `grug-webhook`
Lambda asynchronously (`InvocationType="Event"`) to run the review. The
async worker (`async_dispatch.run_elder_job`) is idempotent on the
`X-GitHub-Delivery` id (a `DELIVERY#<id>` DDB row, 24h TTL).

- **Monitor `[grug-webhook] Elder async-offload failures`** fires on
  `elder_enqueue_failed` (the self-invoke `lambda.invoke` threw — usually
  a Lambda throttle) or `elder_job_unhandled` (the async worker crashed).
  Both mean **that review was dropped** — by design we do NOT sync-fall-
  back (it would re-block the ACK). Recovery: the review re-posts when the
  PR is pushed again, or trigger it manually by closing+reopening the PR
  (re-fires `pull_request`). Grab the `delivery_id` from the log line to
  trace the specific delivery.
- **Review never appears, no failure log** → check for
  `elder_job_duplicate_skipped` (a GitHub redelivery / AWS retry was
  correctly deduped — the FIRST delivery already ran it) and confirm the
  original ran via `elder_job_done`.
- AWS async retries are disabled (`maximum_retry_attempts=0`) — the worker
  owns idempotency + degrade, so AWS retries would only risk a storm.
- **Monitor `[grug-webhook] Elder fallback failed — review dropped for real`**
  (P2). The cave fallback is **LIVE** (ADR-0005, #310/#316/#313, flag ON since
  2026-06-10): clouds-down (`code_review_llm_degraded`) is now NORMAL — the
  SaaS backends are unfunded by deliberate choice (**do NOT top up
  OpenRouter/Poolside**) and the Cave heals each dropped review, so alerting
  on clouds-down alone would page on every working review. This monitor fires
  only when the BACKSTOP fails: the Cave answered degraded
  (`elder_fallback_result_degraded`), the fallback enqueue failed, the queue
  URL was missing, or a large diff couldn't spill to S3. Investigate: the
  `grug-cave-connector` pod (LAN worker), the egress relay, the Cave host,
  the cave DLQs. The errored Activity row re-runs from the dashboard once the
  Cave recovers. Awareness-only signals: the P4 "Cave fallback fired" monitor
  (the backstop activating is expected) + the DLQ-depth/queue-age monitors.

### Elder prompt A/B experiment (#191)

<a id="elder-prompt-experiment"></a>
The Elder review prompt has two arms: **v1** (precision-biased, byte-identical
to the shipped prompt — the control) and **v2** (recall-biased). The arm per
install is chosen by `select_prompt_variant`, driven by the SSM String
`/grug/elder-prompt-experiment` (one of `off` | `split` | `all_v2`). The chosen
arm rides each review's DD LLM-Obs span as `variant_id`, so eval results
(`is_real_bug` judge verdict, `human_verdict` reactions) slice by arm.

**Before flipping — check cell balance.** The variant split `(id // 2) % 2` is
orthogonal to the backend split `id % 2`, giving a 2×2 grid (v1/v2 × poolside/
openrouter). It's balanced over each block of 4 consecutive install IDs, but a
skewed live install-ID population can starve a cell. Verify the spread first:

```bash
# List allowlisted install IDs, then bucket each into its (backend, variant) cell.
aws dynamodb scan --table-name grug-main --region us-east-1 \
  --filter-expression 'begins_with(PK, :p) AND SK = :m' \
  --expression-attribute-values '{":p":{"S":"INST#"},":m":{"S":"META"}}' \
  --projection-expression 'PK' --query 'Items[].PK.S' --output text \
| tr '\t' '\n' | sed 's/INST#//' \
| awk '{b=($1%2==0)?"poolside":"openrouter"; v=(int($1/2)%2==1)?"v2":"v1"; print b" "v}' \
| sort | uniq -c
```

Aim for all four cells populated and roughly even before trusting a result. If
a cell is starved (few installs), the arm comparison for that backend is weak —
note it when reading results.

**Flip the arm** (no redeploy needed; `ignore_changes=["value"]` means Pulumi
won't revert it):

```bash
aws ssm put-parameter --name /grug/elder-prompt-experiment --region us-east-1 \
  --type String --overwrite --value split   # or: all_v2 | off
```

**Mixed-mode window.** The mode is `lru_cache`d per warm container, so a flip
takes effect on the **next cold start** of each webhook container — warm
containers keep the old arm until they recycle. Expect a mixed window of
minutes-to-~1h depending on traffic. To force a fast cutover, publish a new
image tag (any `pulumi up` that bumps `webhook_image_tag` cold-starts all
containers), or simply **wait ≥1h before trusting the DD eval split** so the
fleet has fully transitioned. A garbage value (typo) logs
`prompt_experiment_mode_unrecognized` and degrades to `off` (control).

**Reading results** is operator/DD-console work: build (or filter) an LLM-Obs
view faceted on `@variant_id`, comparing the judge `is_real_bug` rate and the
👍/👎 `human_verdict` rate between `v1` and `v2`. Promoting a winning arm to the
default is a future code slice (bake the winner into `build_system_prompt`'s
default), not a toggle flip.

### Rollback

If Elder produces too many false positives or operationally misbehaves,
disable per-repo via:

```bash
# Flip code_reviewer_enabled=False on a specific repo
aws dynamodb update-item --region us-east-1 --table-name grug-main \
  --key '{"PK":{"S":"INST#<install_id>"},"SK":{"S":"REPO#<repo_id>"}}' \
  --update-expression 'SET code_reviewer_enabled = :f' \
  --expression-attribute-values '{":f":{"BOOL":false}}'
```

Global kill switch (all repos) — set the SSM parameter
`/grug/<env>/elder-disabled` to `true` and redeploy
(future-roadmap; not implemented in this slice).
