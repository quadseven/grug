# Grug — Operations Runbook

Living doc for deploying, rotating secrets, debugging, and recovering grug.
Updated as patterns lock in.

> **Runtime:** grug runs on **Kubernetes** (the #354 self-hosting migration
> retired the AWS Lambdas). One container image (`services/webhook/Dockerfile`)
> backs every workload: Deployments `grug-api`, `grug-webhook`, `grug-consumer`
> and CronJobs `grug-poller`, `grug-key-rotator` (manifests in `k8s/`). The
> app store is **Postgres** (table `grug_kv`); SQS FIFO queues carry async work;
> SSM holds secrets. AWS Lambda / DynamoDB / ECR / Function URLs are gone.

## Deploy

Two independent GitHub Actions pipelines, both on push to `main`:

- **App** — `.github/workflows/deploy.k8s.yml` (paths `services/**`, `k8s/**`).
  Builds the arm64 image, pushes it to the private registry, **seeds the
  `grug-secrets` / `grug-rotator-secret` / `registry-pull` k8s Secrets from
  SSM** (`/grug/*`), then `kubectl apply -k k8s/` and rolls the workloads. The
  image is deployed by **immutable digest**, not a mutable tag.
- **Infra** — `.github/workflows/iac.deploy.yml` (paths `infra/pulumi/**`).
  Runs `pulumi up` for the AWS supporting resources (KMS, SQS, S3, IAM/OIDC,
  SSM references, Datadog monitors/RUM/dashboards). It does **not** deploy the
  app and does not build images.

Public-repo discipline: no infra identifiers are committed. The registry host,
cluster credential, and tailnet join key arrive via the `k8s-prod` GitHub
**environment** (vars/secrets); manifests carry `REGISTRY_PLACEHOLDER` /
`TAG_PLACEHOLDER` that the workflow rewrites at deploy time. The cluster, the
in-cluster registry, the Cloudflare tunnel, and the Postgres (CNPG) database
are provisioned in the **private infrastructure repo**, not here.

First-time bring-up: seed the SSM parameters (see `docs/HITL_PREREQUISITES.md`),
seed the `k8s-prod` environment vars/secrets, run `iac.deploy.yml` (infra), then
`deploy.k8s.yml` (app). The first `pulumi up` must run with credentials that can
create the `grug-gha-deploy` OIDC role (chicken-egg: CI assumes that role).

Smoke test:
```bash
curl -i -X POST https://webhook.grug.lol/webhook/github \
  -H 'X-Hub-Signature-256: sha256=invalid' -d '{}'
# Expect: HTTP 401 {"detail":"invalid signature"}
curl -sf https://api.grug.lol/livez && curl -sf https://webhook.grug.lol/livez
```

Ingress: `<service>.grug.lol` resolves to the Cloudflare tunnel, which forwards
to the in-cluster Service on port 8080. The `X-Grug-CF-Secret` shared-secret
boundary (`cf_auth.py`) rejects requests that bypass Cloudflare.

## Secret rotation

All secrets in SSM under `/grug/*` (per-project) and `/infra/*` (cross-cutting,
e.g. `/infra/llm/*`, `/infra/datadog/*`, `/infra/discord/*`). `grug-secrets` is
re-seeded from SSM on every `deploy.k8s.yml` run, so the rotation pattern is:
**put the new value in SSM, then re-seed + roll the pods.**

```bash
# Re-seed grug-secrets from SSM and roll, without a code change:
gh workflow run deploy.k8s.yml --repo githumps/grug --ref main
# (or, if grug-secrets already holds the new value, just force a re-read:)
kubectl -n grug rollout restart deploy/grug-api deploy/grug-webhook deploy/grug-consumer
```

### App private key (rotate quarterly OR on suspected compromise)
1. GitHub App settings -> Generate new private key -> download `.pem`.
2. `aws ssm put-parameter --overwrite --name /grug/github-app-private-key --type SecureString --value "$(cat <new>.pem)"`
3. Re-seed + roll (above). 4. Old key auto-revokes after ~1h (GitHub side).

