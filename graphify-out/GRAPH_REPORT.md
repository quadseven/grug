# Graph Report - grug  (2026-05-04)

## Corpus Check
- 93 files · ~293,966 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 755 nodes · 977 edges · 50 communities detected
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 68 edges (avg confidence: 0.78)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `6383fea5`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]

## God Nodes (most connected - your core abstractions)
1. `dispatch()` - 28 edges
2. `_verify_session()` - 13 edges
3. `_admin_user()` - 13 edges
4. `verify_signature()` - 11 edges
5. `_comment_payload()` - 11 edges
6. `UserPatchPayload` - 11 edges
7. `User` - 11 edges
8. `_seed_user()` - 10 edges
9. `upsert_oauth_user()` - 10 edges
10. `cmd_pr_gate()` - 9 edges

## Surprising Connections (you probably didn't know these)
- `test_me_returns_user_fields()` --calls--> `User`  [INFERRED]
  services/api/tests/test_oauth_routes.py → services/api/adapters/user_store.py
- `receive_github_webhook()` --calls--> `dispatch()`  [INFERRED]
  services/webhook/main.py → services/webhook/dispatcher.py
- `test_unknown_event_no_op()` --calls--> `dispatch()`  [INFERRED]
  services/webhook/tests/test_dispatcher.py → services/webhook/dispatcher.py
- `test_pull_request_review_placeholder()` --calls--> `dispatch()`  [INFERRED]
  services/webhook/tests/test_dispatcher.py → services/webhook/dispatcher.py
- `test_installation_repositories_no_op()` --calls--> `dispatch()`  [INFERRED]
  services/webhook/tests/test_dispatcher.py → services/webhook/dispatcher.py

## Communities (58 total, 7 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (46): callback(), _client_id(), _client_secret(), login(), _make_session(), _make_state(), me(), GitHub OAuth flow for grug-api (Slice 3 #24).  3 endpoints:   GET /api/v1/auth/g (+38 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (43): _full_payload(), _full_pr_payload(), Tests for webhook → persona dispatcher.  Covers routing decisions, payload-shape, Org installs: installed_by must be the human sender, not the org., Defense-in-depth: non-allowlisted installs no_op silently and     NEVER reach th, Org installs: installed_by must be the human sender, not the org., Org installs: installed_by must be the human sender, not the org., Org installs: installed_by must be the human sender, not the org. (+35 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (38): get_user(), _LazyTable, DDB user store with KMS envelope encryption for OAuth tokens.  Single-table layo, Create-or-update a user row from the OAuth callback flow.      Defaults:       r, Create-or-update a user row from the OAuth callback flow.      Defaults:       r, Create-or-update a user row from the OAuth callback flow.      Defaults:       r, Create-or-update a user row from the OAuth callback flow.      Defaults:       r, Return the user row (with decrypted OAuth tokens) or None.      KMS Decrypt happ (+30 more)

### Community 3 - "Community 3"
Cohesion: 0.09
Nodes (35): cmd_label_stale(), cmd_pr_gate(), cmd_pulse(), DoRCheck, fetch_pr(), find_existing_comment(), _gh(), has_section() (+27 more)

