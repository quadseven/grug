# Graph Report - grug  (2026-05-27)

## Corpus Check
- 118 files · ~1,440,616 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1133 nodes · 1487 edges · 73 communities detected
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 99 edges (avg confidence: 0.79)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `743d04ce`
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
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]

## God Nodes (most connected - your core abstractions)
1. `dispatch()` - 38 edges
2. `_ok_response()` - 19 edges
3. `mock_transport_client()` - 14 edges
4. `_verify_session()` - 14 edges
5. `_admin_user()` - 14 edges
6. `get_user()` - 12 edges
7. `upsert_oauth_user()` - 12 edges
8. `verify_signature()` - 11 edges
9. `_comment_payload()` - 11 edges
10. `UserPatchPayload` - 11 edges

## Surprising Connections (you probably didn't know these)
- `receive_github_webhook()` --calls--> `dispatch()`  [INFERRED]
  services/webhook/main.py → services/webhook/dispatcher.py
- `test_unknown_event_no_op()` --calls--> `dispatch()`  [INFERRED]
  services/webhook/tests/test_dispatcher.py → services/webhook/dispatcher.py
- `test_pull_request_review_placeholder()` --calls--> `dispatch()`  [INFERRED]
  services/webhook/tests/test_dispatcher.py → services/webhook/dispatcher.py
- `test_installation_repositories_no_op()` --calls--> `dispatch()`  [INFERRED]
  services/webhook/tests/test_dispatcher.py → services/webhook/dispatcher.py
- `test_pull_request_unhandled_action_skips()` --calls--> `dispatch()`  [INFERRED]
  services/webhook/tests/test_dispatcher.py → services/webhook/dispatcher.py

## Communities (81 total, 8 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (63): _full_payload(), _full_pr_payload(), Tests for webhook → persona dispatcher.  Covers routing decisions, payload-shape, Peer-review CRITICAL (4x): publish_tpm_evaluation exceptions must     NOT propag, Org installs: installed_by must be the human sender, not the org., Defense-in-depth: non-allowlisted installs no_op silently and     NEVER reach th, Org installs: installed_by must be the human sender, not the org., Org installs: installed_by must be the human sender, not the org. (+55 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (54): get_current_user(), FastAPI dependencies for session-cookie auth.  Single source for "who is the cur, Resolve cookie → User. Returns None for anonymous or invalid.      Session-cooki, Resolve cookie → UserIdentity. Returns None for anonymous or invalid.      Sessi, Authenticate + return user with decrypted OAuth tokens.      KMS Decrypt happens, require_admin(), require_authenticated(), require_authenticated_with_tokens() (+46 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (45): delete_user_state(), _fetch_item(), get_user(), get_user_with_tokens(), _identity_from_item(), _LazyTable, DDB user store with KMS envelope encryption for OAuth tokens.  Single-table layo, Create-or-update a user row from the OAuth callback flow.      Defaults:       r (+37 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (40): delete_installation(), get_enforcement_id(), get_installation(), get_repo_config(), _inst_pk(), is_install_allowlisted(), is_persona_enabled(), _LazyTable (+32 more)

