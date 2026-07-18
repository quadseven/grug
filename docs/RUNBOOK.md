# Grug - Operations Runbook

Living doc for deploying, rotating secrets, debugging, and recovering grug.
Updated as patterns lock in.

> **Runtime:** grug runs on **Kubernetes** (the #354 self-hosting migration
> retired the AWS Lambdas). One Dockerfile (`services/Dockerfile`, ARG
> SERVICE) builds the two images backing Deployments `grug-api`,
> `grug-webhook`, `grug-consumer` and CronJob `grug-poller` (manifests in
> `k8s/`; api runs its own image, the rest share the webhook image). The
> app store is **Postgres** (table `grug_kv`); SQS FIFO queues carry async work;
> SSM holds secrets. AWS Lambda / DynamoDB / ECR / Function URLs are gone.

## Deploy

Two independent GitHub Actions pipelines, both on push to `main`:

- **App** — `.github/workflows/deploy.k8s.yml` (paths `services/**`, `k8s/**`).
  Builds the arm64 images, pushes them to the private registry, **seeds
  `grug-secrets` from SSM** (`/grug/*`) and `registry-pull` from the
  environment's registry credentials, then `kubectl apply -k k8s/` and
  rolls the workloads. The
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
gh workflow run deploy.k8s.yml --repo quadseven/grug --ref main
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

