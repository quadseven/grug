# ADR-0015 - Rule-of-three deferral list resolved: the #142 walk

## Status

Accepted (2026-07-05). Completes the governance loop opened by
[ADR-0001](0001-mirror-with-rule-of-three-deferral.md) and executed in part by
[ADR-0010](0010-persona-dispatch-registry.md),
[ADR-0012](0012-guard-persona-extraction.md), and
[ADR-0014](0014-shared-package-extraction.md). Closes #142.

## Context

ADR-0001 deferred eight abstraction candidates under a rule-of-three policy,
tracked in #142 ("re-open and walk the list when a 3rd Lambda or a 3rd persona
lands"). Both trigger clauses have since resolved:

- The "3rd Lambda" clause is permanently moot: grug migrated off AWS Lambda to
  self-hosted Kubernetes (infra cutover 2026-06-12). There will never be a
  third Lambda.
- The "3rd persona" clause FIRED: Guard was the third persona (ADR-0012), and
  Smasher was the third ASYNC persona (ADR-0013). The headline extraction ran
  as #77 / ADR-0014 (`services/_shared/`, 52 mirrored pairs collapsed to one
  copy).

#142's acceptance criteria require each candidate to get a verdict against
then-current evidence, documented as ADR entries. ADR-0014 covered candidate 1;
this ADR records the rest so #142 can close without losing any deferral.

## Decision

Verdict per candidate (numbering from #142; "candidate 9" was added by the
2026-06-08 weekly arch-review comment):

| # | Candidate | Verdict |
|---|-----------|---------|
| 1 | `services/_shared/` extraction | DONE - #77, PR #495, ADR-0014. |
| 2 | TPM split into DoRGate + ScopeReview | CLOSED, overtaken - the persona-shape problem was solved by the declarative registry (ADR-0010, six personas). The predicted split shape never materialized; Chief remains one inline persona. |
| 3 | ResultPublisher seam | CLOSED, never triggered - GitHub Checks/Reviews remain the only publish target. Re-open only if a second target (chat, email, dashboard push) becomes real work. |
| 4 | `install_store` split into schema + wrapper | CLOSED, moot - the DynamoDB store was replaced wholesale by Postgres (#354); the candidate's subject no longer exists. |
| 5 | Replace `_LazyTable` global with DI'd client | CLOSED, moot - DynamoDB is gone. The successor pattern (`pg_base.get_pool()` double-checked lazy init) was a deliberate re-adoption of the same tradeoff. |
| 6 | Lift `dor_checks` rules into a `CheckDefinition` table | CLOSED, never triggered - no other persona grew an overlapping declarative check shape. Guard/Elder findings flow through the judge pipeline, not DoR-style rule tables. |
| 7 | `TokenedGitHubClient` adapter | EXTRACTED to #510 - trigger fired 2026-06-08 with concrete divergence evidence, re-verified 2026-07-05: two auth-header schemes in production code (`token` vs `Bearer`), 13 files calling `with_install_token_retry`, ~46 raw httpx sites hand-building the same transport. The URL-encoding half of the 2026-06 evidence was fixed site-by-site in the interim, which demonstrates the drift mode the adapter ends. |
| 8 | Lift `lambda_handler` into a composition root | CLOSED, moot - both `lambda_handler.py` files were deleted in the k8s migration; `consumer.py` imports handlers directly. |
| 9 | Split `dispatch_code_review` orchestration / typed `PublishOutcome` | EXTRACTED to #511 - now stronger than when flagged: Guard and Smasher import underscore-private publish helpers from `personas/code_reviewer/dispatch.py`, so the seam is real but has no owner or stability contract. |

### Deferral re-homed: persona-generic async runner

ADR-0012 deferred generalizing the per-persona async enqueue/run machinery "to
#77", and ADR-0013 re-confirmed the deferral - but #77 shipped WITHOUT it (the
extraction moved the mirrored copies; it did not unify the per-persona
runners), leaving the deferral pointing at a closed issue. It now lives on
epic #200 with an explicit trigger: generalize when a FOURTH async persona
lands. Today there are three (Elder, Guard, Smasher); Warder and Pulse are
inline, so the trigger has not fired.

## Consequences

- #142 closes; every candidate has a documented verdict and the two live ones
  are ordinary backlog slices (#510, #511) under epic #200.
- Future deferred-abstraction governance happens on epic #200, not on a
  standalone tracker: one open surface instead of two.
- `CONTEXT.md` and `specs/DESIGN.md` pointers that said "issue #142 may
  revisit" now point at #510 (updated in this change).

## References

- #142 (the tracker this resolves), #77 / ADR-0014, #510, #511, #200
- ADR-0001 (the deferral policy), ADR-0010, ADR-0012, ADR-0013
