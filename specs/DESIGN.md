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
| **Elder persona** | The code-reviewer persona. Reads the PR diff and posts inline review comments + a check-run summary. Runs alongside TPM on every `pull_request` event (`opened`, `synchronize`, `ready_for_review`, `reopened`). The two personas dispatch INDEPENDENTLY — one failing does not skip the other. v1 modules: `personas/code_reviewer/diff_parser.py` (unified-diff → `DiffHunk`s) + `personas/code_reviewer/persona.py` (`evaluate_diff` rollup) + `personas/code_reviewer/dispatch.py` (orchestration — fetch diff, run LLM, publish via reviews+checks clients). LLM-driven via `llm_client.py` (Poolside + OpenRouter round-robin). Advisory by default (`code_reviewer_blocking=False` → check-run `conclusion=neutral`, review `event=COMMENT`); blocking flip via dashboard toggle. See specs 0015 + 0016 + [`services/{api,webhook}/personas/code_reviewer/`](services/api/personas/code_reviewer/). |
| **Pulse (roadmap)** | Scheduled (non-PR-triggered) persona work, named `stuck-PR-pulse` in the future-persona list in `services/webhook/main.py`. Not implemented. Intent: weekly issue-grooming sweep against Grugboard. |

## Process-gate concepts

| Term | Definition |
|---|---|
| **Definition of Ready (DoR)** | The standard a PR description must meet before it can merge. Enforced as a set of `CheckResult`s combined into a single GitHub check-run named `Grug — Definition of Ready`. |
| **DoR check** | Individual rule: `why`, `acceptance`, `estimate`, `scope-fence`, `issue-link`. Defined in [`services/{api,webhook}/personas/tpm/dor_checks.py`](services/api/personas/tpm/dor_checks.py) (mirrored — see ADR-0001). The five rule names are the prose label — there is no `DoRCheck` class; rules are functions returning `CheckResult`. |
| **CheckResult** | Outcome of one `DoR check` against one PR body. Frozen dataclass — fields `name: str`, `passed: bool`, `detail: str`. Pass/fail only — no third "warn" state. |
| **TpmEvaluation** | Aggregate result of running all `DoR check`s against one PR. Frozen dataclass returned by `evaluate_pull_request(...)` in `personas/tpm/persona.py`. Composes into a `CheckRunResult` for GitHub. |
| **CheckRunResult** | Frozen dataclass that maps directly onto GitHub's Checks API `POST /repos/{owner}/{repo}/check-runs` payload. Carries the `status=completed` ↔ `conclusion` cross-field invariant — enforced in `__post_init__`. See [`services/{api,webhook}/github_checks_client.py`](services/api/github_checks_client.py). |
| **`post_check_run` (publisher)** | Module-level function in `github_checks_client.py` that POSTs a `CheckRunResult` to GitHub. The acceptance-criteria spelling "CheckRunPublisher" is the *concept name* — the actual identifier is a function, not a class. |
| **Scope review (roadmap)** | Advisory LLM pass over PR title + body. Wired as a `poolside_client.py` hook called from `evaluate_pull_request(...)` per the `personas/tpm/__init__.py` docstring, but **`poolside_client.py` does not exist in the repo today** — feature is roadmap-only. Intended behavior: flag title↔body mismatch, AC testability, scope-creep, XL inflation; posted as a comment, never blocking. |

## Elder persona — code review

