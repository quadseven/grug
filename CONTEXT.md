# CONTEXT.md — grug domain glossary

The vocabulary used in `services/`, `infra/`, and `web/`. Terms map to identifiers a contributor will encounter while reading code. New contributors should be able to read this file and use the terms correctly without grepping. Drift between this file and the code is a bug; fix the file in the same PR that renames a concept.

> **Architecture decisions live in [`docs/adr/`](docs/adr/).** This file is the lexicon; the ADRs are the load-bearing choices.

## Product surface

| Term | Definition |
|---|---|
| **Grug** | The bot. A hosted GitHub App at `grug.lol` that gates pull requests against a set of process checks. Open-source AGPL-3.0; self-host path in [`docs/SELF_HOST.md`](docs/SELF_HOST.md). |
| **Grug Boss** | Public-facing name of the GitHub App users install on their repos. Same product as Grug; "Grug Boss" is the GitHub Marketplace listing handle. |
| **Persona** | A bounded behavior surface Grug applies per PR — a single grug of the tribe, each with one job. Five run today: **Chief** (DoR gate), **Elder** (code reviewer), **Guard** (security, #466), **Warder** (release tracer, #471), **Pulse** (scheduled nudge, #472). FIVE run today: Chief, Elder, **Guard** (security, #466), **Warder** (release-manager tracer, #471, default off), and **Pulse** (the scheduled stuck-PR nudge, #472, default off). Roadmap: **Smasher** (bug-hunt) - being built as capabilities (Trial #469 mutation-testing, Omen #470 DD-fusion). Personas are per-repo togglable. Each has a **caveman name** (Chief, Elder, …) — the canonical outward identity used in the Activity feed, dashboard, and new code — and a **historical code key** (`tpm`, `code_reviewer`, …) still carried by the legacy persona dirs + config fields (`tpm_enabled`). New code uses the caveman name; the key→name map is resolved in ONE place at the write boundary. _Avoid_: calling the personas by their code keys in user-facing surfaces or new feed code. |
| **Persona registry** | The declarative table of personas - `REGISTRY: tuple[PersonaSpec, ...]` in `personas/registry.py` (shared). One `PersonaSpec` row per persona: key, canonical caveman name, check-run name, config flags + defaults, dispatch style (inline/async), missing-repo policy, handled events, and the `dispatch_module` whose `dispatch_pull_request(ctx: PullRequestContext)` the webhook dispatcher calls. `dispatcher._handle_pull_request` iterates this table; adding a persona = one spec entry + one `personas/<key>/webhook_dispatch.py` module + its `_DEFAULT_PERSONA_CONFIG` keys, with no dispatcher or store edits (ADR-0010, spec section "Persona dispatch - the registry" in `specs/DESIGN.md`). |
| **Chief** (the **TPM** persona) | The grug who leads the tribe. Before the tribe leave the cave to hunt, Chief ask: the hunt have a name? enough meat counted? the path home known? If the plan not whole, tribe not go. Chief not read the code — Chief check the **plan** is ready. = the static **Definition-of-Ready (DoR) check** (`Grug — Definition of Ready`), which blocks merge on a malformed PR body. Code dir `personas/tpm/` (key `tpm`); canonical name **`chief`**. The companion **Scope review** half is wired but NOT shipped — see "Roadmap" rows. See [`services/_shared/personas/tpm/`](services/_shared/personas/tpm/). |
| **Elder** (the **code-reviewer** persona) | The oldest grug, many winters of bad code behind him — Elder seen many a grug fall in the same pit. Elder read your markings one line at a time and name the bad omen before it bite: the null in the dark, the race by the river, the error swallowed in silence. = the LLM **code review** pass (`Grug — Code Review`), advisory by default. Production review jobs enter the durable rerun FIFO, wait for a 90-second quiet snapshot, and cancel/re-enqueue when base, head, title, or body changes before publication. The default `deep` pass gives the recall prompt and untrusted PR title/body intent to BOTH Poolside and OpenRouter (review-only Opus 4.7 with high-effort reasoning), then merges deduplicated findings with backend/model provenance. Both deep passes must succeed before the snapshot is marked complete; a one-backend result is provisional and retried. Judge and write-authorized maintainer reaction labels fan out only to the producer spans that actually exist. Confirmed reactions refresh positive practices/examples; false positives become bounded AVOID guidance. `GRUG_REVIEW_DEPTH=fast` restores the one-primary-plus-fallback path without changing the lighter OpenRouter model used by the judge, Teller, or `/grug ask`. (The deterministic security suite that used to ride inside Elder is now **Guard**, #466/ADR-0012.) Code dir `personas/code_reviewer/` (key `code_reviewer`); canonical name **`elder`** (already `_PERSONA = "Elder"` in dispatch). Speaks in the wise-caveman voice (the `_VOICE` clause in `code_review_prompt.py`). |
| **Guard** (the security persona) | The grug who watch the cave mouth at night. Guard sniff every bundle the tribe carry in: leaked secret smell, sick dependency, weak markings, open door in the camp wall. Evil shall not pass. = the deterministic **security suite** (`Grug — Guard`, #466/ADR-0012): SAST (`sast.py`), dependency-CVE (`sca.py`), committed secrets (`secret_scan.py`), IaC misconfig (`iac_scan.py`) -> the LLM **exploitability judge** (`judge_candidates`) -> Guard's own check-run + inline review, advisory by default (`guard_blocking`). Detector files stay under `personas/code_reviewer/` (ownership by import); recall/precision tracked by the SAST benchmark. Code dir `personas/guard/` (key `guard`); canonical name **`guard`**. |
| **Warder** (the release persona) | The grug who keep the gate ledger. When a hunt merge to the main trail, Warder draft the scroll: what changed since the last carved mark (changelog grouped from Conventional-Commit subjects) and how big the next mark should be (semver hint). = the "Grug — Warder" check-run on merged PRs (#471 tracer; advisory, `warder_enabled` default off). Code dir `personas/warder/` (key `warder`). Deploy gating + Release creation = later slices. |
| **Pulse** (the scheduled persona) | The grug who walk the camp at night and poke sleeping hunts. Every poller tick (15 min cadence), Pulse find open PRs quiet 7+ sunrises whose plan Chief already blessed, and leave ONE gentle nudge per week ("Grug see PR sleep seven sunrises. Tribe forget?"). = `personas/pulse/nudge.py` on the grug-poller CronJob (#472 tracer; `pulse_enabled` default off, hard-capped per run, store-claim idempotent). The first non-webhook persona. |
| **Teller** (the walkthrough persona) | The grug who tell the tale of the hunt before the tribe judge it. Before Elder read close, Teller stand and speak: what the hunt mean, what ground it cross, how big the telling will be. = the PR **walkthrough** comment (#554, epic #522): AI intent summary + per-file blurbs (one bounded LLM call, degrades to a deterministic summary), a changed-files table, a DETERMINISTIC mermaid module diagram (never model-authored - no injection surface), and a review-effort chip. One upsert-by-marker comment, posted on open and edited in place on synchronize; comment-only, no check-run (same shape as Pulse). Code dir `personas/walkthrough/` (key `walkthrough`); canonical name **`teller`**. |

## Process-gate concepts

| Term | Definition |
|---|---|
| **Definition of Ready (DoR)** | The standard a PR description must meet before it can merge. Enforced as a set of `CheckResult`s combined into a single GitHub check-run named `Grug — Definition of Ready`. |
| **DoR check** | Individual rule: `why`, `acceptance`, `estimate`, `scope-fence`, `issue-link`. Defined in [`services/_shared/personas/tpm/dor_checks.py`](services/_shared/personas/tpm/dor_checks.py) (shared - services/_shared/, ADR-0014). The five rule names are the prose label — there is no `DoRCheck` class; rules are functions returning `CheckResult`. Four checks are blocking (`why`, `acceptance`, `estimate`, `scope-fence`); one is advisory (`issue-link`). |
| **CheckResult** | Outcome of one `DoR check` against one PR body. Frozen dataclass — fields `name: str`, `passed: bool`, `detail: str`. Pass/fail only — no third "warn" state. Whether a failed check blocks merge is decided at rollup time by `_ADVISORY_CHECKS`, not by the check itself. |
| **TpmEvaluation** | Aggregate result of running all `DoR check`s against one PR. Frozen dataclass returned by `evaluate_pull_request(...)` in `personas/tpm/persona.py`. Only checks NOT in `_ADVISORY_CHECKS` contribute to the `conclusion` field. Composes into a `CheckRunResult` for GitHub. |
| **CheckRunResult** | Frozen dataclass that maps directly onto GitHub's Checks API `POST /repos/{owner}/{repo}/check-runs` payload. Carries the `status=completed` ↔ `conclusion` cross-field invariant — enforced in `__post_init__`. See [`services/_shared/github_checks_client.py`](services/_shared/github_checks_client.py). |
| **`post_check_run` (publisher)** | Module-level function in `github_checks_client.py` that POSTs a `CheckRunResult` to GitHub. The acceptance-criteria spelling "CheckRunPublisher" is the *concept name* — the actual identifier is a function, not a class. |
| **Scope review (roadmap)** | Advisory LLM pass over PR title + body. Wired as a `poolside_client.py` hook called from `evaluate_pull_request(...)` per the `personas/tpm/__init__.py` docstring, but **`poolside_client.py` does not exist in the repo today** — feature is roadmap-only. Intended behavior: flag title↔body mismatch, AC testability, scope-creep, XL inflation; posted as a comment, never blocking. |

## Activity-feed concepts

| Term | Definition |
|---|---|
| **Check verdict** | The recorded outcome of one **Persona**'s check-run on one PR at one moment — the atom of the **Activity feed** ("What Grug did"). **Append-only**: a re-review appends a new Check verdict, it never mutates the prior one. Persisted as a `CheckVerdictRecord` storing the persona's **raw result** — `persona` (caveman key `chief`/`elder`), `repo`, `pr_number`, `conclusion`, a real `summary` line, `findings_count`, `blocking`, and `degraded_reason` — NOT a pre-collapsed badge, so history stays recomputable if the **Verdict** rules change. Distinct from **CheckResult** (one DoR rule), **CheckRunResult** (the outbound GitHub payload), and `ReactionVerdict` (the human 👍/👎). _Avoid_: "activity event", "run record". |
| **Verdict** | The at-a-glance triage badge **derived** from a **Check verdict** by one shared pure function — the only place the mapping lives. Four values: `block` (PR gated — a blocking check failed), `warn` (advisory flag — Elder found something while in advisory mode), `pass` (clean), `errored` (Grug could not evaluate — degraded run / LLM outage; **never** rendered as `pass`, per the "no lies" rule). Derived server-side in the `/activity` endpoint and rendered verbatim by the frontend (never re-derived). Denormalized onto the `CheckVerdictRecord` for cheap store-side filtering, but the raw facts remain canonical. _Avoid_: conflating with GitHub's `conclusion` (`success/failure/neutral/…`), which is one input to the Verdict, not the Verdict. |
| **Re-run** | Operator-triggered re-dispatch of ONE **Persona**'s check on a PR — exposed as a button on `errored` Activity rows only (a job that produced no usable result). Reviews the PR's **current** head (not the stale commit that errored) and upserts the `CheckVerdictRecord` for `(persona, current head_sha)`: an unchanged PR **heals the failed row in place** (`errored` → real verdict), a moved-on PR **appends** a fresh row and leaves the `errored` one as history. grug's only **backfill** path for degraded jobs — there is otherwise no replay (webhook deliveries are idempotent via `claim_delivery`, so re-delivering the original event is a no-op). |

## Enforcement concepts

| Term | Definition |
|---|---|
| **GitHub Ruleset** | A GitHub Repository Ruleset that requires specific status checks to pass before merging. Grug creates rulesets to enforce its DoR check on the default branch. Managed via the Repository Rulesets API (`POST/GET/DELETE /repos/{owner}/{repo}/rulesets`). See [`services/_shared/github_rulesets_client.py`](services/_shared/github_rulesets_client.py) (shared - services/_shared/, ADR-0014). |
| **Grug-managed ruleset** | A ruleset whose `name` starts with the prefix `Grug —`. This prefix is the ownership marker: `detect_enforcement()` uses it to distinguish rulesets Grug created from externally-managed ones. |
| **EnforcementState** | Literal type — `"grug_managed" \| "external" \| "none"`. Returned by `detect_enforcement()`. `grug_managed`: at least one `Grug —`-prefixed ruleset exists with `required_status_checks` matching the check name. `external`: no grug-managed ruleset, but the check is enforced via a non-Grug ruleset or legacy branch protection. `none`: check is not enforced anywhere. |
| **`detect_enforcement()`** | Module-level function in `github_rulesets_client.py` that determines the enforcement state for a given status check. Queries both the Rulesets API and the legacy Branch Protection API (`GET /repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks`) to cover repos that haven't migrated to rulesets. |
| **Legacy branch protection** | Pre-rulesets mechanism for requiring status checks. Still active on many repos. `detect_enforcement()` checks this as a fallback when no ruleset-based enforcement is found. |
| **`enforcement.py`** | Shared module (`services/_shared/`) containing enforcement lifecycle functions. `ensure_enforcement()` detects current state and creates a Grug-managed ruleset if none exists; `remove_enforcement()` deletes it. Both persist the `enforcement_ruleset_id` on the `RepoConfig` store row. See [`services/_shared/enforcement.py`](services/_shared/enforcement.py). |
| **`ensure_enforcement()`** | Idempotent function: detect → skip if `grug_managed` or `external` → create ruleset → store `enforcement_ruleset_id`. Called on installation created (webhook) and persona enable (API toggle). |
| **`remove_enforcement()`** | Deletes the Grug-managed ruleset by ID (read from `RepoConfig`) and clears the stored `enforcement_ruleset_id`. Called on persona disable (API toggle). |
| **`enforcement_ruleset_id`** | Optional integer field on the `RepoConfig` store row. Stores the GitHub ruleset ID of the Grug-managed enforcement ruleset for quick lookup during delete. `None` means no Grug-managed ruleset exists. |
| **`force_disable_enforcement`** | Optional boolean field on the `RepoConfig` store row, default `False`. When `True`, the self-healing loop skips re-creation of a deleted Grug-managed ruleset. Escape hatch for users who intentionally remove enforcement without disabling the TPM persona. |
| **`heal_enforcement()`** | Module-level function in `enforcement.py`. Called from dispatcher when a `repository_ruleset` deleted event fires for a Grug-managed ruleset. Clears the stale `enforcement_ruleset_id`, delegates to `ensure_enforcement()` for idempotent re-creation, and emits an `enforcement_healed` structured log with old + new ruleset IDs. Skipped when `force_disable_enforcement` is `True` or TPM persona is disabled. |
| **Self-healing** | Reconciliation loop: when a Grug-managed ruleset is externally deleted, the webhook re-creates it if the repo still wants enforcement. Triggered by `repository_ruleset` webhook event with `action=deleted`. The `force_disable_enforcement` flag on `RepoConfig` is the opt-out. |

## Identity & authorization concepts

| Term | Definition |
|---|---|
| **UserIdentity** | The GitHub user behind a session. Identifier: `github_user_id` (integer, stable across login renames). Definitions in `services/_shared/adapters/user_store.py`. |
| **UserWithTokens** | `UserIdentity` plus an attached `oauth_*_blob` (KMS-envelope-encrypted access + refresh tokens). Used only by the api service — webhook never needs it. |
| **Installation** | A `Grug` install on one GitHub account or organization. Identified by `install_id` (GitHub-issued integer). Carries metadata: `account_login`, `account_type` (User/Organization), `installed_at`, `installed_by_user_id`. |
| **RepoConfig** | Per-repo settings stored under an `Installation`. Fields: `tpm_enabled: bool` (default `True`) and `enforcement_ruleset_id: int \| None` (GitHub ruleset ID managed by Grug, default `None`). Storage shape in [`services/_shared/adapters/install_store.py`](services/_shared/adapters/install_store.py). When more personas ship, the field set grows (e.g. `code_reviewer_enabled`); the `_DEFAULT_PERSONA_CONFIG` dict is the source of truth. |
| **AllowlistGate** | Defense-in-depth check: webhook handler refuses to act on any `Installation` whose `installed_by_user_id` is not in the `allowlist` set on the user's store row. Independent of GitHub App's public/private listing. Bypass guard for the hosted SaaS while ramp is closed. |
| **AppJWT** | RSA-signed JWT identifying Grug as a GitHub App (10-min TTL). Generated from the App private key (loaded from SSM SecureString `/grug/github-app-private-key`). Used to exchange for installation tokens. |
| **InstallToken** | Short-lived (~1h TTL) token returned by `POST /app/installations/{id}/access_tokens`. Lets Grug act on behalf of the installation against the GitHub API. Cached per-`Installation` in `TokenCache`. |
| **TokenCache** | Get/put/invalidate store for `AppJWT` and `InstallToken`s. Today: single in-process implementation (`InMemoryTokenCache`) — see ADR-0001. A persistent cache is still planned (PRD #21 Q17); the original DynamoDB-backed sketch predates the #354 Postgres store swap, so it would now be Postgres-backed. |
| **with_install_token_retry** | Helper that wraps a GitHub API call so that on 401 it invalidates the cached `InstallToken`, fetches a fresh one, and retries once. Lives alongside the AppJWT machinery; couples token rotation with call sites today (adapter extraction tracked in #510, per the #142 walk / ADR-0015). |

## Persistence concepts

| Term | Definition |
|---|---|
| **Single-table layout (`grug_kv`)** | Two key shapes co-existing in one Postgres table (`grug_kv`, was DynamoDB `grug-main` pre-#354): `PK=USER#<github_user_id> SK=META` (the `UserIdentity` row, optionally `UserWithTokens` + `allowlisted`) and `PK=INST#<install_id> SK=META` (the `Installation` row) plus `PK=INST#<install_id> SK=REPO#<repo_id>` (one `RepoConfig` per repo). Key/attribute semantics preserved exactly across the port. Schema constants in [`services/_shared/adapters/pg_install_store.py`](services/_shared/adapters/pg_install_store.py); `install_store.py`/`user_store.py` are re-export facades. |
| **KMS envelope** | Per-`UserIdentity` data-encryption-key generated via `kms:GenerateDataKey` and used (AES-GCM, 96-bit nonce) to encrypt OAuth refresh + access tokens. Wrapped DEK stored alongside the ciphertext. Only the api service calls KMS at app-level; webhook never decrypts user tokens. Documented at [`infra/pulumi/components/kms_cmk.py`](infra/pulumi/components/kms_cmk.py). |
| **CredentialBlobCorrupt** | Exception raised when an encrypted blob in the store can't be decrypted (key-version drift, tampering, deliberate test fixture). Handler must idempotently clean up — see the "idempotency check after corruption-empty fallthrough" audit pattern. |
| **UserStateCorrupt** | Same shape as `CredentialBlobCorrupt`, but for non-secret user state fields that fail invariant checks at read time. |

## Auth-boundary concepts

| Term | Definition |
|---|---|
| **`X-Grug-CF-Secret`** | HTTP request header injected by the CF Workers (`infra/cloudflare/workers/grug-{api,webhook}-host-rewrite/worker.js`) on every upstream request. Since the #354 cutover the Worker rewrites `Host` to the in-cluster upstream (the tunnel-served hostname in SSM `/grug/{api,webhook}-upstream-host`) instead of the retired Lambda Function URL; the request transits the Cloudflare tunnel to the in-cluster Service. Validated by `CfAuthMiddleware` on the pod side. The header name is templated into the worker.js by `deploy.sh` so deploy.sh is the single source of truth. |
| **`GRUG_CF_SECRET`** | CF Worker secret binding name. The Workers read `env.GRUG_CF_SECRET` and inject it as the `X-Grug-CF-Secret` header. `deploy.sh` PUTs the binding from SSM `/grug/cf-shared-secret` on every Worker script upload. |
| **`/grug/cf-shared-secret`** | SSM SecureString holding the CF→AWS auth-boundary shared secret. Pulumi-managed via `infra/pulumi/components/cf_shared_secret.py` (`random.RandomPassword`, 64-char lowercase alphanumeric). Rotation: bump `keepers["version"]`, `pulumi up`, re-run `deploy.sh`. |
| **`CfAuthMiddleware`** | Starlette/FastAPI middleware in shared module `cf_auth.py` (services/_shared/). Reads the SSM secret at cold start via the `GRUG_CF_SHARED_SECRET_SSM` env var, validates `X-Grug-CF-Secret` on every non-`/livez` request using `hmac.compare_digest`. Fail-closed by default (audit #4): unset env var or a failed/empty SSM read -> 503; `GRUG_CF_AUTH_FAIL_OPEN=1` is the bring-up-only escape hatch. |

## Cross-service primitives (shared)

The single copy of every cross-service module lives in `services/_shared/` - a PYTHONPATH root shared by both services (import paths unchanged: `adapters.*`, `personas.*`, `observability`, ...). Extracted at #77 when the rule-of-three fired; see [ADR-0014](docs/adr/0014-shared-package-extraction.md) (which supersedes the [ADR-0001](docs/adr/0001-mirror-with-rule-of-three-deferral.md) mirror deferral). A package is wholly owned by one root: service-specific modules inside shared packages (api's user stores, webhook's Smasher trial_*) live in `_shared/` with API-ONLY / WEBHOOK-ONLY markers in their opening docstring lines and are only lazy-imported by their service. A representative sample:

| Module | Purpose |
|---|---|
| `cf_auth.py` | CF→AWS auth-boundary middleware. Validates `X-Grug-CF-Secret` header against the SSM-loaded shared secret; `/livez` exempt; fail-closed by default (audit #4) with `GRUG_CF_AUTH_FAIL_OPEN=1` as the bring-up escape hatch. |
| `observability.py` | DD-extension-aware logger + JSON formatter. Reads `DD_SERVICE`, `DD_ENV`, `GRUG_LOG_LEVEL`. |
| `secrets_loader.py` | SSM SecureString reads at cold start (`GITHUB_APP_ID_SSM`, `GITHUB_APP_WEBHOOK_SECRET_SSM`, etc.). |
| `github_checks_client.py` | Thin `httpx`-based wrapper over GitHub's Checks API; carries the `CheckRunResult` dataclass. |
| `github_rulesets_client.py` | Thin `httpx`-based wrapper over GitHub's Repository Rulesets API + legacy branch protection; carries `EnforcementState` type and `detect_enforcement()`. |
| `enforcement.py` | Enforcement lifecycle — `ensure_enforcement()` and `remove_enforcement()` wired from dispatcher + API. |
| `adapters/install_store.py` | Facade re-exporting `pg_install_store.py` (Postgres single-table CRUD for `Installation` + `RepoConfig` + `AllowlistGate` reads) — import/patch paths preserved from the DDB era. |
| `ports/token_cache.py` | `TokenCache` Protocol + `InMemoryTokenCache` impl. |
| `personas/tpm/dor_checks.py` | The 5 `DoR check` rules + the `CheckResult` dataclass. |
| `personas/tpm/persona.py` | `TpmEvaluation` dataclass + `evaluate_pull_request(...)` entry point. |

## Infrastructure concepts

| Term | Definition |
|---|---|
| **Kubernetes deploy (#354)** | Deploy shape since the cutover: each service is a Docker image (built natively for arm64) pushed to an in-cluster registry; the `grug` namespace runs Deployments `grug-api` / `grug-webhook` / `grug-consumer` + CronJob `grug-poller` (api runs its own image; the rest share the webhook image). `deploy.k8s.yml` builds, seeds Secrets from SSM, and `kubectl apply -k k8s/`. (Was AWS Lambda image-mode + ECR pre-#354.) |
| **Cloudflare tunnel + Worker** | `api.grug.lol` / `webhook.grug.lol` resolve via CF DNS to the per-service `<service>-host-rewrite` Worker, which injects `X-Grug-CF-Secret` and rewrites `Host` to the in-cluster upstream; a `cloudflared` tunnel transports that to the k8s Service on `:8080`. App-layer gating is the CF shared secret + HMAC + signed session (no Cognito). (Was a CF Worker → Lambda Function URL proxy pre-#354.) |
| **Postgres store (CNPG)** | The single-table store `grug_kv` on a shared CloudNativePG cluster (`GRUG_DATABASE_URL`, `psycopg`). Replaced the DynamoDB `grug-main` table at #354; key/attribute semantics preserved exactly. |
| **Pulumi stack** | One stack per environment. Manages the AWS-side infra (SSM refs, SQS, KMS, OIDC, IAM users, DD monitors/dashboard/RUM); the k8s manifests are applied by `deploy.k8s.yml`, not Pulumi. Stack/project structure under [`infra/pulumi/`](infra/pulumi/). |
| **Grugboard** | GitHub Projects (v2) board at https://github.com/users/githumps/projects/1. Target of the future `Pulse (roadmap)` persona's label sync + issue reprefixing. |

## Operational concepts

| Term | Definition |
|---|---|
| **Cutover** | Migration path from a self-hosted to a hosted setup, or between Pulumi stacks. Runbook at [`docs/CUTOVER.md`](docs/CUTOVER.md). |
| **HITL prerequisite** | A human-in-the-loop step that must precede automated work (e.g. App registration on github.com, SSM secret pre-load). Documented in [`docs/HITL_PREREQUISITES.md`](docs/HITL_PREREQUISITES.md). |
| **Self-host** | Operator deploys Grug against their own AWS + Cloudflare account. Step-by-step in [`docs/SELF_HOST.md`](docs/SELF_HOST.md). AGPL-3.0 network-service compliance applies. |

## Vocabulary debt (named for future cleanup)

These terms exist in the codebase but are inconsistent or under-named. Resolving them is out of scope for the initial CONTEXT.md authoring; track in dedicated issues.

- **`with_install_token_retry`** — verbose helper name; better as a method on a `TokenedGitHubClient` adapter (no longer deferred: extracted to #510 by the #142 walk, ADR-0015).
- **`get_pool()` lazy init** — double-checked-lock pool bootstrap in `pg_base.py` deferring DB connection past import (same rationale as the DDB-era `_LazyTable` it replaced).
- **No name for the SPA's session shape** — `web/src/` consumes the api service's `/me` payload but the SPA's TS types don't have a corresponding `Session` or `Viewer` concept. Add when frontend changes touch session state.

---

## Datadog LLM Observability (DD LLMObs) reference

Grug uses **Datadog LLM Observability** to track Elder model calls and collect trusted human feedback from reactions on inline comments.

### Key terminology

| Term | Definition |
|---|---|
| **ML app name** | Datadog organizes traces by ML app. Grug uses `grug-elder` (set on the webhook and consumer workloads). |
| **Review span name** | Each Elder producer call creates an `elder_code_review` LLMObs span in `services/_shared/llm_client.py`. A deep review can create both Poolside and OpenRouter spans for one snapshot. |
| **DD_SERVICE tag** | The workload identity is `grug-webhook` for webhook handling and `grug-consumer` for durable review execution. This is APM service identity, not the LLMObs ML app. |
| **Reaction polling** | The `grug-poller` CronJob polls reactions on Grug review comments every 15 minutes. Only reactions from users with repository write/admin permission can steer repository learning. |
| **Producer provenance** | A merged finding retains every backend/model origin. Judge and reaction evaluations attach only to exported spans that actually produced that finding. |
| **Annotation queue** | Human review workflow in Datadog UI. The `human_verdict` categorical label uses `false_positive` and `confirmed` values to validate judge accuracy over time. |

### How to inspect Grug traces with pup

```bash
# List all projects (Grug project has id e24e1215...)
pup llm-obs projects list -o json

# Create annotation queue for human review
pup llm-obs annotation-queues create --name="Grug human_verdict Reviews" \
  --project-id=<PROJECT_ID> \
  --label="verdict|categorical|false_positive,confirmed" \
  --has-assessment=true \
  --has-reasoning=true

# Search for Elder spans (filter by ML app)
pup llm-obs spans search --ml-app grug-elder --from 1h

# List annotation queues
pup llm-obs annotation-queues list --project-id <PROJECT_ID>
```

### How reactions become evaluations and learning

1. A maintainer reacts to a Grug inline comment.
2. `grug-poller` polls the GitHub reactions API.
3. `poll_and_annotate()` filters reactors by repository permission and classifies the trusted reaction.
4. `submit_reaction_annotation()` sends a `human_verdict` evaluation to each available producer span for that finding.
5. The stable comment evidence row refreshes repository practices: confirmed findings become positive guidance/examples; false positives become AVOID guidance.

### Running issues

- **No traces showing?** Check `DD_LLMOBS_ENABLED=true` on both Elder workloads and verify `elder_code_review` spans under `@ml_app:grug-elder`.
- **Reactions not appearing?** Confirm `grug-poller` is running and the reactor has repository write/admin permission. New reactions take up to 15 minutes to appear.
- **Annotations queue empty?** Search for producer spans first. A finding whose producer span failed to export remains learnable but is intentionally not attributed to a different model span.

---

*This file is alive. Add terms as the codebase grows. Renaming a concept = update CONTEXT.md in the same PR. See [ADR-0014](docs/adr/0014-shared-package-extraction.md) for the shared-package architecture decision.*
