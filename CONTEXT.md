# CONTEXT.md — grug domain glossary

The vocabulary used in `services/`, `infra/`, and `web/`. Terms map to identifiers a contributor will encounter while reading code. New contributors should be able to read this file and use the terms correctly without grepping. Drift between this file and the code is a bug; fix the file in the same PR that renames a concept.

> **Architecture decisions live in [`docs/adr/`](docs/adr/).** This file is the lexicon; the ADRs are the load-bearing choices.

## Product surface

| Term | Definition |
|---|---|
| **Grug** | The bot. A hosted GitHub App at `grug.lol` that gates pull requests against a set of process checks. Open-source AGPL-3.0; self-host path in [`docs/SELF_HOST.md`](docs/SELF_HOST.md). |
| **Grug Boss** | Public-facing name of the GitHub App users install on their repos. Same product as Grug; "Grug Boss" is the GitHub Marketplace listing handle. |
| **Persona** | A bounded behavior surface Grug applies per PR. v1 ships exactly one: **TPM** (static Definition-of-Ready check). v1.5+ roadmap (per PRD #21) adds `code-reviewer`, `release-manager`, `stuck-PR-pulse`, and the LLM scope-review half of TPM. Personas are per-repo togglable. |
| **TPM persona** | Today's only persona. Static **Definition-of-Ready (DoR) check** that blocks merge if PR body is malformed. The companion **Scope review** half is wired as a `poolside_client.py` hook in `evaluate()` per the `__init__.py` docstring but is NOT shipped — see "Roadmap" rows. See [`services/{api,webhook}/personas/tpm/`](services/api/personas/tpm/). |
| **Pulse (roadmap)** | Scheduled (non-PR-triggered) persona work, named `stuck-PR-pulse` in the future-persona list in `services/webhook/main.py`. Not implemented. Intent: weekly issue-grooming sweep against Grugboard. |

## Process-gate concepts

| Term | Definition |
|---|---|
| **Definition of Ready (DoR)** | The standard a PR description must meet before it can merge. Enforced as a set of `CheckResult`s combined into a single GitHub check-run named `Grug — Definition of Ready`. |
| **DoR check** | Individual rule: `why`, `acceptance`, `estimate`, `scope-fence`, `issue-link`. Defined in [`services/{api,webhook}/personas/tpm/dor_checks.py`](services/api/personas/tpm/dor_checks.py) (mirrored — see ADR-0001). The five rule names are the prose label — there is no `DoRCheck` class; rules are functions returning `CheckResult`. Four checks are blocking (`why`, `acceptance`, `estimate`, `scope-fence`); one is advisory (`issue-link`). |
| **CheckResult** | Outcome of one `DoR check` against one PR body. Frozen dataclass — fields `name: str`, `passed: bool`, `detail: str`. Pass/fail only — no third "warn" state. Whether a failed check blocks merge is decided at rollup time by `_ADVISORY_CHECKS`, not by the check itself. |
| **TpmEvaluation** | Aggregate result of running all `DoR check`s against one PR. Frozen dataclass returned by `evaluate_pull_request(...)` in `personas/tpm/persona.py`. Only checks NOT in `_ADVISORY_CHECKS` contribute to the `conclusion` field. Composes into a `CheckRunResult` for GitHub. |
| **CheckRunResult** | Frozen dataclass that maps directly onto GitHub's Checks API `POST /repos/{owner}/{repo}/check-runs` payload. Carries the `status=completed` ↔ `conclusion` cross-field invariant — enforced in `__post_init__`. See [`services/{api,webhook}/github_checks_client.py`](services/api/github_checks_client.py). |
| **`post_check_run` (publisher)** | Module-level function in `github_checks_client.py` that POSTs a `CheckRunResult` to GitHub. The acceptance-criteria spelling "CheckRunPublisher" is the *concept name* — the actual identifier is a function, not a class. |
| **Scope review (roadmap)** | Advisory LLM pass over PR title + body. Wired as a `poolside_client.py` hook called from `evaluate_pull_request(...)` per the `personas/tpm/__init__.py` docstring, but **`poolside_client.py` does not exist in the repo today** — feature is roadmap-only. Intended behavior: flag title↔body mismatch, AC testability, scope-creep, XL inflation; posted as a comment, never blocking. |

## Enforcement concepts

| Term | Definition |
|---|---|
| **GitHub Ruleset** | A GitHub Repository Ruleset that requires specific status checks to pass before merging. Grug creates rulesets to enforce its DoR check on the default branch. Managed via the Repository Rulesets API (`POST/GET/DELETE /repos/{owner}/{repo}/rulesets`). See [`services/{api,webhook}/github_rulesets_client.py`](services/api/github_rulesets_client.py) (mirrored — see ADR-0001). |
| **Grug-managed ruleset** | A ruleset whose `name` starts with the prefix `Grug —`. This prefix is the ownership marker: `detect_enforcement()` uses it to distinguish rulesets Grug created from externally-managed ones. |
| **EnforcementState** | Literal type — `"grug_managed" \| "external" \| "none"`. Returned by `detect_enforcement()`. `grug_managed`: at least one `Grug —`-prefixed ruleset exists with `required_status_checks` matching the check name. `external`: no grug-managed ruleset, but the check is enforced via a non-Grug ruleset or legacy branch protection. `none`: check is not enforced anywhere. |
| **`detect_enforcement()`** | Module-level function in `github_rulesets_client.py` that determines the enforcement state for a given status check. Queries both the Rulesets API and the legacy Branch Protection API (`GET /repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks`) to cover repos that haven't migrated to rulesets. |
| **Legacy branch protection** | Pre-rulesets mechanism for requiring status checks. Still active on many repos. `detect_enforcement()` checks this as a fallback when no ruleset-based enforcement is found. |
| **`enforcement.py`** | Mirrored module containing enforcement lifecycle functions. `ensure_enforcement()` detects current state and creates a Grug-managed ruleset if none exists; `remove_enforcement()` deletes it. Both persist the `enforcement_ruleset_id` on the `RepoConfig` DDB row. See [`services/{api,webhook}/enforcement.py`](services/api/enforcement.py). |
| **`ensure_enforcement()`** | Idempotent function: detect → skip if `grug_managed` or `external` → create ruleset → store `enforcement_ruleset_id`. Called on installation created (webhook) and persona enable (API toggle). |
| **`remove_enforcement()`** | Deletes the Grug-managed ruleset by ID (read from `RepoConfig`) and clears the stored `enforcement_ruleset_id`. Called on persona disable (API toggle). |
| **`enforcement_ruleset_id`** | Optional integer field on `RepoConfig` DDB row. Stores the GitHub ruleset ID of the Grug-managed enforcement ruleset for quick lookup during delete. `None` means no Grug-managed ruleset exists. |

## Identity & authorization concepts

| Term | Definition |
|---|---|
| **UserIdentity** | The GitHub user behind a session. Identifier: `github_user_id` (integer, stable across login renames). Definitions in `services/api/adapters/user_store.py`. |
| **UserWithTokens** | `UserIdentity` plus an attached `oauth_*_blob` (KMS-envelope-encrypted access + refresh tokens). Used only by the api Lambda — webhook never needs it. |
| **Installation** | A `Grug` install on one GitHub account or organization. Identified by `install_id` (GitHub-issued integer). Carries metadata: `account_login`, `account_type` (User/Organization), `installed_at`, `installed_by_user_id`. |
| **RepoConfig** | Per-repo settings stored under an `Installation`. Fields: `tpm_enabled: bool` (default `True`) and `enforcement_ruleset_id: int \| None` (GitHub ruleset ID managed by Grug, default `None`). Storage shape in [`services/{api,webhook}/adapters/install_store.py`](services/api/adapters/install_store.py). When more personas ship, the field set grows (e.g. `code_reviewer_enabled`); the `_DEFAULT_PERSONA_CONFIG` dict is the source of truth. |
| **AllowlistGate** | Defense-in-depth check: webhook handler refuses to act on any `Installation` whose `installed_by_user_id` is not in the `allowlist` set on the user's DDB row. Independent of GitHub App's public/private listing. Bypass guard for the hosted SaaS while ramp is closed. |
| **AppJWT** | RSA-signed JWT identifying Grug as a GitHub App (10-min TTL). Generated from the App private key (loaded from SSM SecureString `/grug/github-app-private-key`). Used to exchange for installation tokens. |
| **InstallToken** | Short-lived (~1h TTL) token returned by `POST /app/installations/{id}/access_tokens`. Lets Grug act on behalf of the installation against the GitHub API. Cached per-`Installation` in `TokenCache`. |
| **TokenCache** | Get/put/invalidate store for `AppJWT` and `InstallToken`s. Today: single in-process implementation (`InMemoryTokenCache`) — see ADR-0001 + planned `DdbTokenCache` per PRD #21 Q17. |
| **with_install_token_retry** | Helper that wraps a GitHub API call so that on 401 it invalidates the cached `InstallToken`, fetches a fresh one, and retries once. Lives alongside the AppJWT machinery; couples token rotation with call sites today (issue #142 may revisit). |

## Persistence concepts

| Term | Definition |
|---|---|
| **DynamoDB single-table layout** | Two key shapes co-existing in one DDB table: `PK=USER#<github_user_id> SK=META` (the `UserIdentity` row, optionally `UserWithTokens` + `allowlisted`) and `PK=INST#<install_id> SK=META` (the `Installation` row) plus `PK=INST#<install_id> SK=REPO#<repo_id>` (one `RepoConfig` per repo). Schema constants in [`services/{api,webhook}/adapters/install_store.py`](services/api/adapters/install_store.py). |
| **KMS envelope** | Per-`UserIdentity` data-encryption-key generated via `kms:GenerateDataKey` and used (AES-GCM, 96-bit nonce) to encrypt OAuth refresh + access tokens. Wrapped DEK stored alongside the ciphertext. Only the api Lambda calls KMS at app-level; webhook never decrypts user tokens. Documented at [`infra/pulumi/components/kms_cmk.py`](infra/pulumi/components/kms_cmk.py). |
| **CredentialBlobCorrupt** | Exception raised when an encrypted blob in DDB can't be decrypted (key-version drift, tampering, deliberate test fixture). Handler must idempotently clean up — see the "idempotency check after corruption-empty fallthrough" audit pattern. |
| **UserStateCorrupt** | Same shape as `CredentialBlobCorrupt`, but for non-secret user state fields that fail invariant checks at read time. |

## Cross-service primitives (mirrored)

The following eight modules exist as byte-identical copies under both `services/api/` and `services/webhook/`. See [ADR-0001](docs/adr/0001-mirror-with-rule-of-three-deferral.md) for the load-bearing reasoning.

| Module | Purpose |
|---|---|
| `observability.py` | DD-extension-aware logger + JSON formatter. Reads `DD_SERVICE`, `DD_ENV`, `GRUG_LOG_LEVEL`. |
| `secrets_loader.py` | SSM SecureString reads at cold start (`GITHUB_APP_ID_SSM`, `GITHUB_APP_WEBHOOK_SECRET_SSM`, etc.). |
| `github_checks_client.py` | Thin `httpx`-based wrapper over GitHub's Checks API; carries the `CheckRunResult` dataclass. |
| `github_rulesets_client.py` | Thin `httpx`-based wrapper over GitHub's Repository Rulesets API + legacy branch protection; carries `EnforcementState` type and `detect_enforcement()`. |
| `enforcement.py` | Enforcement lifecycle — `ensure_enforcement()` and `remove_enforcement()` wired from dispatcher + API. |
| `adapters/install_store.py` | DDB single-table CRUD for `Installation` + `RepoConfig` + `AllowlistGate` reads. |
| `ports/token_cache.py` | `TokenCache` Protocol + `InMemoryTokenCache` impl. |
| `personas/tpm/dor_checks.py` | The 5 `DoR check` rules + the `CheckResult` dataclass. |
| `personas/tpm/persona.py` | `TpmEvaluation` dataclass + `evaluate_pull_request(...)` entry point. |

## Infrastructure concepts

| Term | Definition |
|---|---|
| **AWS Lambda image-mode** | Deploy shape: per-service Docker image pushed to ECR, Lambda function points at the digest. Both services use this. Container brings DD extension + `datadog_lambda` wrapper. |
| **Lambda Function URL** | Public HTTPS endpoint AWS provisions for each Lambda. `AuthType=NONE` (we gate via app-layer HMAC + Cognito). Hosted at `api.grug.lol` / `webhook.grug.lol` via Cloudflare Worker proxy. |
| **Cloudflare Worker proxy** | Per-service `<service>-host-rewrite` Worker that rewrites incoming `<service>.grug.lol` requests to the Lambda Function URL. Lets us use friendly domains + WAF in front of Lambda. |
| **Pulumi stack** | One stack per environment. Today: `dev` only. Stack name and project structure under [`infra/pulumi/`](infra/pulumi/). |
| **Grugboard** | GitHub Projects (v2) board at https://github.com/users/githumps/projects/1. Target of the future `Pulse (roadmap)` persona's label sync + issue reprefixing. |

## Operational concepts

| Term | Definition |
|---|---|
| **Cutover** | Migration path from a self-hosted to a hosted setup, or between Pulumi stacks. Runbook at [`docs/CUTOVER.md`](docs/CUTOVER.md). |
| **HITL prerequisite** | A human-in-the-loop step that must precede automated work (e.g. App registration on github.com, SSM secret pre-load). Documented in [`docs/HITL_PREREQUISITES.md`](docs/HITL_PREREQUISITES.md). |
| **Self-host** | Operator deploys Grug against their own AWS + Cloudflare account. Step-by-step in [`docs/SELF_HOST.md`](docs/SELF_HOST.md). AGPL-3.0 network-service compliance applies. |

## Vocabulary debt (named for future cleanup)

These terms exist in the codebase but are inconsistent or under-named. Resolving them is out of scope for the initial CONTEXT.md authoring; track in dedicated issues.

- **`with_install_token_retry`** — verbose helper name; better as a method on a future `TokenedGitHubClient` adapter (deferred in issue #142).
- **`_LazyTable`** — descriptor pattern in `install_store.py` that defers boto3 init. Anti-pattern; documented rationale; defer-replace per #142.
- **No name for the SPA's session shape** — `web/src/` consumes the api Lambda's `/me` payload but the SPA's TS types don't have a corresponding `Session` or `Viewer` concept. Add when frontend changes touch session state.

---

*This file is alive. Add terms as the codebase grows. Renaming a concept = update CONTEXT.md in the same PR. See [ADR-0001](docs/adr/0001-mirror-with-rule-of-three-deferral.md) for the mirror-discipline architecture decision.*
