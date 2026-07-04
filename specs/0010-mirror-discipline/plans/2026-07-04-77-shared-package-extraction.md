# Slice plan - #77 v1.5: extract services/_shared/ package (mirror retirement)

## 0. Slice header

- **Spec:** `0010-mirror-discipline`
- **IOA action(s) grounded:** `Extract` (Synced -> Extracted, terminal per
  `ExtractedIsTerminal`)
- **Bools grounded (atomic set):** `Extract` carries an EMPTY effect set in
  the IOA (the transition itself is the fact). The pre-existing
  `rule_of_three_triggers_extraction_to_services_underscore_shared_*` bool
  stays grounded - its meaning flips from "policy exists" to "policy fired":
  the 3rd async persona (Smasher, ADR-0013) is the trigger evidence, recorded
  in `async_dispatch.py`'s own `enqueue_smasher_review` docstring and
  ADR-0013's deferral note.
- **Bools left deferred (with reason):** none - the four mirror-era contract
  bools (header, body-identity, enforcement x2) describe the Copied/Synced
  states, which are now historical. Their attesters are re-pointed at the
  Extracted-state necessary conditions (section 2) rather than deleted, so
  spec 0010 keeps a live grounding.
- **Estimated size:** 180 min target

## 1. IOA atomicity verification

```
Extract effect set (from spec 0010/mirror-discipline.ioa.toml):
  (empty - state transition only, params = [consumer_count])
```