| Term | Definition |
|---|---|
| **`parse_diff`** | Pure function in `personas/code_reviewer/diff_parser.py` (mirrored — ADR-0001) that takes a unified-diff string and returns `tuple[DiffHunk, ...]`. Hand-rolled (no `unidiff` dep) — the subset we need is small and Lambda cold-start cost of the dep wouldn't pay back. Handles multi-file diffs, skips binary blocks, picks the new-side path on renames. Spec 0015 §Parse contract attests purity. |
| **`DiffHunk`** | Frozen dataclass — fields `file_path: str`, `new_start: int`, `new_lines: frozenset[int]`, `body: str`. `new_lines` is the set of new-file line numbers that were added or are context-with-a-removed-neighbor; the Elder anti-hallucination filter rejects LLM findings whose `(file, line)` is not in this set. `body` retains the raw @@-prefixed text for feeding back to the LLM as review context. |
| **`evaluate_diff`** | Pure function in `personas/code_reviewer/persona.py` (mirrored) that consumes `tuple[DiffHunk, ...]` + `LlmReviewResponse` (from `llm_client.py`) and produces a `CodeReviewEvaluation`. Drops findings outside the diff (anti-hallucination); maps the wire-format `llm_client.Finding` (`path`, `rule`) to the persona-level `Finding` (`file`, `rule_name`, plus `suggestion`). Spec 0015 §Evaluate contract attests purity. |
| **`Finding` (persona-level)** | Frozen dataclass distinct from `llm_client.Finding`. Fields: `file`, `line`, `severity: Literal["low","medium","high","critical"]`, `rule_name`, `message`, `suggestion`. Posted as GitHub inline review comments by the publisher slice. |
| **`CodeReviewEvaluation`** | Aggregate verdict from one Elder pass. Frozen dataclass — fields `findings: tuple[Finding, ...]`, `passed: bool`, `conclusion: CheckConclusion`. `passed = no high+critical findings`; medium+low are advisory. When the LLM didn't produce content (`kind in {no_diff, all_failed, parse_failed}`), `passed=True` + `conclusion=neutral` — Elder is advisory-first, infra flakiness must not block PRs. Composes 1:1 into a `CheckRunResult` (spec 0001). |
| **`post_review`** | Module-level function in `github_reviews_client.py` (mirrored) that POSTs a `ReviewResult` to `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`. Same pattern as `post_check_run`: no retry/no swallow — `with_install_token_retry` is the caller's responsibility. Spec 0016 §Schema + §Retry contracts. |
| **`ReviewResult`** | Frozen dataclass — fields `commit_id: str`, `event: Literal["COMMENT", "REQUEST_CHANGES"]`, `body: str`, `comments: tuple[InlineComment, ...]`. `event=COMMENT` is the advisory mode; `event=REQUEST_CHANGES` blocks merge. APPROVE + PENDING are intentionally not modeled (an LLM-backed reviewer should not auto-approve, and draft reviews don't publish). `commit_id` pins the review to a specific PR head so it cannot race against new pushes. |
| **`InlineComment`** | Frozen dataclass — fields `path: str`, `line: int` (NEW-side line, matches `DiffHunk.new_lines`), `body: str`. Constructor asserts `line >= 1` because GitHub's PR Reviews API 422s on `line=0`. |
| **`ReviewEvent`** | Literal type — `"COMMENT" \| "REQUEST_CHANGES"`. Restricted subset of GitHub's full enum (APPROVE/COMMENT/REQUEST_CHANGES/PENDING) — see `ReviewResult` for why. |
| **Prompt library** | The Elder review system prompt, in `code_review_prompt.py` (mirrored, a sibling of `llm_client.py` — its consumer — so no import cycle and no persona-up import). `ReviewRule` frozen dataclass (name / bug_class / description / bad+good example / default severity); `RULES` is ≥15 bug-class rules seeded from the /audit skill stages; `build_system_prompt()` renders them deterministically + appends the `Finding` JSON output contract. `llm_client._SYSTEM_PROMPT = build_system_prompt()` at import. Standalone so prompt variants A/B-test (DD LLM Obs) without touching dispatch. |
| **LLM-as-a-judge** | Quality-feedback loop. After a review publishes, `personas/code_reviewer/judge.run_judge` grades each surviving finding via a SECOND LLM call (`llm_client.judge_findings`, own `elder_judge` DD span) and submits a per-finding `is_real_bug` categorical evaluation to DD LLM Obs (`llm_client.submit_finding_evaluation`), attached to the original review span. Best-effort: runs post-publish, never raises, never alters the review outcome. Gated on findings-present + review-span-present. Builds ground-truth for prompt optimization. |
| **`FindingJudgement`** | Frozen dataclass — fields `finding_index: int`, `is_real_bug: bool`, `reasoning: str`. Returned by `judge_findings`; `finding_index` ties back to the evaluation's finding list. Out-of-range / missing indices are dropped (anti-hallucination, same discipline as `evaluate_diff`). |
| **`review_span_context`** | Optional exported DD LLM Obs span carried on `LlmReviewResponse`. The span of the successful review call — the judge attaches `is_real_bug` evals to THIS span so the eval shows on the trace whose output produced the finding. `None` when the review degraded or ddtrace is absent (judge then skips). |
| **Reaction-poll loop** | Second quality-feedback loop (#245). A developer's 👍/👎 on a Grug inline comment is the HUMAN ground-truth that calibrates the LLM judge. GitHub does NOT webhook comment reactions, so a scheduled poller (#245b) reads them via the reactions REST API. `personas/code_reviewer/reactions.py` (mirrored) is the engine: `_classify_reactions` (👎→false_positive, precedence over 👍→confirmed), `poll_comment_reactions` (GH GET), `poll_and_annotate` (batch + dedup). Submits `llm_client.submit_reaction_annotation` → a `human_verdict` categorical DD eval on the review span (distinct label from the judge's `is_real_bug`). |
| **`CommentRecord`** | DDB row persisting a Grug inline-comment for later reaction polling. `PK=INST#<id>`, `SK=CRCOMMENT#<comment_id>`; carries repo, pr_number, `review_span_context`, finding_tags, and `last_verdict` (dedup baseline). `install_store.put_comment_record` / `list_comment_records` / `update_comment_record_reaction`. The poller only submits a `human_verdict` when the current reaction classification differs from `last_verdict` — a stale 👎 doesn't re-submit every cycle, but a 👎→👍 flip does. |

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
| **`force_disable_enforcement`** | Optional boolean field on `RepoConfig` DDB row, default `False`. When `True`, the self-healing loop skips re-creation of a deleted Grug-managed ruleset. Escape hatch for users who intentionally remove enforcement without disabling the TPM persona. |
| **`heal_enforcement()`** | Module-level function in `enforcement.py`. Called from dispatcher when a `repository_ruleset` deleted event fires for a Grug-managed ruleset. Clears the stale `enforcement_ruleset_id`, delegates to `ensure_enforcement()` for idempotent re-creation, and emits an `enforcement_healed` structured log with old + new ruleset IDs. Skipped when `force_disable_enforcement` is `True` or TPM persona is disabled. |
| **Self-healing** | Reconciliation loop: when a Grug-managed ruleset is externally deleted, the webhook re-creates it if the repo still wants enforcement. Triggered by `repository_ruleset` webhook event with `action=deleted`. The `force_disable_enforcement` flag on `RepoConfig` is the opt-out. |
| **Enforcement migration** | One-time backfill script (`scripts/migrate_enforcement.py`) that scans all installations for TPM-enabled repos and creates Grug-managed rulesets where none exist. Handles the grug repo's legacy branch protection → ruleset migration. Supports `--dry-run`. Idempotent. |
| **`grug.enforcement.state`** | DogStatsD gauge metric emitted by `emit_enforcement_metric()` in `observability.py` on every enforcement state change. Value: 1.0 (grug_managed), 0.5 (external), 0.0 (none). Tags: `repo`, `persona`, `enforcement_type`. Used by the enforcement gap monitor. |
| **Enforcement gap monitor** | DD monitor `grug-enforcement-gap` that alerts when any repo has `enforcement_type:none` for >1 hour. Routes to the DD monitoring Discord webhook. |

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
| `personas/code_reviewer/diff_parser.py` | `DiffHunk` dataclass + pure `parse_diff(unified_diff)` for the Elder persona. |
| `personas/code_reviewer/persona.py` | `Finding` + `CodeReviewEvaluation` dataclasses + pure `evaluate_diff(hunks, llm_response)`. |
| `github_reviews_client.py` | `ReviewResult` + `InlineComment` frozen dataclasses + `post_review(...)` PR Reviews API client. |

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

---

## Appendix — Slice plan tracking convention (post `/temper-improve`)

SDD slices are planned + executed against the temper specs above. Slice
plans live **in the repo** under
`specs/<NNNN-spec-slug>/plans/<YYYY-MM-DD>-<slice>.md`, not in `/tmp/`
or chat scrollback. Every shipped plan gets an `## Outcome` postscript
capturing the v1→v2 transition (what was proposed, what peer-review
flagged, what shipped). The historical record is the only way future-you
can ask "why does the adapter do X instead of Y?" and get an answer.

**Template:** `specs/SLICE_PLAN_TEMPLATE.md` — copy when starting a new
slice. Bakes the recurring patterns peer-review catches (IOA atomicity,
runtime attester allowlist-not-denylist, cross-user defense at storage
layer, single transactional mutation, clock injection, explicit
deferral checklist, mirror discipline, storage-side scope of mutation)
as required sections so the v1→v2 ceremonial round collapses to
substantive review.

**Workflow:**
1. Copy `specs/SLICE_PLAN_TEMPLATE.md` to
   `specs/<NNNN>/plans/<date>-<slice>.md`.
2. Fill all required sections (or explicit `N/A — <reason>`).
3. Optional but recommended for first slice on a new IOA action: submit
   to `/peer-review` chain. If BLOCK'd, rewrite to v2 in the same file
   with the v1 narrative + v2 deltas preserved as a transition record.
4. Implement against the plan; spec edits are spec-first (TOML → temper
   verify → grounding attester → code).
5. After commit, add `## Outcome` postscript with shipped SHA +
   reviewer-finding pre-emption matrix.

**When a recurring CRIT class becomes mechanically detectable** (e.g.
the over-broad-DELETE pattern from PR #151 could become an AST-walking
attester that flags `delete_item` calls outside test fixtures), the rule
moves OUT of the template INTO a CI gate or grounding attester. Until
then, template + per-slice peer-review are the human-layer enforcement.

**PII guard:** `services/{api,webhook}/tests/test_log_pii_guard.py`
scans for raw secret-bearing field names in log calls (OAuth plaintext
tokens, KMS plaintext keys, App private keys). Does NOT scan for
`github_user_id` / `install_id` — those are intentionally logged with
DD as the authorized sink + support flow needing the raw id. Migrating
identifiers to `observability.fingerprint()` is a deliberate future
call, not a CI blocker today. New log calls referencing the secret-set
field names get red-X'd at PR time unless wrapped with
`observability.fingerprint()` or logging the `_blob` / `_encrypted`
form.