### Webhook secret (rotate annually)
1. `NEW=$(openssl rand -hex 32)`. 2. Update the GH App webhook-secret field.
3. `aws ssm put-parameter --overwrite --name /grug/github-app-webhook-secret --type SecureString --value "$NEW"`. 4. Re-seed + roll.

### OAuth client secret (on suspected compromise)
GH App -> Generate new client secret -> `aws ssm put-parameter --overwrite --name /grug/github-app-client-secret ...` -> re-seed + roll.

### LLM keys (`/infra/llm/{poolside,openrouter}_api_key`)
Shared cross-project SecureStrings. Update in SSM -> re-seed + roll.

### Datadog / Discord notify
DD keys live at `/infra/datadog/{api,app}_key`; the monitor notify channel is a
Datadog Webhook integration sourced from `/infra/discord/monitoring-alerts`
(set in `infra/pulumi/__main__.py`). Rotating these is an `iac.deploy.yml`
concern, not a pod re-seed.

### AWS pod access key
Rotated automatically by the `grug-key-rotator` CronJob (#386) — see
[Key rotation](#key-rotation). Do not rotate by hand unless that is wedged.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Pod in `CrashLoopBackOff` | bad image / missing dep / failing startup self-check (#405) | `kubectl -n grug describe pod <p>` + DD logs; the `[grug] Pod in CrashLoopBackOff` monitor pages (#406) |
| Deployment stuck `0/1` ready, rollout not completing | new pod fails the dependency-aware `/readyz` (#404) — SSM/KMS or Postgres unreachable | last-good pods keep serving (RollingUpdate maxUnavailable:0); fix the dependency, the rollout self-limits |
| `ImagePullBackOff` | `registry-pull` secret stale / wrong digest | re-run `deploy.k8s.yml` (re-seeds the pull secret + deploys a fresh digest) |
| `/readyz` 503 but `/livez` 200 | a dependency (SSM/KMS/Postgres) is down — by design (#404) | check the dependency; `/readyz` recovers on the next TTL once it's back |
| 5xx through Cloudflare | tunnel down or pods not ready | check the tunnel + `kubectl -n grug get pods`; the workload-not-ready monitor pages (#406) |
| `cf_shared_secret_mismatch` burst | CF shared-secret drifted vs SSM, or a direct-to-origin probe | the CF auth-boundary monitor pages; reconcile the secret |
| A required PR check stuck/missing after a brief outage | the inline DoR/TPM delivery errored during the outage; GitHub gave up (infra #1254) | self-heals: the `grug-poller` CronJob replays errored deliveries every 15m (#407); force it with the manual replay below |

## Missed-delivery replay (#407)

The DoR/TPM check runs inline on the webhook, so a GitHub delivery that arrives
while grug is down is lost. Recovery:

- **Automatic:** the `grug-poller` CronJob (every 15m) calls
  `delivery_replay.replay_since()` each tick — it lists App webhook deliveries
  via `GET /app/hook/deliveries`, and `POST .../attempts` re-sends every event
  (`guid`) that never got a 200-399 delivery. Idempotent: a `guid` whose
  redelivery succeeds is skipped next tick, and the redelivered webhook carries
  that `guid` as `X-GitHub-Delivery`, so the consumer's `claim_delivery`
  dedupes a second processing. (Note: `post_check_run` is NOT idempotent - it
  POSTs the Checks create endpoint - so correctness rests on the guid-skip +
  claim_delivery, never on re-posting being a no-op.)
- **Manual fallback** (force a replay now, or widen the window) — runs the same
  code inside a webhook pod (App SSM env already wired, no local creds needed):

  ```bash
  kubectl -n grug exec deploy/grug-webhook -- python replay_deliveries.py --hours 6
  # or an explicit window start:
  kubectl -n grug exec deploy/grug-webhook -- python replay_deliveries.py --since 2026-06-14T20:00:00Z
  ```

  Exit code is non-zero if any redeliver attempt errored. Default poller window
  is `GRUG_REPLAY_WINDOW_HOURS` (6h).

### Async SQS jobs need no replay (confirmed)

The async layers — `grug-rerun-jobs` (Elder re-runs) and `grug-cave-results`
(cave fallback) — are already outage-resilient and require **no** replay
mechanism: SQS persists each message until acknowledged, with a visibility
timeout + DLQ (redrive after maxReceiveCount). When the `grug-consumer` pod
recovers it long-polls and auto-drains the backlog (#368), and its fail-fast
startup self-check (#405) guarantees it only starts when AWS is reachable. So a
consumer outage delays — never drops — re-run / cave-result jobs.

## Tear-down + rebuild / disaster recovery

The app is fully reconstructable from code + SSM. There is no Lambda/ECR
bootstrap dance: rebuild = re-run the deploy pipelines.

**What persists (lives outside grug's Pulumi state):**
- App data — **Postgres `grug_kv`** (CNPG, managed in the private infra repo;
  survives a grug `pulumi destroy` entirely, since grug's Pulumi never owned it).
- All SSM params (`/grug/*`, `/infra/*`) — referenced, not created, by grug Pulumi.
- GitHub App registration + installations + branch-protection (github.com).
- The OKE cluster, in-cluster registry, and Cloudflare tunnel (private infra).

**What grug's `pulumi up`/`destroy` owns (recreated from code):** KMS CMK
(`alias/grug-tokens`, 7-day deletion delay), SQS queues + DLQs, the cave-diffs
S3 bucket, IAM/OIDC roles, Cloudflare DNS records, Datadog monitors/RUM/
dashboards. (A legacy DynamoDB table is still declared but is NOT the app store
— removal tracked separately.)

**Rebuild:** `iac.deploy.yml` (infra) then `deploy.k8s.yml` (app). Re-seed the
admin row if the database was lost: `infra/scripts/seed-admin.py` writes to
Postgres `grug_kv` (needs `GRUG_DATABASE_URL`); without it the allowlist gate
no-ops every PR. PR check-runs queue + auto-retry (GitHub retries 5xx ~3x over
~30 min), so they recover after the rollout.

**KMS caveat:** OAuth tokens are envelope-encrypted under the CMK DEK; if the
CMK is destroyed + recreated, old encrypted tokens become unrecoverable and
users re-sign-in to re-encrypt. Zero impact for the admin-only user base.

## Observability

- **DD APM:** <https://app.datadoghq.com/apm/services?service=grug-webhook> (also `grug-api`, `grug-consumer`, `grug-poller`)
- **DD Logs:** <https://app.datadoghq.com/logs?query=service%3Agrug-webhook>
- **Pod logs:** `kubectl -n grug logs deploy/grug-webhook --tail=200` (note: kubectl logs can be unreliable on the BYON nodes — prefer DD)
- **Monitors:** k8s-native (CrashLoopBackOff / workload-not-ready / restart-spike / poller-cronjob), all routing to `@webhook-grug-discord-monitoring` (#406)
- **Pulumi state + CF dashboard:** via the operator's Pulumi org + Cloudflare account.

## Service tags

All grug DD entities tagged: `service:` one of `grug-api` / `grug-webhook` /
`grug-consumer` / `grug-poller` / `grug-key-rotator` (cross-workload monitors
use namespace-level `service:grug`); `env:prod`; `version:<image-sha>`;
`team:grug`.

## Architecture decisions

### Sync-vs-async route handlers

`receive_github_webhook` is **`async def`** (it `await request.body()` so HMAC
verifies the raw wire bytes; a sync `def` with `Body(...)` lets Pydantic
JSON-decode before bytes-validation and 422 before HMAC runs — see the comment
in `main.py`). Because it is `async def`, its sync I/O does NOT get Starlette's
`run_in_threadpool` offload — it would run directly on the event loop.

**On k8s this is a live concern, not a free invariant.** Under Lambda there was
one invocation per warm container, so the loop had no peer coroutines to starve.
A uvicorn pod serves **concurrent** requests on one loop, so a slow sync call in
the async handler delays peers, stretching their ACK toward GitHub's ~10s
timeout. So the ACK-path sync calls are offloaded with
`await asyncio.to_thread(...)` (#371): the SSM secret fetch and `dispatch` (its
remaining sync httpx/store work) run in a worker thread — the same offload
`cf_auth.py`'s middleware uses. The heavy Elder review is already off-loop on a
background thread (#368). Any NEW sync I/O added to this handler must be wrapped
the same way (or moved to `httpx.AsyncClient` + `aioboto3`). (Originally #68;
re-opened by the k8s move, fixed in #371.)

### Shared modules (services/_shared/)

The cross-service modules (adapters, ports, personas, github_app_auth,
observability, clients, ...) live ONCE in `services/_shared/`, a PYTHONPATH
root both images and both test suites add after the service dir (#77,
ADR-0014 - supersedes the ADR-0001 mirror discipline and its drift-lint).
Import paths are unchanged (`from adapters.install_store import ...`). Edit
the shared copy; NEVER create a same-relpath file under `services/api/` or
`services/webhook/` - it would silently shadow the shared module for that
service (guarded by `tests/test_shared_no_shadowing.py` + the spec-0010
attester). Per-service files (main.py, rerun.py, dispatcher/consumer, auth/,
crypto/, sast_benchmark/, spark_cave/) stay in their service tree.

## Roles Anywhere credential path (grug-poller tracer, #388)

grug-poller runs on cert-derived short-lived AWS creds (ADR-0008): the
cert-manager Certificate `grug-pki` (CN=grug, 6h/renew-4h, Secret
`grug-pki-tls`) + `aws_signing_helper` (baked into the webhook image) via
SDK `credential_process` (`AWS_CONFIG_FILE=/etc/grug-aws/config`, ARNs
seeded from SSM `/infra/roles-anywhere/...` at deploy). api/webhook/
consumer stay on the static key (`grug-aws-static-key` Secret, split out
of grug-secrets at #388; the #386 rotator now rotates THAT Secret) until
the #389 rollout.

- **Rollback** (poller misbehaving on the cert path): add
  `- secretRef: {name: grug-aws-static-key}` back to the poller's envFrom
  and drop the `AWS_CONFIG_FILE` env - env creds out-rank
  credential_process, so this instantly reverts to the static key. Both
  paths coexist until #389.
- **"Untrusted certificate. Insufficient certificate"** from Roles
  Anywhere = the leaf lost the `digital signature` usage (the
  infrastructure#1318 gotcha). Check `kubectl -n grug get certificate
  grug-pki -o yaml` usages; `test_pki_manifests.py` pins them in CI.
- **The 15m canary**: every poller tick starts with an UNGUARDED
  `sts get-caller-identity` (`roles_anywhere_identity_proven` in DD, with
  the assumed-role ARN). A broken/expired cert or a bypassed chain
  CRASHES the Job - the KSM `duration_since_last_successful` monitor
  pages within the hour. No news from this log line = the cert path is
  dead, not idle.
- **Rotator/SSM gap (pre-existing, #388 follow-up filed)**: the #386
  rotator rotates the LIVE IAM key but never writes it back to
  /grug/k8s-pod-aws-*, so a deploy right after a rotation seeds
  grug-aws-static-key with a DELETED key until the next rotator tick.
- **Cert not issuing**: `kubectl get clusterissuer pki-intermediate` must
  be READY (shared PKI, infrastructure repo); then describe the
  Certificate for cert-manager events.

## Elder (code-reviewer) persona — end-to-end verification

Elder ships in advisory mode by default. After a deploy that changes
`dispatcher.py` / `dispatch.py` / `llm_client.py`, verify on a real PR.

**Prerequisites:** SSM `/infra/llm/{poolside,openrouter}_api_key` loaded (the
webhook pod mounts them via `grug-secrets`); `code_reviewer_enabled=True`,
`code_reviewer_blocking=False` on the test repo's RepoConfig (defaults).

**Steps:** open a small PR on a Grug-installed repo; wait ~30s; verify (1) the
`Grug — Code Review` check-run (conclusion `neutral` in advisory mode), (2) at
least one inline `(file, line)` review comment, (3) DD log
`service:grug-webhook @event:code_reviewer_dispatched` carrying
`installation_id`/`pr`/`head_sha`/`backend`/`model`/`findings_count`/`result`.
Backends round-robin by `installation_id % 2` (poolside even / openrouter odd).

**Failure-mode checks:** no check-run -> DD
`@event:(code_review_fetch_or_parse_failed OR code_review_check_run_publish_failed OR code_review_degraded_publish_failed)`;
neutral+"skipped" -> `@event:code_review_llm_degraded` (which backend kind);
findings but no inline review -> `@event:code_review_review_publish_failed`
(independent surface); webhook 500 -> DD `@event:(tpm_dispatch_unhandled OR code_review_dispatch_unhandled)` (last-resort guards, empty in steady state).

**Security-suite checks (SAST/SCA/secret/IaC):** the four candidate sources
are best-effort - a detector failure returns `()` and never breaks the core
review, which also means a silently-broken detector looks like "no findings."
Verify after deploys that touch them: open a test PR adding a known-bad line
(e.g. a fake `AKIA...` key, or `privileged: true` in a YAML) and confirm the
finding posts. Semgrep engine issues surface as
`@event:(sast_semgrep_binary_missing OR sast_semgrep_failed)`; the judge
failing CLOSED (all candidates suppressed) shows findings_count drop to zero
with `@event:judge_verdicts_unparseable`. Recall/precision baseline lives at
`services/webhook/sast_benchmark/baseline.json` (re-record via the
`benchmark.sast` workflow, record mode).

### Elder async offload + self-recovery

<a id="elder-async-offload"></a>
Off the webhook ACK path (#272, k8s mechanics #368): the sync handler ACKs
GitHub (<10s) and runs the Elder review on an **in-process daemon thread**
(`async_dispatch.run_elder_job`), idempotent on the `X-GitHub-Delivery` id (a
`DELIVERY#<id>` claim row in Postgres `grug_kv`).

- **`[grug-webhook] Elder async-offload failures`** fires on
  `elder_enqueue_failed` (the offload couldn't start) or `elder_job_unhandled`
  (the worker hit an unhandled error). **Self-recovery (#418):** an
  `elder_job_unhandled` no longer waits for a human re-push — it enqueues ONE
  durable re-run to `grug-rerun-jobs`, and the `grug-consumer` re-runs it with
  the SQS redrive contract (visibility timeout -> DLQ after maxReceiveCount).
  Grab the `delivery_id` from the log line (it carries `exc_info`). A
  pod-restart-mid-review drop has no job to enqueue from and still re-triggers
  on next push.
- Review never appears, no failure log -> check `elder_job_duplicate_skipped`
  (a redelivery correctly deduped — the first run already did it) + confirm via
  `elder_job_done`.
- **`[grug-webhook] Elder fallback failed`** (P2): the cave fallback is LIVE
  (ADR-0005, #310/#316/#313, flag ON since 2026-06-10). Clouds-down
  (`code_review_llm_degraded`) is NORMAL — the SaaS backends are unfunded by
  deliberate choice (**do NOT top up OpenRouter/Poolside**) and the Cave heals
  each dropped review. This monitor fires only when the BACKSTOP fails (Cave
  answered degraded, fallback enqueue failed, queue URL missing, or a big diff
  couldn't spill to S3). Investigate the `grug-cave-connector` pod, the egress
  relay, the Cave host, the cave DLQs. Re-run the errored Activity row from the
  dashboard once the Cave recovers.

### Elder prompt A/B experiment (#191)

<a id="elder-prompt-experiment"></a>
Two arms: **v1** (precision, the control) and **v2** (recall), chosen per
install by `select_prompt_variant` from SSM String `/grug/elder-prompt-experiment`
(`off` | `split` | `all_v2`). The arm rides each review's DD LLM-Obs span as
`variant_id`. The variant split `(id // 2) % 2` is orthogonal to the backend
split `id % 2` (a 2x2 grid).

**Check cell balance before flipping** — bucket the allowlisted installs by
`(backend, variant)`. The install IDs live in Postgres `grug_kv` now (NOT
DynamoDB); query them with SQL against `grug_kv` (the `INST#...:META` rows) and
bucket each `id` as `b=(id%2==0?poolside:openrouter)`, `v=((id//2)%2==1?v2:v1)`.
Aim for all four cells populated before trusting a result.

**Flip the arm** (no redeploy; `ignore_changes=["value"]` keeps Pulumi off it):
```bash
aws ssm put-parameter --name /grug/elder-prompt-experiment --region us-east-1 \
  --type String --overwrite --value split   # or: all_v2 | off
```
The mode is `lru_cache`d per pod, so a flip takes effect on the next pod
recycle; force a fast cutover with
`kubectl -n grug rollout restart deploy/grug-webhook`, or wait for the fleet to
turn over before trusting the split. A garbage value logs
`prompt_experiment_mode_unrecognized` and degrades to `off`.

**Arm-up record (2026-06-10, #276):** live population is one install (the
maintainer -> `poolside x v2`; other cells empty), so the experiment is a
**temporal** comparison (v2 post-flip vs v1 history) on the
[DD notebook #14750419](https://app.datadoghq.com/notebook/14750419). Confounds:
SaaS backends are unfunded (arm sample accrues only when a cloud answers); and
cave-fallback reviews carry no `variant_id` (the connector uses its own prompt)
— exclude healed reviews. Re-run the cell-balance check before switching to
`split`.

### Rollback

Disable Elder per-repo by flipping `code_reviewer_enabled=False` on the repo's
RepoConfig — update the row in Postgres `grug_kv` (SQL `UPDATE`) or via the
admin dashboard. (A global SSM kill switch is future-roadmap, not implemented.)

## Key rotation

Interim automated rotation of the `grug-k8s-pod` AWS access key (#386),
throwaway until Roles Anywhere (#388/#389). The `grug-key-rotator` CronJob
runs every 12h: mint a new key -> patch it into `grug-secrets` ->
rollout-restart `grug-api`/`grug-webhook`/`grug-consumer` and wait -> delete
the old key. Logic in `services/webhook/key_rotator.py`. It authenticates
with `grug-rotator-secret` (IAM user `grug-k8s-rotator`, scoped to
access-key ops on `grug-k8s-pod` only), NEVER `grug-secrets`.

**On `[grug] AWS key-rotation failed`:** the rotation is stuck but pods keep
working (fail safe-open keeps the old key valid). Check the Job logs:

```bash
kubectl -n grug get jobs -l app=grug-key-rotator
kubectl -n grug logs job/<grug-key-rotator-...> | grep key_rotation
```

Common causes:
- **Rollout did not complete** (`new_key_id` logged as `dangling_new_key_id`):
  a new key WAS created and is in `grug-secrets`, but a Deployment didn't
  roll. The new key is live + valid; the OLD key was NOT deleted. Investigate
  the stuck rollout; the next 12h tick will retry (it treats the now-current
  new key as current).
- **Two keys, none current** (`refusing to guess`): `grug-k8s-pod` has 2 keys
  and neither matches the one in `grug-secrets` (out-of-band change / stale
  Secret). Reconcile by hand: confirm which key the pods actually use, delete
  the other, then re-run the Job (`kubectl -n grug create job --from=cronjob/grug-key-rotator manual-rotate`).

**Manual rotation:** `kubectl -n grug create job --from=cronjob/grug-key-rotator manual-rotate`,
then `kubectl -n grug wait --for=condition=complete job/manual-rotate --timeout=300s`.

**Disable rotation:** `kubectl -n grug patch cronjob grug-key-rotator -p '{"spec":{"suspend":true}}'`.