### AWS pod credentials
RETIRED as a stored secret (#389): no static AWS key exists anywhere.
Every workload derives short-lived creds from its Roles Anywhere
certificate - see the Roles Anywhere section below.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Pod in `CrashLoopBackOff` | bad image / missing dep / failing startup self-check (#405) | `kubectl -n grug describe pod <p>` + DD logs; the `[grug] Pod in CrashLoopBackOff` monitor pages (#406) |
| Deployment stuck `0/1` ready, rollout not completing | new pod fails the dependency-aware `/readyz` (#404) — SSM/KMS or Postgres unreachable | last-good pods keep serving (RollingUpdate maxUnavailable:0); fix the dependency, the rollout self-limits |
| `ImagePullBackOff` | `registry-pull` secret stale / wrong digest | re-run `deploy.k8s.yml` (re-seeds the pull secret + deploys a fresh digest) |
| `/readyz` 503 but `/livez` 200 | a dependency (SSM/KMS/Postgres) is down — by design (#404) | check the dependency; `/readyz` recovers on the next TTL once it's back |
| 5xx through Cloudflare | tunnel down or pods not ready | check the tunnel + `kubectl -n grug get pods`; the workload-not-ready monitor pages (#406) |
| `cf_shared_secret_mismatch` burst | CF shared-secret drifted vs SSM, or a direct-to-origin probe | the CF auth-boundary monitor pages; reconcile the secret |
| A required PR check stuck/missing after a brief outage | the inline DoR/TPM delivery errored and GitHub does not auto-redeliver (infra #1254) | self-heals: the `grug-poller` CronJob replays errored deliveries every 15m (#407); force it with the manual replay below |

## Missed-delivery replay (#407)

The DoR/TPM check runs inline on the webhook. GitHub does not automatically
redeliver a failed webhook, so recovery is owned here:

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
no-ops every PR. GitHub does not automatically retry failed webhooks. The
`grug-poller` replays failed App deliveries every 15 minutes, so recovery after
the rollout depends on that CronJob being healthy.

**KMS caveat:** OAuth tokens are envelope-encrypted under the CMK DEK; if the
CMK is destroyed + recreated, old encrypted tokens become unrecoverable and
users re-sign-in to re-encrypt. Zero impact for the admin-only user base.

## Observability

- **DD APM:** <https://app.datadoghq.com/apm/services?service=grug-webhook> (also `grug-api`, `grug-consumer`, `grug-poller`)
- **DD Logs:** <https://app.datadoghq.com/logs?query=service%3Agrug-webhook>
- **Pod logs:** `kubectl -n grug logs deploy/grug-webhook --tail=200` (note: kubectl logs can be unreliable on the BYON nodes — prefer DD)
- **Monitors:** k8s-native (CrashLoopBackOff / workload-not-ready / restart-spike / poller-cronjob, #406) + owned queue-depth family (cave-jobs backlog / re-run DLQ / cave DLQs / consumer backlog / telemetry health, on `grug.sqs.*` gauges the consumer emits - #379), all routing to `@webhook-grug-discord-monitoring`
- **Pulumi state + CF dashboard:** via the operator's Pulumi org + Cloudflare account.

## Service tags

All grug DD entities tagged: `service:` one of `grug-api` / `grug-webhook` /
`grug-consumer` / `grug-poller` (cross-workload monitors
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

<a id="roles-anywhere-credential-path-grug-poller-tracer-388"></a>

## Roles Anywhere credential path (fleet-wide, #388/#389)

EVERY grug workload (api, webhook, consumer, poller) runs on cert-derived
short-lived AWS creds (ADR-0008; #388 tracer -> #389 rollout): the
cert-manager Certificate `grug-pki` (CN=grug, 6h/renew-4h, Secret
`grug-pki-tls`) + `aws_signing_helper` (baked into BOTH images) via SDK
`credential_process` (`AWS_CONFIG_FILE=/etc/grug-aws/config`, ARNs seeded
from SSM `/infra/roles-anywhere/...` at deploy). Each service proves the
identity at BOOT (`aws_identity.prove_roles_anywhere_identity` - asserts
the ra-grug session, fails the pod loud) and the poller re-proves every
15m tick. Failures page via the
"[grug] Roles Anywhere credential acquisition failing (15min)" monitor.
The #389 retirement REMOVED the static key, the reserve Secret, and the
#386 rotator - this is the ONLY credential path.

- **Rollback / recovery (post-retirement)**: the static key, its
  reserve Secret, and the #386 rotator NO LONGER EXIST - Roles Anywhere
  is the only credential path. If the cert path breaks fleet-wide:
  1. Fastest: fix the cert path (Certificate Ready? ClusterIssuer? the
     intermediate in the tls.crt bundle? RA outage?). Old pods keep
     serving (maxUnavailable:0) except the consumer (Recreate - pauses,
     SQS buffers).
  2. Full fallback: revert the retirement PR (restores the Pulumi
     user/AccessKey/SSM params + deploy seed), run the pulumi-up, then
     redeploy and flip the affected workload (envFrom the reseeded
     Secret + drop AWS_CONFIG_FILE). The PR #504 drill proved the flip
     mechanics both ways while the reserve existed.
- **Cutover blast radius**: api/webhook roll maxUnavailable:0 - a
  fleet-broken cert path BLOCKS the deploy while old pods keep serving.
  The consumer is strategy Recreate: its old pod stops BEFORE the new
  one proves creds, so a bad rollout PAUSES consumption (SQS buffers,
  no loss) until fixed or rolled back.
- **"Untrusted certificate. Insufficient certificate"** from Roles
  Anywhere = the leaf lost the `digital signature` usage (the
  infrastructure#1318 gotcha). Check `kubectl -n grug get certificate
  grug-pki -o yaml` usages; `test_pki_manifests.py` pins them in CI.
- **Deploy failed AFTER the seed step** (ARN lookup/sed/apply): the
  cluster is in the split-secret half-state - grug-secrets is keyless,
  manifests still old. Running pods keep their baked env; do NOT
  restart api/webhook/consumer pods, just fix and re-run deploy.k8s
  (the seed is delete-then-create idempotent).
- **The 15m canary**: every poller tick starts with an UNGUARDED
  `sts get-caller-identity` (`roles_anywhere_identity_proven` in DD, with
  the assumed-role ARN). A broken/expired cert or a bypassed chain
  CRASHES the Job - the KSM `duration_since_last_successful` monitor
  pages within the hour. No news from this log line = the cert path is
  dead, not idle.
- **Cert not issuing**: `kubectl get clusterissuer pki-intermediate` must
  be READY (shared PKI, infrastructure repo); then describe the
  Certificate for cert-manager events.

## Elder (code-reviewer) persona — end-to-end verification

Elder ships in advisory mode by default. After a deploy that changes
`dispatcher.py` / `dispatch.py` / `llm_client.py`, verify on a real PR.

**Prerequisites:** SSM `/infra/llm/{poolside,openrouter}_api_key` loaded and
readable by the Elder workloads; `code_reviewer_enabled=True` and
`code_reviewer_blocking=False` on the test repo's RepoConfig (defaults).
Production manifests must show `GRUG_ELDER_DURABLE_QUEUE=1` and
`GRUG_ELDER_SETTLE_SECONDS=90` on `grug-webhook`, plus
`GRUG_REVIEW_DEPTH=tiered` (or `deep` only during a deliberate dual-arm
rollback) and `GRUG_OPENROUTER_REVIEW_MODEL=anthropic/claude-opus-4.7` on
both webhook and consumer.

**Steps:** open a small PR on a Grug-installed repo and leave its head unchanged
for at least the settle window. Verify (1) webhook log `elder_review_enqueued`,
(2) consumer log `elder_review_settling`, (3) consumer log
`llm_tiered_escalation` with `escalate=false` for ordinary small PRs and a
single Cave coder `elder_code_review` span (reasoner only when escalate is
true), and (4) the Elder check-run (`neutral` in advisory mode) plus any
inline `(file, line)` findings. The consumer's `code_reviewer_dispatched` /
`elder_review_durable_done` logs must carry the stable head and final result.
Inference starts only after the quiet window (Swift Hunt shortens it for
tiny PRs).

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

<a id="deploy-rollback"></a>
## Deploy rollback (#499, ADR-0017)

Two paths, both re-applying the `grug-last-good` ConfigMap anchor (the
digest pair that was running before the last deploy) - no rebuild:

- **Automatic**: every deploy anchors, applies, then runs a synthetic
  (health probes, a signed ping through the full auth stack, 60s
  zero-restart soak). If the APPLY or the SYNTHETIC fails, the deploy
  rolls itself back, annotates
  `grug.dev/image-source=rollback-last-good`, emits `grug.deploy.rollback`
  (pages Discord via the '[grug] Deploy auto-rollback fired' monitor),
  and fails the run. The bad merge is STILL ON MAIN - revert or fix
  forward, then let the next deploy re-prove itself.
- **Contract**: auto-rollback restores the previous digests AND the
  pre-seed grug-secrets snapshot (with forced pod restarts); k8s/
  manifest regressions are NOT undone (the step warns when the merge
  touched k8s/) - revert + redeploy for those. The manual path is
  image-only (no snapshot exists in a later run).
- **Manual one-click**: Actions -> deploy.rollback -> Run workflow (main).
  Re-applies the release that was running BEFORE the latest deploy - use
  it when the current release is bad, INCLUDING a bad release that
  passed the synthetic. NOTE a "drill" genuinely reverts one release
  (not a no-op): roll forward afterwards with Actions -> deploy.k8s ->
  Run workflow.

Verify state after either path: `kubectl -n grug get deploy -o
jsonpath='{range .items[*]}{.metadata.name}: {.metadata.annotations.grug\.dev/image-source}{"\n"}{end}'`
and check the anchor with `kubectl -n grug get configmap grug-last-good -o yaml`.

<a id="elder-async-offload"></a>

**Queue-depth telemetry + monitors (#379):** the consumer emits
`grug.sqs.messages_visible` / `grug.sqs.messages_not_visible`, a derived
`grug.sqs.stalled` gauge, and a 1/0
`grug.sqs.telemetry_queue_ok` boolean (all tagged `queue:<name>`) every
~60s for all six grug queues; aws.sqs.* is NOT collected in this org, so
these owned gauges are the only queue signal. Alert meanings: backlog
monitors = visible work had no message in flight across 15min; DLQ monitors = a poison
message landed (inspect it via the AWS console or `aws sqs
receive-message` on the DLQ); `Queue telemetry degraded` firing on VALUES
= that queue's probe success rate fell below half (check
`queue_depth_probe_failed` warnings for the botocore error `code` -
AccessDenied means the ra-grug IAM grant regressed); `Queue telemetry
degraded` in NO DATA = the queue vanished from emission (rename) or the
consumer/telemetry thread is down entirely - treat as consumer-down and
check the workload monitors + pod state.

Off the webhook ACK path (#272, k8s mechanics #368): the sync handler ACKs
GitHub in under 10 seconds after writing a snapshot-scoped `kind=review`
message to `grug-rerun-jobs.fifo`. Snapshot identity covers base SHA, head SHA,
title, and body; FIFO ordering is per PR. The webhook stamps a 90-second settle
duration. The consumer fetches the current PR and leases that snapshot, waits,
then fetches it again. If code, base, or intent changed, the consumer enqueues
the freshly fetched eligible snapshot, releases the stale lease, and publishes
nothing from stale input. A stable snapshot continues through deep inference
against the immutable base/head compare. The dispatcher checks the full
snapshot and open/non-draft eligibility before inference and again before
publication. Review messages renew both their SQS visibility and ownership-token
database leases every 120 seconds. A dead worker's expired lease is reclaimable;
a completed snapshot leaves a 30-day tombstone. Model, freshness-check, or
publication failures release the owned lease and raise so SQS redrives the
message. Four bounded workers consume separate per-PR FIFO groups, so one deep
review cannot globally block unrelated reviews or `/grug ask`. Repeated failures
reach the rerun DLQ.

- **`[grug-webhook] Elder async-offload failures`** fires on
  `elder_enqueue_failed`: the durable message was not accepted, so inspect the
  webhook's `GRUG_RERUN_QUEUE_URL`, Roles Anywhere session, and SQS grant. There
  is no synchronous fallback because preserving the GitHub ACK budget is the
  stronger contract. The request returns 5xx so GitHub records a failed
  delivery; GitHub does not retry it automatically. Confirm the 15-minute
  delivery replay poller is healthy. `elder_job_unhandled` now indicates only
  the local-thread compatibility path (`GRUG_ELDER_DURABLE_QUEUE` absent/false).
- Review never appears, no enqueue failure -> follow the snapshot through
  `elder_review_enqueued`, `elder_review_settling`, then one of
  `elder_review_durable_done`, `elder_review_stale_snapshot_cancelled`, or
  `elder_review_duplicate_snapshot_skipped`. A consumer exception leaves the
  message for visibility redrive; persistent failures land in the rerun DLQ and
  page through the owned queue monitors.
- **`[grug-webhook] Elder fallback failed`** (P2): the cave fallback is LIVE
  (ADR-0005, #310/#316/#313). A one-cloud failure is visible as a provisional
  partial review and is retried rather than marked complete.
  `code_review_llm_degraded` with `kind=all_failed` means no cloud pass produced
  a usable response and the Cave path is needed. Check API credit/key/timeout
  health before treating repeated cloud failure as normal.
  This monitor fires when the backstop also fails (Cave
  answered degraded, fallback enqueue failed, queue URL missing, or a big diff
  couldn't spill to S3). Investigate the `grug-cave-connector` pod, the egress
  relay, the Cave host, the cave DLQs. Re-run the errored Activity row from the
  dashboard once the Cave recovers.

### Elder prompt A/B experiment (#191)

<a id="elder-prompt-experiment"></a>
Two arms remain: **v1** (precision) and **v2** (recall). Production
`GRUG_REVIEW_DEPTH=deep` deliberately pins v2 for both backends, so the SSM
experiment does not split normal production reviews. In `fast` mode,
`select_prompt_variant` reads `/grug/elder-prompt-experiment`
(`off` | `split` | `all_v2`) and the selected arm rides the review's DD LLM-Obs
span as `variant_id`.

**Check cell balance before a fast-mode experiment** - bucket the allowlisted
installs by `(primary backend, variant)`. The install IDs live in Postgres
`grug_kv` (not DynamoDB); query the `INST#...:META` rows and bucket each id as
`b=(id%2==0?poolside:openrouter)`, `v=((id//2)%2==1?v2:v1)`. Aim for all four
cells before trusting a fast-mode result. Deep-mode samples are dual-backend v2
and must not be mixed into that comparison.

**Flip the arm** (no redeploy; `ignore_changes=["value"]` keeps Pulumi off it):
```bash
aws ssm put-parameter --name /grug/elder-prompt-experiment --region us-east-1 \
  --type String --overwrite --value split   # or: all_v2 | off
```
The mode is `lru_cache`d per pod, so a flip takes effect on the next pod
recycle. In a deliberate fast-mode experiment, restart both review-capable
deployments (`kubectl -n grug rollout restart deploy/grug-webhook
deploy/grug-consumer`) before trusting the split. A garbage value logs
`prompt_experiment_mode_unrecognized` and degrades to `off`.

**Historical arm-up record (2026-06-10, #276):** the population was one install
(`poolside x v2`; other cells empty), so the notebook comparison was temporal:
[DD notebook #14750419](https://app.datadoghq.com/notebook/14750419). That record
predates the deep dual-backend default and is not evidence about the current
ensemble. Cave-fallback reviews still carry no `variant_id`; exclude them from
any prompt-arm analysis.

### Elder feedback learning

Each posted Elder inline comment stores the redacted finding, reviewed SHA, PR
author, and all model-origin span IDs for 30 days. On new records the poller
checks each reactor through GitHub's collaborator-permission endpoint and
trusts only users with `write` or `admin` access. A changed verdict:

1. submits `human_verdict` to every model span that produced the finding;
2. upserts a stable `grug-elder` row in the repository ledger; and
3. recomputes the bounded practice and few-shot caches used by later reviews.

A thumbs-up becomes positive practice/few-shot evidence. A thumbs-down records a
false positive, removes it from positive examples, and creates bounded `AVOID
FALSE POSITIVE` guidance requiring materially new evidence before Elder repeats
the pattern. Reactions by users without write access do not annotate or train.
Legacy records without capture metadata remain observable under the old DD-only
behavior and never auto-train.

Inspect `reaction_poll_cycle`, `reaction_submit_or_persist_failed`, and the
repo's `LEDGER#<owner/repo>` rows when feedback is not appearing. The ledger
write and cache refresh happen before `last_verdict` advances, so a transient
store failure retries the idempotent learning update on the next poll.

### Review latency harness (#648)

Short Ollama smokes do not model long-context review prefill. The
`review_latency` package replays Elder-shaped prompts (real
`_build_messages` path) at concurrency 1/2/4/8 and reports p50/p95
complete wall-clock (and TTFT when streaming works).

Pure unit tests ship in `make webhook-test`. Live runs are **manual /
on-demand only** (never per-PR CI):

```bash
# From a host that can reach the Cave OpenAI-compatible endpoint.
# Do not commit private URLs or keys.
cd services/webhook
export GRUG_BENCH_CAVE_URL='https://<your-cave-chat-completions-url>'
export GRUG_BENCH_CAVE_MODEL='qwen3-coder-next:q8_0'   # or reasoner model
# optional second arm:
# export GRUG_BENCH_REASONER_URL=...
# export GRUG_BENCH_REASONER_MODEL=...

# Ship each trial as an LLMObs span + parse_ok/complete_s evaluations
# (ml_app=grug-elder-bakeoff by default; agentless, no local DD agent):
export GRUG_BENCH_LLMOBS=1
export DD_API_KEY="$(aws ssm get-parameter --name /grug/dd-api-key \
  --with-decryption --query Parameter.Value --output text)"
# optional: export DD_LLMOBS_ML_APP=grug-elder-bakeoff

make -C ../.. review-latency
# or:
PYTHONPATH=../_shared uv run --with httpx --with ddtrace python -m review_latency \
  --levels 1,2,4,8 --json /tmp/latency-trials.json
```

Inspect bakeoff results in Datadog LLM Evaluations:
`https://app.datadoghq.com/llm/evaluations?query=%40ml_app%3Agrug-elder-bakeoff`
and production Elder traffic under `ml_app:grug-elder`.

Compare a candidate runtime (e.g. vLLM) by pointing `GRUG_BENCH_CAVE_URL`
at that server with the same model id and re-running; keep the fixture
set identical. Decision rule for #649: only cut over if p95 complete at
C=4/8 improves without a parse-fail regression.

### Rollback

Production default is **tiered** (ADR-0019): single coder arm unless
escalation fires. Rollbacks:

```bash
# Max-recall dual-arm (old production default)
kubectl -n grug set env deploy/grug-webhook deploy/grug-consumer \
  GRUG_REVIEW_DEPTH=deep

# Coder-only with reasoner only if coder fails (no sample deep)
kubectl -n grug set env deploy/grug-webhook deploy/grug-consumer \
  GRUG_REVIEW_DEPTH=fast

kubectl -n grug rollout status deploy/grug-webhook
kubectl -n grug rollout status deploy/grug-consumer
```

These do not change the judge, Teller, or `/grug ask` model. Live overrides
are temporary unless the manifests in `k8s/*-deployment.yaml` match.

If the durable queue path itself is the fault, remove
`GRUG_ELDER_DURABLE_QUEUE` from the webhook deployment to use the compatibility
thread path while repairing SQS; this gives up pod-restart durability and the
quiet/stale-head gate. Disable Elder entirely per repo by flipping
`code_reviewer_enabled=False` in Postgres `grug_kv` or through the admin
dashboard. A global SSM kill switch is not implemented.