consumer_count = 3: Elder + Guard + Smasher are the three async personas
whose duplicated enqueue+run machinery fired the rule-of-three (ADR-0013
explicitly deferred the generalization to #77). No bool cherry-picking is
possible on an empty effect set. `MustSyncBeforeExtract` holds: the repo has
been in Synced (drift-lint green on main) continuously; the last drift-lint
run on main before this slice is the Synced witness.

## 2. Runtime attester design (per bool)

Both attesters keep their filenames (wired into check.temper-specs.yml) and
are rewritten to prove Extracted-state NECESSARY conditions:

- `infra/scripts/attest_mirror_policy_consistency.py` now asserts:
  1. `services/_shared/` exists and contains the shared import roots
     (`adapters/`, `personas/`, `ports/`, `github_app_auth/`,
     `observability.py`, `secrets_loader.py`, ...).
  2. **No shadowing:** no relative path under `services/api/` or
     `services/webhook/` duplicates a path in `services/_shared/`
     (a stray copy would silently shadow the shared module - the
     post-extraction drift class).
  3. Drift-lint is retired: `scripts/check-mirrored-files.sh` and
     `.github/workflows/check.drift-lint.yml` are GONE.
  4. No line-1 `# MIRRORED` headers remain under `services/`.
- `infra/scripts/attest_dor_checks_mirror.py` now asserts
  `personas/tpm/dor_checks.py` exists EXACTLY once, at
  `services/_shared/personas/tpm/dor_checks.py` (byte-identity is vacuously
  true with one copy; single-copy is the stronger fact).

A pytest twin of the no-shadowing assertion lands in
`services/webhook/tests/` so the guard runs on every services/** PR via
check.python, not only on temper-workflow path triggers.

## 3. Cross-user / cross-tenant defense

N/A - refactor only; no state mutation, no new read/write paths.

## 4. Single transactional mutation

N/A - no store writes.

## 5. Clock injection

N/A - no time-dependent behavior.

## 6. Explicit deferral checklist

- `TokenedGitHubClient` transport dedup (#142 candidate 7) - DEFERRED, its
  own slice; this slice does not touch the GitHub client wrappers' bodies.
- Collapsing the per-persona `webhook_dispatch.py` modules - DEFERRED
  deliberately: they are the registry's per-persona seam (ADR-0010), and
  their flag-check bodies genuinely differ.
- Consolidating the two Dockerfiles into one ARG-driven file - DEFERRED;
  kept two files with path-prefixed COPYs to minimize deploy-surface diff.
- `conftest.py` sys.path bootstrap remains duplicated per service (pytest
  conftest discovery is directory-bound); fixture LOGIC moves to
  `services/_shared/grug_shared_conftest.py`, each service keeps a thin shim.
- DD monitor sweep for logger-name change `grug.{api,webhook}.persona.tpm`
  -> `{DD_SERVICE}.persona.tpm`: grep found zero references in infra/ or
  tests; no monitor keys on logger.name for tpm (verified pre-slice).

## 7. Mirror discipline (services/api/ vs services/webhook/)

This slice RETIRES the discipline - terminal `Extracted` state:

- 42 header-mirrored + 10 byte-identical pairs move to `services/_shared/`
  (single copy, headers stripped). `conftest.py` becomes a per-service shim.
- `personas/tpm/persona.py` (diverged by ONE logger-namespace line, as
  ADR-0001 anticipated) is unified via the established
  `f"{os.getenv('DD_SERVICE', 'grug')}.persona.tpm"` convention.
- Package-integrity rule: a Python package is wholly owned by ONE root.
  Service-specific modules inside shared-owned packages move WITH the
  package, marked `# API-ONLY` / `# WEBHOOK-ONLY` line-1 headers and only
  lazy-imported (existing convention): `adapters/{user_store,pg_user_store}.py`
  (api-only), `personas/smasher/trial_*.py` (webhook-only).
- Diverged-by-design top-level modules stay per-service: `main.py`,
  `rerun.py`, plus each service's private packages (`auth/`, `crypto/`,
  `sast_benchmark/`, `spark_cave/`, ...).
- Import paths are PRESERVED (top-level names: `adapters.*`, `personas.*`,
  `observability`, ...) via PYTHONPATH (`/app/_shared` in images, conftest
  insert in tests) - the entire existing patch-target surface survives and
  both suites must pass UNCHANGED (that is the no-behavior-change proof).

## 8. Storage-side scope of mutation

N/A - no store mutation.

## 9. Sign-off

Ship-slice pipeline: spec (this file + ADR-0014) -> tests-first for the new
guards -> mechanical moves -> full suites green unchanged -> sequential
8-stage audit -> peer review -> merge. Live verify post-deploy: both pods
healthy + a real webhook delivery dispatching (DD logs).

## §10 - Sizing breakdown

- Move manifest + header strip script: 30 min
- Build plumbing (Dockerfiles, .dockerignore re-root, deploy.k8s.yml): 30 min
- Async enqueue+run generalization in async_dispatch.py: 45 min
- Attester rewrites + new guard tests: 30 min
- Docs (ADR-0014, ADR-0001 supersede, CONTEXT.md, DESIGN.md, RUNBOOK.md,
  spec hints): 30 min
- Suite runs + local docker build smoke: 15 min

## §11 - Recurring CRITs to pre-empt

- **Patch-target preservation:** import paths unchanged by design; zero
  test-suite edits expected outside the two path-scanning tests below.
- **Monitored log names byte-identical:** the generic async runner emits the
  exact `{elder,guard,smasher}_{job,enqueue}_*` lines; Elder keeps its
  RAW-GUID `claim_delivery` key (legacy, load-bearing) while Guard/Smasher
  keep `{delivery_id}:guard` / `{delivery_id}:smasher`; Elder's
  `claim_review` persona stays `code_reviewer` while its rerun persona stays
  `elder`; thread names stay length-19-bounded (`elder-`/`guard-` [:13],
  `smasher-` [:11]).
- **PII-guard scan shrinkage:** `test_log_pii_guard.py` (both suites) scans
  SERVICE_DIR only - moved files would silently ESCAPE the scan; both tests
  gain `services/_shared/` in their scan roots.
- **Shadow-guard:** new test asserts no service-tree path duplicates a
  _shared path (the new drift class).
- **iac.deploy.yml is pulumi-only post-#354** - no image-build edits there;
  the only build surface is deploy.k8s.yml step "Build + push images".