### Community 4 - "Community 4"
Cohesion: 0.1
Nodes (35): _inst_to_admin_view(), list_all_installations(), list_users(), patch_user(), Admin-only user + allowlist management (Slice 8 #29).  Endpoints — all gated by, All INST# rows across all users., Flip allowlisted / role / tier on a user. Audit log to DD., Project a USER# DDB row into the admin response shape.      Excludes oauth_*_blo (+27 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (35): cmd_label_stale(), cmd_pr_gate(), cmd_pulse(), DoRCheck, fetch_pr(), find_existing_comment(), _gh(), has_section() (+27 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (30): build_aad(), CredentialBlobCorrupt, decrypt_blob(), decrypt_user_dek(), encrypt_blob(), encrypt_for_user(), _encryption_context(), generate_user_dek() (+22 more)

### Community 7 - "Community 7"
Cohesion: 0.06
Nodes (22): Tests for personas.tpm.persona — _summary + evaluate_pull_request (pure) + publi, issue-link is advisory — missing it should NOT block the PR., scope-fence is blocking — missing it MUST block the PR., external_id binds (owner, repo, pr_number, head_sha) so GH     de-duplicates acr, When both advisory (issue-link) and blocking (scope-fence) fail,     the blockin, TpmEvaluation is frozen so callers can't mutate the rollup., results is a tuple (immutable) — caller can iterate but not append., Advisory checks that fail should render ⚠️ not ❌ in the summary. (+14 more)

### Community 8 - "Community 8"
Cohesion: 0.1
Nodes (30): _ok_response(), Tests for github_rulesets_client — create/list/delete + enforcement detection., Grug-prefixed ruleset with matching check → grug_managed., Non-Grug ruleset enforcing the check → external., No rulesets match, but legacy branch protection enforces the check → external., No rulesets, no legacy protection → none., No rulesets, legacy endpoint 404s (no branch protection at all) → none., If both a Grug-managed AND external ruleset match, grug_managed wins. (+22 more)

### Community 9 - "Community 9"
Cohesion: 0.08
Nodes (30): _ensure_can_access(), fix_enforcement(), get_enforcement(), list_install_repos(), list_installations(), User-facing installation + per-repo config endpoints (Slice 7 #28).  3 endpoints, List repos visible to this install (live from GitHub) merged with     DDB per-re, Upsert per-repo persona toggle. Caller must own the install. (+22 more)

### Community 10 - "Community 10"
Cohesion: 0.09
Nodes (19): HTMLParser, _AnchorCollector, _collect(), _has_class(), main(), _normalize_button_text(), Collect every `<a>` element with (href, class, inner_text)., True iff every word in `needle` is present in `cls` (order-free). (+11 more)

### Community 11 - "Community 11"
Cohesion: 0.08
Nodes (26): CheckRunResult, post_check_run(), GitHub Checks API client — post + update check-runs.  Wraps the two endpoints we, POST a check-run. Idempotent on (name, head_sha) per GitHub spec., POST a check-run. Idempotent on (name, head_sha) per GitHub spec., evaluate_pull_request(), publish_tpm_evaluation(), TPM persona — pure DoR rollup + GitHub Checks publisher.  Per spec 0002 (`evalua (+18 more)

### Community 12 - "Community 12"
Cohesion: 0.09
Nodes (16): useAdminInstallations(), useAdminUsers(), usePatchUser(), api(), ApiError, useEnforcement(), useFixEnforcement(), useInstallations() (+8 more)

### Community 13 - "Community 13"
Cohesion: 0.07
Nodes (25): Tests for enforcement lifecycle — ensure/remove/heal enforcement.  Covers the en, No stored ID, no rulesets at all → nothing to do., Full lifecycle: enable creates, disable deletes., Deleted Grug ruleset → clear old ID → ensure creates a new one., heal_enforcement returns the EnforcementState from ensure., If someone added an external ruleset before we heal, skip creation., No enforcement → create ruleset + store ID in DDB., Already grug_managed → no-op. (+17 more)

### Community 14 - "Community 14"
Cohesion: 0.08
Nodes (7): Tests for static DoR checks.  Critical regression: closes #20 — empty `- [ ]` pl, The bug from #20: `- [ ]` empty checkboxes must not count., `M&Ms` / `the M key` / `XL t-shirts` must NOT satisfy estimate., `M&Ms` / `the M key` / `XL t-shirts` must NOT satisfy estimate., `M&Ms` / `the M key` / `XL t-shirts` must NOT satisfy estimate., test_acceptance_empty_placeholders_reject_closes_20(), test_estimate_rejects_bare_letter_in_prose()

### Community 15 - "Community 15"
Cohesion: 0.09
Nodes (16): Tests for github_oauth route handlers — login + me + logout.  Callback tests (to, Real router round-trip — catches @router.post path typos +     middleware regres, Real router round-trip — catches @router.post path typos +     middleware regres, Real router round-trip — anonymous /me returns the documented shape., Real router round-trip — anonymous /me returns the documented shape., Real router round-trip — login redirects + sets cookie., Real router round-trip — login redirects + sets cookie., The state in the URL = the state in the cookie. _verify_state passes. (+8 more)

### Community 16 - "Community 16"
Cohesion: 0.14
Nodes (19): Unit tests for the HMAC verifier.  Pure-function tests — no fixtures, no IO, no, _sign(), test_empty_secret_returns_false_even_with_matching_hmac(), test_malformed_header_shapes_return_false(), test_malformed_hex_returns_false(), test_missing_header_returns_false(), test_tampered_body_returns_false(), test_valid_signature_returns_true() (+11 more)

### Community 17 - "Community 17"
Cohesion: 0.09
Nodes (10): _ddb_table(), Tests for install_store + allowlist gate.  Uses moto to spin a local DDB so adap, Re-recording the same install must not error., Greptile P2 on PR #41 — re-record must NOT overwrite installed_at.     Without t, v1 default policy: unrecognized personas don't gate via this fn., Spin a moto DDB grug-main with the production schema., v1 default policy: unrecognized personas don't gate via this fn., test_is_persona_enabled_unknown_persona_defaults_true() (+2 more)

### Community 18 - "Community 18"
Cohesion: 0.13
Nodes (19): _comment_payload(), _no_install_lookups(), Tests for #2 — `/grug recheck` slash command via issue_comment.  Covers: - Trigg, async-blocker-hunter F-01: transport error during perm lookup     must return sk, async-blocker-hunter F-01: transport error during perm lookup     must return sk, async-blocker-hunter F-01: transport error during PR re-fetch     must return sk, async-blocker-hunter F-01: transport error during PR re-fetch     must return sk, Skip allowlist + persona toggle DDB calls. (+11 more)

### Community 19 - "Community 19"
Cohesion: 0.1
Nodes (17): Tests for github_app_auth — JWT signing + install-token exchange.  Coverage gap:, force_refresh=True drops the cached entry + hits HTTP again., Authorization header must be 'Bearer <App JWT>' — not 'token <...>'., 401 propagates so with_install_token_retry can catch + refresh., Mint a fresh RSA keypair per test (PEM strings)., Each test sees a fresh cache (module-scope state would leak)., First call signs a fresh JWT with iat (60s back) + exp + iss claims., Second call within TTL returns cached value — does NOT re-sign. (+9 more)

### Community 20 - "Community 20"
Cohesion: 0.11
Nodes (18): Composition root for Grug SaaS Pulumi project (PRD githumps/grug#21).  Per `feed, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image, # NOTE: DD extension is BAKED into the Lambda container image (+10 more)

### Community 21 - "Community 21"
Cohesion: 0.12
Nodes (9): Coverage for adapters.user_store.get_user + get_user_with_tokens.  Per issue #10, upsert_oauth_user preserves admin/tier/allowlisted on re-auth., Edge case: provider returned access but never refresh., Edge case: provider returned access but never refresh., Identity-only path must not expose token attrs (#103 invariant)., upsert_oauth_user preserves admin/tier/allowlisted on re-auth., test_get_user_does_not_carry_token_fields(), test_get_user_returns_admin_state_after_allowlist() (+1 more)

### Community 22 - "Community 22"
Cohesion: 0.13
Nodes (15): mock_transport_client(), Factory fixture: build a real httpx.Client backed by MockTransport.      Usage:, Transport-level ConnectError must propagate (not get caught by an     httpx.HTTP, test_post_check_run_connect_error_propagates(), 500 from rulesets API must propagate., Transport-level ConnectError on rulesets API must propagate., 401 from rulesets list_rulesets must propagate so     with_install_token_retry c, 500 from the legacy branch protection endpoint must re-raise. (+7 more)

### Community 23 - "Community 23"
Cohesion: 0.17
Nodes (8): _format_record(), Tests for observability.JsonFormatter + configure_logging.  Covers: - Standard f, Idempotent re-configure: second call doesn't accumulate handlers., test_configure_logging_replaces_existing_handlers(), test_extra_kwargs_lifted_into_payload(), test_non_serialisable_extra_values_use_default_str(), test_reserved_logrecord_keys_excluded(), test_standard_fields_in_output()

### Community 24 - "Community 24"
Cohesion: 0.29
Nodes (13): check_acceptance(), check_estimate(), check_issue_link(), check_scope_fence(), check_why(), CheckResult, Static DoR checks for PR bodies.  Ported from scripts/tpm.py with the bullet-cou, Return text under the first matching ## section, or None.      Matches case-inse (+5 more)

### Community 25 - "Community 25"
Cohesion: 0.2
Nodes (11): Tests for installations.py route handlers + auth helpers.  Covers: - _ensure_can, silent-failure-hunter P2 #6 regression: corrupt GSI1 row PK     must skip + log,, installed_by_user_id may be stored as int OR str depending on     DDB type-coerc, test_ensure_can_access_admin_always_passes(), test_ensure_can_access_install_owner_passes(), test_ensure_can_access_int_string_robust(), test_ensure_can_access_stranger_raises_403(), test_list_installations_empty_for_user_with_none() (+3 more)

### Community 26 - "Community 26"
Cohesion: 0.13
Nodes (5): _oauth_mod(), CSRF state-token tests for auth.github_oauth.  State token format: `<random>.<ts, Two consecutive _make_state calls produce different tokens., Stub _state_secret to deterministic value. Avoid SSM round-trip., test_make_state_random_per_call()

### Community 27 - "Community 27"
Cohesion: 0.2
Nodes (12): Pure-function tests for admin._user_to_admin_view + _inst_to_admin_view.  Critic, install_id always emitted as int — SPA dashboards expect numeric., Security invariant: encrypted token ciphertext must NEVER reach     the admin JS, test_inst_view_int_install_id_round_trip(), test_user_view_admin_role_passes_through(), test_user_view_allowlisted_truthy_coerces_bool(), test_user_view_default_role_user(), test_user_view_default_tier_free() (+4 more)

### Community 28 - "Community 28"
Cohesion: 0.22
Nodes (13): _auth_headers(), _check_name_in_legacy(), _check_name_in_ruleset(), create_ruleset(), delete_ruleset(), detect_enforcement(), list_rulesets(), Return True if any required_status_checks rule in the ruleset matches check_name (+5 more)

### Community 29 - "Community 29"
Cohesion: 0.22
Nodes (13): _auth_headers(), _check_name_in_legacy(), _check_name_in_ruleset(), create_ruleset(), delete_ruleset(), detect_enforcement(), list_rulesets(), Return True if any required_status_checks rule in the ruleset matches check_name (+5 more)

### Community 30 - "Community 30"
Cohesion: 0.14
Nodes (13): health(), livez(), FastAPI app for the grug-api Lambda.  Slice 2 (#23) scope: stand up the api Lamb, Liveness — process running. Restart on fail., Liveness — process running. Restart on fail., Liveness — process running. Restart on fail., Readiness — downstream deps reachable. v2 always ready (no deps)., Readiness — downstream deps reachable. v2 always ready (no deps). (+5 more)

### Community 31 - "Community 31"
Cohesion: 0.32
Nodes (12): _attest_main_callsite(), _attest_rum_application(), _attest_ssm_exports(), _is_output_secret(), _kwarg_value(), main(), Both SSM params present + both wrapped in pulumi.Output.secret()., __main__.py must call dd_rum.create(name='grug-web', ...). (+4 more)

### Community 32 - "Community 32"
Cohesion: 0.18
Nodes (12): create(), DeployRole, _ensure_oidc_provider(), GitHub Actions OIDC trust + deploy role.  Per `feedback_prefer_ssm_over_1p`: no, # NOTE: tightening to specific resource ARNs is a, # NOTE: tightening to specific resource ARNs is a, # NOTE: tightening to specific resource ARNs is a, # NOTE: tightening to specific resource ARNs is a (+4 more)

### Community 33 - "Community 33"
Cohesion: 0.24
Nodes (12): _ok_response(), Tests for github_checks_client.post_check_run.  Covers the request shape (URL, a, post_check_run does NOT swallow 401 — the with_install_token_retry     wrapper a, Real-transport-backed (issue #105) — 500 raised via raise_for_status., Mimic httpx.Response.raise_for_status + .json()., test_post_check_run_401_propagates_for_retry_helper(), test_post_check_run_500_propagates_unwrapped(), test_post_check_run_body_omits_conclusion_when_none() (+4 more)

### Community 34 - "Community 34"
Cohesion: 0.28
Nodes (12): _ok_resp(), Coverage for installations.update_repo_config — auth + GH membership.  The PUT /, silent-failure-hunter P1 #3: missing 'repositories' key →     502 not silent emp, Single-repo membership lookup must scan all pages — no early exit     on full pa, Sentry CRITICAL fix: repo not visible to install must 404 even if     repo exist, test_update_repo_config_admin_can_access_any(), test_update_repo_config_malformed_gh_502(), test_update_repo_config_paginates_until_match() (+4 more)

### Community 35 - "Community 35"
Cohesion: 0.26
Nodes (10): _candidate_files(), PII guard — scan source for log calls that emit raw secret material.  Scoped to, Sanity: the helper this guard expects exists + is callable., PII guard — fails if any `.py` outside the whitelist references a     raw-secret, Sanity: the helper this guard expects exists + is callable., Return one finding per raw-secret-in-log emission, or []., PII guard — same shape as the api-side test., _scan_file() (+2 more)

### Community 36 - "Community 36"
Cohesion: 0.17
Nodes (3): _kms_envelope(), Round-trip tests for crypto.kms_envelope synchronous wrappers.  Mocks boto3 KMS, Import kms_envelope with a fake CMK ARN + a stubbed KMS client.      The module

### Community 37 - "Community 37"
Cohesion: 0.24
Nodes (8): Health-endpoint tests for grug-api.  Per `feedback_health_endpoint_standard` mem, Memory feedback_health_endpoint_standard: /healthz is K8s-deprecated.     grug-w, Liveness must be cheap — no DDB, KMS, or HTTPX call. If it ever     starts depen, Memory feedback_health_endpoint_standard: /healthz is K8s-deprecated.     grug-a, test_livez_does_no_io(), test_livez_returns_200_with_status_ok(), test_no_healthz_endpoint(), test_readyz_returns_200_with_status_ready()

### Community 38 - "Community 38"
Cohesion: 0.35
Nodes (9): _app_id(), _app_private_key(), get_app_jwt(), get_install_token(), GitHub App auth — JWT signing + install token exchange (cached).  Per PRD #21 Q1, Return a fresh App JWT (cached up to ~9min)., Return a fresh installation access token (cached up to ~55min).      GitHub inst, Run `fn(token)` once. On httpx 401, invalidate cache + retry once.      Use this (+1 more)

### Community 39 - "Community 39"
Cohesion: 0.25
Nodes (7): configure_logging(), emit_enforcement_metric(), fingerprint(), JsonFormatter, Structured JSON logging configuration.  DD Lambda extension layer (added in Slic, Return a non-reversible per-process correlation id for `value`.      Use to log, Emit grug.enforcement.state gauge via DD Lambda Extension DogStatsD.      Tags:

### Community 40 - "Community 40"
Cohesion: 0.28
Nodes (5): TestClient-driven tests for receive_github_webhook.  PR #99 added the JSON-decod, silent-failure-hunter P1 #1: body that passes HMAC but fails     JSON decode mus, _sign(), test_signed_non_json_body_returns_400(), test_signed_valid_json_dispatches()

### Community 41 - "Community 41"
Cohesion: 0.22
Nodes (5): Regression test for Sentry HIGH on PR #39.  GitHub OAuth re-auth can return an a, Edge case: first OAuth grant supplies no refresh (some providers)., Edge case: first OAuth grant supplies no refresh (some providers)., Edge case: first OAuth grant supplies no refresh (some providers)., test_first_signin_with_no_refresh_works()

### Community 42 - "Community 42"
Cohesion: 0.39
Nodes (7): _calls_named(), _find_function(), _has_except_credential_blob_corrupt(), main(), Lines where func calls a bare-name function `target(...)`., Verify at least one ExceptHandler for CredentialBlobCorrupt ends in `return None, _returns_none_in_corrupt_handler()

### Community 43 - "Community 43"
Cohesion: 0.25
Nodes (6): CheckRunResult, post_check_run(), GitHub Checks API client — post + update check-runs.  Wraps the two endpoints we, POST a check-run. Idempotent on (name, head_sha) per GitHub spec., POST a check-run. Idempotent on (name, head_sha) per GitHub spec., POST a check-run. Idempotent on (name, head_sha) per GitHub spec.

### Community 44 - "Community 44"
Cohesion: 0.29
Nodes (6): configure_logging(), emit_enforcement_metric(), fingerprint(), JsonFormatter, Return a non-reversible per-process correlation id for `value`.      Use to log, Emit grug.enforcement.state gauge via DD Lambda Extension DogStatsD.      Tags:

### Community 45 - "Community 45"
Cohesion: 0.39
Nodes (6): Regression test for #45 — H3 inside ## section must not truncate.  Mirrored from, Sanity: H3-only `### Why` should NOT count as `## Why`., test_acceptance_with_h3_subsections_passes(), test_acceptance_with_h4_subsections_passes(), test_h3_only_section_does_not_satisfy_h2_requirement(), test_why_with_h3_inside_passes()

### Community 46 - "Community 46"
Cohesion: 0.43
Nodes (6): _check_lazy_table_getattr(), _has_module_lock(), _has_threading_import(), main(), Look for `<name> = threading.Lock()` at module scope., Find class _LazyTable, find __getattr__, verify it uses `with <lock>:`     AND h

### Community 47 - "Community 47"
Cohesion: 0.43
Nodes (6): _has_function(), _is_persona_enabled_builds_key_correctly(), main(), _module_default_persona_config(), Find `_DEFAULT_PERSONA_CONFIG = {"tpm_enabled": True, ...}` at module scope., Search the function body for an f-string / format / concat that builds     `<per

### Community 48 - "Community 48"
Cohesion: 0.43
Nodes (6): _calls_named(), _has_int_cast_of(), main(), _module_imports(), All bare-name imports across all from/import statements., Verify some `int(<name_containing_substring>)` cast exists — the     GitHub-issu

### Community 49 - "Community 49"
Cohesion: 0.38
Nodes (6): _common_tags(), create_all(), _MonitorBundle, Datadog monitor + synthetic factories for grug observability.  Per memory `refer, Build the v1 monitor set + synthetic. Returns the bundle so the     composition, Build the v1 monitor set + synthetic. Returns the bundle so the     composition

### Community 50 - "Community 50"
Cohesion: 0.33
Nodes (6): ensure_enforcement(), heal_enforcement(), Delete the Grug-managed ruleset if one exists.      Reads the stored ruleset_id, Create a Grug-managed ruleset if no enforcement exists. Idempotent.      Returns, Re-create a Grug-managed ruleset after external deletion.      Clears the stale, remove_enforcement()

### Community 51 - "Community 51"
Cohesion: 0.33
Nodes (6): ensure_enforcement(), heal_enforcement(), Delete the Grug-managed ruleset if one exists.      Reads the stored ruleset_id, Create a Grug-managed ruleset if no enforcement exists. Idempotent.      Returns, Re-create a Grug-managed ruleset after external deletion.      Clears the stale, remove_enforcement()

### Community 52 - "Community 52"
Cohesion: 0.53
Nodes (5): _behavioral_check(), _find_class(), _has_post_init_raising_value_error(), main(), Import the module fresh and try constructing illegal CheckRunResult     instance

### Community 53 - "Community 53"
Cohesion: 0.4
Nodes (5): create(), Datadog RUM Application + SSM credential export for grug.lol.  Per spec 0013 (Ru, Resources created by `dd_rum.create()` — kept together so the     composition ro, Provision the DD RUM Application + export its credentials to SSM.      Args:, RumBundle

### Community 54 - "Community 54"
Cohesion: 0.33
Nodes (5): create_proxied_cname(), Cloudflare DNS factory for grug.lol.  For Slice 1 we only need a CNAME for `webh, Convert `https://<host>/whatever` → `<host>` for CNAME content., Create a CNAME `<name>.<domain>` → host of target_url.      `proxied=True` (defa, _strip_scheme_and_path()

### Community 55 - "Community 55"
Cohesion: 0.4
Nodes (5): create(), grant_use_to_role(), GrugTokensCmk, Customer-managed KMS key for grug user-token envelope encryption.  Annual rotati, Grant a Lambda role kms:GenerateDataKey + kms:Decrypt on this CMK.

### Community 56 - "Community 56"
Cohesion: 0.6
Nodes (4): _call_target_name(), main(), Return the leftmost-name of the call target, e.g. `httpx.post(...)` → `httpx`,, _violations_in_function()

### Community 57 - "Community 57"
Cohesion: 0.6
Nodes (4): _check_result_is_frozen(), main(), _module_check_functions(), Verify `@dataclass(frozen=True)` on the CheckResult class. Peer-review     HIGH

### Community 58 - "Community 58"
Cohesion: 0.4
Nodes (4): create(), ECR repository factory with lifecycle policy.  Per PRD #21: untagged images expi, Create a private ECR repo with lifecycle pruning., Create a private ECR repo with lifecycle pruning.      `force_delete` is opt-in

### Community 59 - "Community 59"
Cohesion: 0.67
Nodes (3): _body_sha512(), main(), SHA-512 over file bytes with the first line stripped if it matches     the MIRRO

### Community 60 - "Community 60"
Cohesion: 0.83
Nodes (3): _attest_spa_chain(), _attest_static_snippets(), main()

### Community 61 - "Community 61"
Cohesion: 0.67
Nodes (3): create(), LambdaService, Lambda + Function URL + log group factory.  Returns a `LambdaService` namespace-

### Community 62 - "Community 62"
Cohesion: 0.5
Nodes (3): _build_handler(), pytest config for services/webhook/ tests.  Adds the parent directory to sys.pat, Build a MockTransport handler that returns / raises in sequence.

### Community 66 - "Community 66"
Cohesion: 0.67
Nodes (3): Inverse: status=queued + conclusion=success is also a 422 from GH., Inverse: status=queued + conclusion=success is also a 422 from GH., test_check_run_result_rejects_in_progress_with_conclusion()

### Community 67 - "Community 67"
Cohesion: 0.67
Nodes (3): type-design-analyzer: GitHub 422s status=completed + conclusion=None.     Reject, type-design-analyzer: GitHub 422s status=completed + conclusion=None.     Reject, test_check_run_result_rejects_completed_without_conclusion()

## Knowledge Gaps
- **441 isolated node(s):** `Look for `<name> = threading.Lock()` at module scope.`, `Find class _LazyTable, find __getattr__, verify it uses `with <lock>:`     AND h`, `Find `_DEFAULT_PERSONA_CONFIG = {"tpm_enabled": True, ...}` at module scope.`, `Search the function body for an f-string / format / concat that builds     `<per`, `Collect every `<a>` element with (href, class, inner_text).` (+436 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Community 2` to `Community 9`, `Community 34`, `Community 4`, `Community 25`?**
  _High betweenness centrality (0.032) - this node is a cross-community bridge._
- **Why does `upsert_oauth_user()` connect `Community 2` to `Community 1`, `Community 6`?**
  _High betweenness centrality (0.015) - this node is a cross-community bridge._
- **Why does `get_user()` connect `Community 2` to `Community 1`?**
  _High betweenness centrality (0.013) - this node is a cross-community bridge._
- **Are the 27 inferred relationships involving `dispatch()` (e.g. with `receive_github_webhook()` and `test_unknown_event_no_op()`) actually correct?**
  _`dispatch()` has 27 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `mock_transport_client()` (e.g. with `test_perm_lookup_transport_error_returns_skip()` and `test_pr_fetch_transport_error_returns_skip()`) actually correct?**
  _`mock_transport_client()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 8 inferred relationships involving `_verify_session()` (e.g. with `get_current_user()` and `require_authenticated_with_tokens()`) actually correct?**
  _`_verify_session()` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `_admin_user()` (e.g. with `UserIdentity` and `User`) actually correct?**
  _`_admin_user()` has 2 INFERRED edges - model-reasoned connections that need verification._