### Community 4 - "Community 4"
Cohesion: 0.1
Nodes (30): delete_installation(), get_installation(), get_repo_config(), _inst_pk(), is_install_allowlisted(), is_persona_enabled(), _LazyTable, list_user_installations() (+22 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (7): Tests for static DoR checks.  Critical regression: closes #20 — empty `- [ ]` pl, The bug from #20: `- [ ]` empty checkboxes must not count., `M&Ms` / `the M key` / `XL t-shirts` must NOT satisfy estimate., `M&Ms` / `the M key` / `XL t-shirts` must NOT satisfy estimate., `M&Ms` / `the M key` / `XL t-shirts` must NOT satisfy estimate., test_acceptance_empty_placeholders_reject_closes_20(), test_estimate_rejects_bare_letter_in_prose()

### Community 6 - "Community 6"
Cohesion: 0.11
Nodes (13): useAdminInstallations(), useAdminUsers(), usePatchUser(), api(), ApiError, useInstallations(), useInstallRepos(), useSetRepoConfig() (+5 more)

### Community 7 - "Community 7"
Cohesion: 0.1
Nodes (20): CheckRunResult, post_check_run(), GitHub Checks API client — post + update check-runs.  Wraps the two endpoints we, POST a check-run. Idempotent on (name, head_sha) per GitHub spec., evaluate_pull_request(), TPM persona evaluator — runs static DoR + posts check-run., Persona-level rollup of dor_checks results.      Distinct from CheckResult (per-, Persona-level rollup of dor_checks results.      Distinct from CheckResult (per- (+12 more)

### Community 8 - "Community 8"
Cohesion: 0.19
Nodes (22): User, UserPatchPayload, SPA → api PUT /repo/{id}/config payload.      `extra='forbid'` catches SPA typos, RepoConfigPayload, BaseModel, _admin_user(), Tests for admin allowlist + role mgmt (Slice 8 #29).  Uses moto DDB so logic run, Admin cannot demote themselves — prevents only-admin lock-out. (+14 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (19): Unit tests for the HMAC verifier.  Pure-function tests — no fixtures, no IO, no, _sign(), test_empty_secret_returns_false_even_with_matching_hmac(), test_malformed_header_shapes_return_false(), test_malformed_hex_returns_false(), test_missing_header_returns_false(), test_tampered_body_returns_false(), test_valid_signature_returns_true() (+11 more)

### Community 10 - "Community 10"
Cohesion: 0.09
Nodes (10): _ddb_table(), Tests for install_store + allowlist gate.  Uses moto to spin a local DDB so adap, Re-recording the same install must not error., Greptile P2 on PR #41 — re-record must NOT overwrite installed_at.     Without t, v1 default policy: unrecognized personas don't gate via this fn., Spin a moto DDB grug-main with the production schema., v1 default policy: unrecognized personas don't gate via this fn., test_is_persona_enabled_unknown_persona_defaults_true() (+2 more)

### Community 11 - "Community 11"
Cohesion: 0.1
Nodes (17): Tests for github_app_auth — JWT signing + install-token exchange.  Coverage gap:, force_refresh=True drops the cached entry + hits HTTP again., Authorization header must be 'Bearer <App JWT>' — not 'token <...>'., 401 propagates so with_install_token_retry can catch + refresh., Mint a fresh RSA keypair per test (PEM strings)., Each test sees a fresh cache (module-scope state would leak)., First call signs a fresh JWT with iat (60s back) + exp + iss claims., Second call within TTL returns cached value — does NOT re-sign. (+9 more)

### Community 12 - "Community 12"
Cohesion: 0.16
Nodes (16): _comment_payload(), _no_install_lookups(), Tests for #2 — `/grug recheck` slash command via issue_comment.  Covers: - Trigg, async-blocker-hunter F-01: transport error during perm lookup     must return sk, async-blocker-hunter F-01: transport error during PR re-fetch     must return sk, Skip allowlist + persona toggle DDB calls., test_no_trigger_text_no_ops(), test_non_allowlisted_install_no_ops() (+8 more)

### Community 13 - "Community 13"
Cohesion: 0.12
Nodes (18): _ensure_can_access(), list_install_repos(), list_installations(), User-facing installation + per-repo config endpoints (Slice 7 #28).  3 endpoints, List repos visible to this install (live from GitHub) merged with     DDB per-re, Upsert per-repo persona toggle. Caller must own the install., Upsert per-repo persona toggle. Caller must own the install., Upsert per-repo persona toggle. Caller must own the install. (+10 more)

### Community 14 - "Community 14"
Cohesion: 0.11
Nodes (12): Tests for github_oauth route handlers — login + me + logout.  Callback tests (to, Real router round-trip — catches @router.post path typos +     middleware regres, Real router round-trip — anonymous /me returns the documented shape., Real router round-trip — login redirects + sets cookie., The state in the URL = the state in the cookie. _verify_state passes., User row deleted from DDB AFTER the session was minted — return     not-authenti, test_login_state_cookie_value_round_trips_via_verify_state(), test_login_via_test_client() (+4 more)

### Community 15 - "Community 15"
Cohesion: 0.12
Nodes (16): Composition root for Grug SaaS Pulumi project (PRD githumps/grug#21).  Per `feed, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image (+8 more)

### Community 16 - "Community 16"
Cohesion: 0.18
Nodes (15): _ok_response(), Tests for github_checks_client.post_check_run.  Covers the request shape (URL, a, post_check_run does NOT swallow 401 — the with_install_token_retry     wrapper a, type-design-analyzer: GitHub 422s status=completed + conclusion=None.     Reject, Inverse: status=queued + conclusion=success is also a 422 from GH., Mimic httpx.Response.raise_for_status + .json()., test_check_run_result_rejects_completed_without_conclusion(), test_check_run_result_rejects_in_progress_with_conclusion() (+7 more)

### Community 17 - "Community 17"
Cohesion: 0.13
Nodes (8): Tests for personas.tpm.persona — _summary + evaluate_pull_request.  Covers the p, external_id binds (owner, repo, pr_number, head_sha) so GH     de-duplicates acr, TpmEvaluation is frozen so callers can't mutate the rollup., results is a tuple (immutable) — caller can iterate but not append., external_id binds (owner, repo, pr_number, head_sha) so GH     de-duplicates acr, test_evaluate_pull_request_external_id_format(), test_tpm_evaluation_is_frozen(), test_tpm_evaluation_results_is_tuple()

### Community 18 - "Community 18"
Cohesion: 0.2
Nodes (11): Tests for installations.py route handlers + auth helpers.  Covers: - _ensure_can, silent-failure-hunter P2 #6 regression: corrupt GSI1 row PK     must skip + log,, installed_by_user_id may be stored as int OR str depending on     DDB type-coerc, test_ensure_can_access_admin_always_passes(), test_ensure_can_access_install_owner_passes(), test_ensure_can_access_int_string_robust(), test_ensure_can_access_stranger_raises_403(), test_list_installations_empty_for_user_with_none() (+3 more)

### Community 19 - "Community 19"
Cohesion: 0.13
Nodes (5): _oauth_mod(), CSRF state-token tests for auth.github_oauth.  State token format: `<random>.<ts, Two consecutive _make_state calls produce different tokens., Stub _state_secret to deterministic value. Avoid SSM round-trip., test_make_state_random_per_call()

### Community 20 - "Community 20"
Cohesion: 0.2
Nodes (12): Pure-function tests for admin._user_to_admin_view + _inst_to_admin_view.  Critic, install_id always emitted as int — SPA dashboards expect numeric., Security invariant: encrypted token ciphertext must NEVER reach     the admin JS, test_inst_view_int_install_id_round_trip(), test_user_view_admin_role_passes_through(), test_user_view_allowlisted_truthy_coerces_bool(), test_user_view_default_role_user(), test_user_view_default_tier_free() (+4 more)

### Community 21 - "Community 21"
Cohesion: 0.32
Nodes (12): check_acceptance(), check_estimate(), check_issue_link(), check_scope_fence(), check_why(), CheckResult, Static DoR checks for PR bodies.  Ported from scripts/tpm.py with the bullet-cou, Return text under the first matching ## section, or None.      Matches case-inse (+4 more)

### Community 22 - "Community 22"
Cohesion: 0.14
Nodes (13): health(), livez(), FastAPI app for the grug-api Lambda.  Slice 2 (#23) scope: stand up the api Lamb, Liveness — process running. Restart on fail., Liveness — process running. Restart on fail., Liveness — process running. Restart on fail., Readiness — downstream deps reachable. v2 always ready (no deps)., Readiness — downstream deps reachable. v2 always ready (no deps). (+5 more)

### Community 23 - "Community 23"
Cohesion: 0.2
Nodes (13): _inst_to_admin_view(), list_all_installations(), list_users(), patch_user(), Admin-only user + allowlist management (Slice 8 #29).  Endpoints — all gated by, All INST# rows across all users., Flip allowlisted / role / tier on a user. Audit log to DD., Project a USER# DDB row into the admin response shape.      Excludes oauth_*_blo (+5 more)

### Community 24 - "Community 24"
Cohesion: 0.28
Nodes (12): _ok_resp(), Coverage for installations.update_repo_config — auth + GH membership.  The PUT /, silent-failure-hunter P1 #3: missing 'repositories' key →     502 not silent emp, Single-repo membership lookup must scan all pages — no early exit     on full pa, Sentry CRITICAL fix: repo not visible to install must 404 even if     repo exist, test_update_repo_config_admin_can_access_any(), test_update_repo_config_malformed_gh_502(), test_update_repo_config_paginates_until_match() (+4 more)

### Community 25 - "Community 25"
Cohesion: 0.23
Nodes (8): _format_record(), Tests for observability.JsonFormatter + configure_logging.  Covers: - Standard f, Idempotent re-configure: second call doesn't accumulate handlers., test_configure_logging_replaces_existing_handlers(), test_extra_kwargs_lifted_into_payload(), test_non_serialisable_extra_values_use_default_str(), test_reserved_logrecord_keys_excluded(), test_standard_fields_in_output()

### Community 26 - "Community 26"
Cohesion: 0.29
Nodes (11): _ok_resp(), Coverage for installations.list_install_repos GET endpoint.  PR #96/#107 covered, Truncate at 1000 repos (10 × 100) + log warning. Larger orgs     silently lose v, Critical: the per-repo config (tpm_enabled toggle) must be     merged into each, silent-failure-hunter P1 #3 regression: missing 'repositories' key     on the GE, test_list_install_repos_malformed_payload_502(), test_list_install_repos_merges_ddb_config_per_row(), test_list_install_repos_pagination_cap_at_10_pages() (+3 more)

### Community 27 - "Community 27"
Cohesion: 0.23
Nodes (6): Tests for auth.dependencies — get_current_user / require_authenticated / require, test_get_current_user_valid_returns_user(), test_require_admin_admin_user_passes_through(), test_require_admin_non_admin_raises_403(), test_require_authenticated_passes_user_through(), _user()

### Community 28 - "Community 28"
Cohesion: 0.17
Nodes (3): _kms_envelope(), Round-trip tests for crypto.kms_envelope synchronous wrappers.  Mocks boto3 KMS, Import kms_envelope with a fake CMK ARN + a stubbed KMS client.      The module

### Community 29 - "Community 29"
Cohesion: 0.35
Nodes (9): _app_id(), _app_private_key(), get_app_jwt(), get_install_token(), GitHub App auth — JWT signing + install token exchange (cached).  Per PRD #21 Q1, Return a fresh App JWT (cached up to ~9min)., Return a fresh installation access token (cached up to ~55min).      GitHub inst, Run `fn(token)` once. On httpx 401, invalidate cache + retry once.      Use this (+1 more)

### Community 30 - "Community 30"
Cohesion: 0.18
Nodes (5): Coverage for adapters.user_store.get_user.  upsert_oauth_user has dedicated test, Edge case: provider returned access but never refresh., upsert_oauth_user preserves admin/tier/allowlisted on re-auth., test_get_user_returns_admin_state_after_allowlist(), test_get_user_round_trip_with_only_access_token()

### Community 31 - "Community 31"
Cohesion: 0.24
Nodes (8): Health-endpoint tests for grug-api.  Per `feedback_health_endpoint_standard` mem, Memory feedback_health_endpoint_standard: /healthz is K8s-deprecated.     grug-w, Liveness must be cheap — no DDB, KMS, or HTTPX call. If it ever     starts depen, Memory feedback_health_endpoint_standard: /healthz is K8s-deprecated.     grug-a, test_livez_does_no_io(), test_livez_returns_200_with_status_ok(), test_no_healthz_endpoint(), test_readyz_returns_200_with_status_ready()

### Community 32 - "Community 32"
Cohesion: 0.25
Nodes (8): create(), _ensure_oidc_provider(), GitHub Actions OIDC trust + deploy role.  Per `feedback_prefer_ssm_over_1p`: no, # NOTE: tightening to specific resource ARNs is a, # NOTE: tightening to specific resource ARNs is a, # NOTE: tightening to specific resource ARNs is a, # NOTE: tightening to specific resource ARNs is a, Return ARN of the well-known GitHub OIDC provider.      The token.actions.github

### Community 33 - "Community 33"
Cohesion: 0.28
Nodes (5): TestClient-driven tests for receive_github_webhook.  PR #99 added the JSON-decod, silent-failure-hunter P1 #1: body that passes HMAC but fails     JSON decode mus, _sign(), test_signed_non_json_body_returns_400(), test_signed_valid_json_dispatches()

### Community 34 - "Community 34"
Cohesion: 0.39
Nodes (6): Regression test for #45 — H3 inside ## section must not truncate.  Mirrored from, Sanity: H3-only `### Why` should NOT count as `## Why`., test_acceptance_with_h3_subsections_passes(), test_acceptance_with_h4_subsections_passes(), test_h3_only_section_does_not_satisfy_h2_requirement(), test_why_with_h3_inside_passes()

### Community 35 - "Community 35"
Cohesion: 0.25
Nodes (4): Regression test for Sentry HIGH on PR #39.  GitHub OAuth re-auth can return an a, Edge case: first OAuth grant supplies no refresh (some providers)., Edge case: first OAuth grant supplies no refresh (some providers)., test_first_signin_with_no_refresh_works()

### Community 36 - "Community 36"
Cohesion: 0.29
Nodes (5): CheckRunResult, post_check_run(), GitHub Checks API client — post + update check-runs.  Wraps the two endpoints we, POST a check-run. Idempotent on (name, head_sha) per GitHub spec., POST a check-run. Idempotent on (name, head_sha) per GitHub spec.

### Community 37 - "Community 37"
Cohesion: 0.33
Nodes (5): create_proxied_cname(), Cloudflare DNS factory for grug.lol.  For Slice 1 we only need a CNAME for `webh, Convert `https://<host>/whatever` → `<host>` for CNAME content., Create a CNAME `<name>.<domain>` → host of target_url.      `proxied=True` (defa, _strip_scheme_and_path()

### Community 38 - "Community 38"
Cohesion: 0.47
Nodes (5): _common_tags(), create_all(), _MonitorBundle, Datadog monitor + synthetic factories for grug observability.  Per memory `refer, Build the v1 monitor set + synthetic. Returns the bundle so the     composition

### Community 39 - "Community 39"
Cohesion: 0.4
Nodes (5): create(), grant_use_to_role(), GrugTokensCmk, Customer-managed KMS key for grug user-token envelope encryption.  Annual rotati, Grant a Lambda role kms:GenerateDataKey + kms:Decrypt on this CMK.

### Community 40 - "Community 40"
Cohesion: 0.4
Nodes (4): create(), ECR repository factory with lifecycle policy.  Per PRD #21: untagged images expi, Create a private ECR repo with lifecycle pruning., Create a private ECR repo with lifecycle pruning.      `force_delete` is opt-in

### Community 41 - "Community 41"
Cohesion: 0.5
Nodes (3): configure_logging(), JsonFormatter, Structured JSON logging configuration.  DD Lambda extension layer (added in Slic

### Community 42 - "Community 42"
Cohesion: 0.67
Nodes (3): create(), LambdaService, Lambda + Function URL + log group factory.  Returns a `LambdaService` namespace-

## Knowledge Gaps
- **283 isolated node(s):** `Composition root for Grug SaaS Pulumi project (PRD githumps/grug#21).  Per `feed`, `# NOTE: CF Worker (grug-webhook-host-rewrite) is managed OUT OF BAND`, `# NOTE: DD extension is BAKED into the Lambda container image`, `Lambda + Function URL + log group factory.  Returns a `LambdaService` namespace-`, `ECR repository factory with lifecycle policy.  Per PRD #21: untagged images expi` (+278 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Community 8` to `Community 2`, `Community 14`, `Community 18`, `Community 24`, `Community 26`, `Community 27`?**
  _High betweenness centrality (0.064) - this node is a cross-community bridge._
- **Why does `get_user()` connect `Community 2` to `Community 8`, `Community 0`?**
  _High betweenness centrality (0.024) - this node is a cross-community bridge._
- **Why does `upsert_oauth_user()` connect `Community 2` to `Community 8`, `Community 0`?**
  _High betweenness centrality (0.022) - this node is a cross-community bridge._
- **Are the 19 inferred relationships involving `dispatch()` (e.g. with `receive_github_webhook()` and `test_unknown_event_no_op()`) actually correct?**
  _`dispatch()` has 19 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `_verify_session()` (e.g. with `get_current_user()` and `test_make_then_verify_session_round_trip()`) actually correct?**
  _`_verify_session()` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `verify_signature()` (e.g. with `receive_github_webhook()` and `test_valid_signature_returns_true()`) actually correct?**
  _`verify_signature()` has 9 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Composition root for Grug SaaS Pulumi project (PRD githumps/grug#21).  Per `feed`, `# NOTE: CF Worker (grug-webhook-host-rewrite) is managed OUT OF BAND`, `# NOTE: DD extension is BAKED into the Lambda container image` to the rest of the system?**
  _283 weakly-connected nodes found - possible documentation gaps or missing edges._