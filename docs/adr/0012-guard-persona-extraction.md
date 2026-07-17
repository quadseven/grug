# ADR-0012 - Extract the Guard persona; ownership by import; per-persona async jobs until rule-of-three

## Status

Accepted (2026-07-03). Implements #466 (epic #464 slice 2). Builds on
ADR-0010 (registry dispatch): Guard is the first persona added through the
"one PersonaSpec + one module + default-config keys" seam.

## Context

The landing page sells "Guard" (SCA, secret scanning, SAST) as its own
persona; in reality that work shipped INSIDE Elder - four deterministic
candidate sources (`sast.py`, `sca.py`, `secret_scan.py`, `iac_scan.py`)
feeding the exploitability judge, merged into Elder's evaluation and
published under "Grug - Code Review". Users could not see, toggle, or block
on security findings separately, and the roster over-promised. The
detectors exist and are benchmarked; extraction is packaging.

Three design questions had real alternatives:

1. **Where does Guard's dispatch code live relative to the detectors?**
2. **How does a SECOND async persona execute?** (Elder's offload is a
   background daemon thread under `GRUG_K8S_RUNTIME`, #272/#368.)
3. **How do Elder and Guard share one webhook delivery's idempotency?**

## Decision

**1. Ownership by IMPORT, not file move.** The detector modules and the
exploitability judge STAY under `personas/code_reviewer/`; the new
`personas/guard/dispatch.py` imports them (and Elder's fetch/publish
helpers: `_fetch_pr_diff`, `_fetch_file_contents`, `_build_review_result`,
`_publish_shape`, `_prior_finding_keys`, `_resolve_result`). Moving five
mirrored files would churn the drift-lint list, the SAST benchmark imports,
and every historical reference for zero behavior; the issue's own
acceptance ("the benchmark harness targets the shared detector code")
assumes they stay. The `services/_shared` extraction that would give these
helpers a neutral home is #77 - and Guard, the third persona, is exactly
the rule-of-three trigger that re-opens it (see the #142 note).

**2. Per-persona async machinery, generalization deferred.**
`async_dispatch` gains `enqueue_guard_review` + `run_guard_job` mirroring
the Elder pair rather than a persona-generic
`enqueue_persona_review(persona_key, ...)`. Deliberate: Elder's enqueue is
a live patch target in ~15 tests and its log event names
(`elder_enqueue_failed`, `elder_job_unhandled`) are wired into a production
DD monitor - a rename-into-generic risks byte-identical-behavior for a
refactor Guard does not need. A THIRD async persona triggers the
generalization (rule-of-three, same policy as ADR-0001). The offload
monitor now alerts on the union of Elder + Guard event names.

**3. Namespaced delivery claim.** Elder and Guard both dispatch from the
SAME webhook delivery. `claim_delivery` is keyed on the raw GitHub GUID, so
Guard claims `{delivery_id}:guard` - an unnamespaced claim would let
whichever persona ran first mark the delivery consumed and silently skip
the other. Per-head-SHA idempotency reuses `claim_review` with
`persona="guard"` (already persona-parameterized). GitHub delivery GUIDs
are UUIDs and cannot contain `:guard`, so the namespace cannot collide.

**4. Guard's surfaces.** Own check-run ("Grug - Guard", Guard voice), own
RepoConfig flags (`guard_enabled` default True, `guard_blocking` default
False - advisory-first like Elder), own Activity rows
(`review_types.Persona` += "guard"; the caveman name IS "guard"), inline
review comments through Elder's shared dedup path (`grug-rule` markers are
rule-scoped, so no cross-persona collision). Degraded runs publish neutral
and record errored Activity rows ("no lies", ADR-0003).

**5. Guard rides the rerun lane end-to-end (codex PR #482).** The api
`RerunRequest` accepts `persona=guard`, the rerun consumer dispatches
`dispatch_guard_review` with `guard_blocking`, and `run_guard_job`
self-recovers (#418) by enqueueing a guard rerun on an unhandled dispatch
error. Without this, two real gaps existed: an errored Guard Activity row
showed a rerun button that silently no-op'd (validation rejected guard and
the consumer skipped it), and - worse - the head-SHA claim taken before
dispatch meant a transient Guard failure suppressed that SHA's security
check until a new push, with no recovery path.

**6. Elder loses the security concat.** `dispatch_code_review` is the LLM
diff review only; its judge-gated publication (#467) now grades only LLM
findings. Guard's findings get their precision pass from
`judge_candidates` (their own exploitability judge) - they do NOT pass
through the #467 gate a second time.

## Consequences

### Positive

- Users see/toggle/block security separately; the roster claim is honest.
- The registry seam proved out: adding Guard touched ZERO dispatcher/store
  logic - one spec entry, one webhook_dispatch module, two dict keys.
- Two independent check-runs mean a security regression cannot hide behind
  a clean style review or vice versa.

### Negative / accepted

- **Second diff fetch + file-contents fetch per PR** (Elder's job and
  Guard's job each fetch independently). Accepted: the fetches are cheap
  GitHub API calls against the same token, and sharing them would couple
  the two async jobs' lifecycles. Revisit with the #77 extraction.
- Guard importing Elder's private helpers (`_`-prefixed) is a smell the
  #77 extraction resolves; the mirror discipline keeps both sides in
  lockstep meanwhile.
- The dashboard's guard TILE is still the localStorage roster mock: the
  tile is GLOBAL while the real flag is PER-REPO, so wiring it is a UX
  decision (apply-to-all semantics?), not plumbing - follow-up issue. The
  per-repo flip works today via the API (`RepoConfigPayload.guard_*`).

## References

- #466 (this slice), #464 (epic), #77/#142 (rule-of-three), #272/#368
  (async offload), #418 (self-recover), ADR-0001/0002/0003/0010
- `personas/guard/` + `async_dispatch.run_guard_job`
- `specs/DESIGN.md` "Guard persona" rows
