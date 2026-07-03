# ADR-0010 - Persona dispatch iterates the registry; the store's default dict stays the literal extension point

## Status

Accepted (2026-07-03). Closes the wiring half of #465 (epic #464); the
registry table itself landed additively in PR #476. Pays the ADR-0001
rule-of-three debt flagged on #142/#77 at the dispatch seam.

## Context

Adding a persona before this ADR meant hand-editing three places, twice
(api + webhook mirrors):

1. `dispatcher._handle_pull_request` grew a bespoke if-block per persona
   (TPM inline, Elder enqueue), each hand-deciding its missing-`repo_id`
   default (TPM=enabled, Elder=disabled - folklore, not policy).
2. `pg_install_store.get_repo_config` copied each persona flag out of the
   stored row field-by-field; `set_repo_config` took one explicit kwarg
   per flag.
3. The dead `pull_request_review` branch in `dispatch()` (a v1.5
   placeholder that Elder's real dispatch never used) still shadowed the
   generic fallthrough.

The third persona (Guard, #466) fires ADR-0001's rule-of-three trigger.
`personas/registry.py` (PR #476) already declares the per-persona facts
(`PersonaSpec`: key, canonical name, check-run name, config flags +
defaults, dispatch style, missing-repo policy) and is locked against the
hand-wired behavior by `test_registry.py` - but nothing consumed it.

Two constraints shaped the wiring:

- **Temper spec 0009 attests `_DEFAULT_PERSONA_CONFIG` as a literal
  dict.** `attest_repo_config_default_dict.py` AST-checks for a
  module-scope literal `{str: bool}` assignment in both
  `pg_install_store.py` mirrors. Deriving the dict from the registry at
  import time (`_DEFAULT_PERSONA_CONFIG = default_persona_config()`)
  would fail the attestation.
- **Existing test seams must survive unchanged.** The dispatcher suite
  patches `dispatcher.is_persona_enabled`, `dispatcher.get_repo_config`,
  `async_dispatch.enqueue_elder_review`, and
  `personas.tpm.persona.evaluate_pull_request` /
  `publish_tpm_evaluation`. The refactor must keep those names resolved
  at those import paths, at call time.

## Decision

**1. The dispatcher iterates `personas.registry.REGISTRY`.**
`_handle_pull_request` keeps its event/payload/allowlist gates, then runs
one generic loop:

- Enablement: `repo_id is None` resolves via `spec.missing_repo_policy`;
  otherwise `is_persona_enabled(install_id, repo_id, spec.key)`. This is
  the same asymmetry the two hand-written blocks encoded (Chief on,
  Elder off), now data.
- Blocking: personas with a `blocking_flag` get one `get_repo_config`
  read; the flag value lands on the context. Personas without one never
  trigger the read (same call pattern as before: zero store reads for
  TPM, one for an enabled Elder).
- Dispatch: `PersonaSpec` gains `events: tuple[str, ...]` and
  `dispatch_module: str`. The loop imports `spec.dispatch_module`
  (stdlib `importlib`, cached after first use, lazy so the ACK path's
  cold start stays cheap) and calls its module-level
  `dispatch_pull_request(ctx) -> dict[str, str]` - the uniform "run
  persona" seam.
- Isolation: each call is wrapped in a per-persona try/except that logs
  `persona_dispatch_unhandled` and records
  `{"persona": spec.key, "result": "unhandled_error"}` - one persona's
  import failure or bug cannot starve the others (the independence
  criterion from #185).

**2. Per-persona webhook dispatch moves into the persona's package.**
`personas/tpm/webhook_dispatch.py` (the former `dispatcher._dispatch_tpm`
body) and `personas/code_reviewer/webhook_dispatch.py` (the former Elder
enqueue block, including the `elder_enqueue_failed` log). Both are
mirrored (`check-mirrored-files.sh` MIRRORED_WITH_HEADER) and keep their
inner imports lazy so the existing patch targets
(`personas.tpm.persona.*`, `async_dispatch.enqueue_elder_review`) still
intercept.

**3. `PullRequestContext` (in `registry.py`) is the dispatch payload.**
Frozen dataclass: `installation_id`, `owner`, `repo_name`, `head_sha`,
`pr_number`, `pr_body`, `payload`, `delivery_id`, `blocking`. Persona
modules read what they need; new personas get the full event context
without a signature change.

**4. The store's `_DEFAULT_PERSONA_CONFIG` stays a literal dict; the
plumbing around it goes generic.** `get_repo_config` derives the flag
fields from the dict's keys (one comprehension instead of per-flag
copies); `set_repo_config` accepts persona flags as keyword arguments
validated against the dict's keys (unknown -> `TypeError`, preserving
the old unexpected-kwarg semantics). Adding a persona's flags = adding
dict keys; the get/set/enabled paths pick them up with no further store
edits. `test_registry.py` keeps `registry.default_persona_config() ==
_DEFAULT_PERSONA_CONFIG` locked, so the registry and the dict cannot
drift silently. The literal-dict shape is what temper spec 0009 attests
(`adding_persona_extends_default_config_dict...` is the spec's own
extension contract), so SSOT-by-locked-test is chosen over
SSOT-by-construction.

**5. The dead `pull_request_review` branch is retired.** The event now
falls through to the generic "no handler" no_op.

**Adding a persona is now:** one `PersonaSpec` entry + one persona
package exposing `dispatch_pull_request` + its flag keys in
`_DEFAULT_PERSONA_CONFIG` (+ the frontend toggle). No dispatcher edits,
no store edits.

## Consequences

### Positive

- Guard (#466), Warder (#471), Pulse (#472) and every #464 tracer land
  as data + one module, not dispatcher surgery. The toy-persona test in
  `test_dispatcher_registry.py` proves the seam.
- The missing-`repo_id` policy is a named, tested registry field instead
  of dispatcher folklore.
- The rule-of-three payoff for #77 gets cheaper: the dispatch seam and
  the config plumbing are now shaped identically in both mirrors, so a
  future `services/_shared` extraction moves one registry + one loop
  instead of untangling per-persona blocks.

### Negative / accepted

- **Inline vs async failure handling (audit #477 stage-2 + codex
  peer-review).** The per-persona guard isolates every persona so one
  failure never starves the others (#185), but the guard's OUTCOME
  depends on `dispatch_style`:
  - **inline** (Chief): an unexpected dispatch exception 200s with
    `result=unhandled_error`. A retry would duplicate the check-run
    publish it already attempted, so swallowing is correct. The
    `persona_dispatch_unhandled` log (carrying `delivery_id`, `kind`,
    `head_sha`, `dispatch_style`, persona + repo coords) is the alerting
    channel, wired to the `grug-webhook-persona-dispatch-unhandled`
    monitor.
  - **async** (Elder): an unexpected dispatch exception is a HANDOFF
    failure - the review was never durably enqueued. Swallowing it into
    a 200 would drop the review with no GitHub redelivery, strictly worse
    than the pre-registry code where the enqueue exception propagated and
    500ed. So the loop re-raises the first async-handoff error AFTER
    running every persona: the delivery is non-2xx, GitHub redelivers,
    and the inline personas that already ran re-publish idempotently per
    head_sha (parity with the old behavior).
  The deliberate #272 EXPECTED-failure path is unchanged: when
  `enqueue_elder_review` returns False (throttle/backpressure), Elder's
  dispatch returns `result=enqueue_failed` and 200s - a drop that
  re-triggers on the next push, monitored by the elder-offload alert, NOT
  re-raised. Store-read failures (`is_persona_enabled`, `get_repo_config`)
  remain OUTSIDE the guard and still 500, preserving replay eligibility
  for store outages.
- The moved TPM/Elder dispatch logs (`tpm_publish_failed`,
  `tpm_dispatch_unhandled`, `elder_enqueue_failed`) keep their event
  names and extras but move logger namespace from
  `grug.webhook.dispatcher` to the DD_SERVICE-derived persona loggers
  (`<service>.persona.<key>.webhook_dispatch`). DD queries filtering on
  the event names (the canonical practice) are unaffected; any query
  pinned to the old logger name needs the `old OR new` transition form.
- `dispatch_module` is a string, not a callable - a typo fails at first
  dispatch, not import. Covered by the registry test asserting every
  registered module imports and exposes `dispatch_pull_request`.
- The registry is still consulted only by the `pull_request` handler.
  `events` is declared per-spec but a second event type will need its
  own handler loop (deliberately deferred - no second evented persona
  exists).

## References

- #465 (this slice), #464 (epic), PR #476 (registry scaffold)
- #142 (rule-of-three re-evaluation), #77 (shared-package extraction)
- ADR-0001 (mirror-with-rule-of-three-deferral), ADR-0002 (caveman
  persona names)
- `specs/0009-repo-config/` + `infra/scripts/attest_repo_config_default_dict.py`
  (the literal-dict constraint)
- `specs/DESIGN.md` section "Persona dispatch - the registry"
