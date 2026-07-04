# ADR-0014 - Extract services/_shared/; retire the mirror discipline

## Status

Accepted (2026-07-04). Supersedes the deferral in
[ADR-0001](0001-mirror-with-rule-of-three-deferral.md) (whose trigger this
ADR executes), closes #77.

## Context

ADR-0001 deliberately mirrored shared modules across `services/api/` and
`services/webhook/` until a rule-of-three trigger fired, with
`scripts/check-mirrored-files.sh` + the drift-lint workflow as the honesty
gate. Two things have since happened:

1. The mirror set grew from 6 modules to **52 pairs** (42 header-mirrored +
   10 byte-identical, excluding `conftest.py`) - every persona slice added
   2-4 pairs. The lockstep tax is now paid on nearly every PR.
2. The rule-of-three fired for the async personas: Elder, Guard, and
   Smasher each carry a near-identical copy of the enqueue+run machinery in
   `async_dispatch.py`. ADR-0012 kept Guard's copy separate "until a THIRD
   async persona"; ADR-0013 shipped that third copy (Smasher) and explicitly
   deferred the generalization to #77.

The temper spec `specs/0010-mirror-discipline/` modeled this lifecycle from
the start: `Copied -> Synced -> Extracted`, with `Extract` guarded on the
rule-of-three bool and `ExtractedIsTerminal`. This slice executes that
transition.

## Decision

### 1. `services/_shared/` is a PYTHONPATH root, not a pip package

The single copy of every previously-mirrored module lives under
`services/_shared/`. It is added to the import path, NOT installed:

- **Images:** `ENV PYTHONPATH=/app/_shared` in both Dockerfiles; the build
  context widens from `services/<svc>` to `services/` so `COPY _shared/`
  works (`docker build services -f services/<svc>/Dockerfile`).
- **Tests:** each service's `conftest.py` inserts `../_shared` after the
  service dir, exactly as it already inserts the service dir itself.

**Import paths are preserved.** `from adapters.install_store import ...`,
`import observability`, `patch("personas.tpm.dor_checks...")` all work
unchanged - the top-level module names are the same, only the file location
moved. This is the load-bearing choice: a `grug_shared.*` namespace would
have forced a sweep across every import AND every `patch(...)` string in
~1000 tests, for zero runtime benefit. Precedent: `install_store.py` is
already a facade that preserved DDB-era patch paths across the Postgres
port.

### 2. Package-integrity rule

A Python package is wholly owned by ONE root - a regular package found
first on `sys.path` shadows any same-named package elsewhere, so split
packages are forbidden. Consequences:

- `adapters/user_store.py` + `adapters/pg_user_store.py` (api-only) and
  `personas/smasher/trial_*.py` (webhook-only) move INTO `_shared/` with
  their packages, keep their `# API-ONLY` / `# WEBHOOK-ONLY` line-1 markers,
  and stay lazy-imported (the other service never executes them - the same
  convention that already keeps `async_dispatch` imports webhook-side).
- Genuinely per-service top-level modules stay put: `main.py`, `rerun.py`
  (deliberately different files sharing a name), `auth/`, `crypto/`
  (api), `dispatcher.py`, `consumer.py`, `async_dispatch.py`,
  `sast_benchmark/`, `spark_cave/` (webhook).
- `personas/tpm/persona.py` - the one body-diverged mirrored-package member
  (a single hardcoded logger namespace) - is unified via the convention
  every other shared module already uses:
  `logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.tpm")`.
- `conftest.py` cannot move (pytest conftest discovery is directory-bound):
  each service keeps a thin sys.path-bootstrap shim; the fixture logic
  lives once in `services/_shared/grug_shared_conftest.py`.

### 3. The async enqueue+run machinery generalizes (webhook-local)

`async_dispatch.py` collapses the three per-persona copies into one generic
enqueue + one generic run driven by a small per-persona spec table. The
machinery stays in `services/webhook/` (the api never runs async jobs -
putting it in `_shared/` would misstate its consumers). Contracts preserved
byte-for-byte:

- Public names (`enqueue_{elder,guard,smasher}_review`,
  `run_{elder,guard,smasher}_job`) remain as thin wrappers - they are live
  patch targets and lazy-import targets of the `webhook_dispatch` modules.
- Monitored log lines keep their exact names
  (`elder_job_done`, `guard_enqueue_invoke_error`, ...).
- Elder claims the RAW delivery GUID (legacy); Guard/Smasher claim
  `{delivery_id}:<persona>`. Elder's `claim_review` persona stays
  `code_reviewer` while its rerun persona stays `elder`.
- Thread names stay bounded to 19 chars (`elder-`/`guard-` + `[:13]`,
  `smasher-` + `[:11]`).

A registry-coverage test asserts every `dispatch_style == "async"`
`PersonaSpec` has an entry in the async table, so a fourth async persona
cannot silently miss the machinery.

### 4. Drift-lint retires; shadowing becomes the guarded drift class

`scripts/check-mirrored-files.sh` + `.github/workflows/check.drift-lint.yml`
are deleted. The post-extraction failure mode is a stray copy of a shared
module reappearing under a service tree and silently shadowing `_shared/`.
Guards:

- `infra/scripts/attest_mirror_policy_consistency.py` (kept filename, new
  assertions): `_shared/` layout present, zero path-shadowing, drift-lint
  gone, no `# MIRRORED` headers remain.
- `infra/scripts/attest_dor_checks_mirror.py`: `dor_checks.py` exists
  exactly once, under `_shared/`.
- A pytest twin of the no-shadowing assertion runs in the webhook suite on
  every services/** PR.

## Consequences

- 52 duplicate files deleted; every future shared-module edit is one-copy.
- Adding a persona no longer doubles its file count.
- The drift-lint CI job disappears from every PR.
- Docker build context is `services/` for both images (slightly larger
  context upload; layer contents unchanged, plus `PYTHONPATH`).
- Spec 0010 reaches its terminal `Extracted` state; its attesters now
  ground the extracted reality instead of the mirror contracts.
- The `# MIRRORED` header convention is dead; `# API-ONLY` /
  `# WEBHOOK-ONLY` markers remain for single-service modules inside
  `_shared/` packages.
