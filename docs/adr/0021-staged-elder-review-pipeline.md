# ADR-0021 - Staged Elder review pipeline

## Status

Accepted (2026-07-20). Extends ADR-0019; it does not change the tiered arm
policy or the GitHub publication contract.

## Context

Elder historically gave one model call the whole reviewable diff, full-file
context, cross-file snippets, production signal, repository guidance, and the
entire findings contract. That has two structural problems:

1. The hardest PR determines reliability for the whole review. A model that
   stalls or exhausts its context produces no findings at all.
2. A single answer makes context gathering, bug discovery, and final
   adjudication look like one job, even though Grug already has separate judge,
   verification, deterministic-analysis, and publication stages.

The July 2026 model bakeoffs remain useful for model and runtime comparisons,
but a monolithic prompt measures whether one model can perform the whole
product. It does not tell us which model is best for a bounded role.

## Decision

Large Elder reviews use a map-review-reduce pipeline.

### Map

`review_pipeline.plan_review` is a pure, model-independent planner. Diffs at or
below `GRUG_REVIEW_COHORT_CHARS` remain one cohort. Larger diffs are grouped by
repository area, ordered into contract, implementation, verification, and
documentation layers, then packed to the configured character budget. An
oversized individual hunk is kept intact in its own cohort metadata, but is not
sent to a model: splitting it would corrupt line anchors and sending it would
break the prompt bound. Elder reports that cohort as failed and partial (or
fully degraded when it is the only cohort). Elder never silently truncates a
hunk.

The planner renders a compact REVIEW MAP listing every cohort, dependency
layer, changed path, and structural reviewability concern. It contains no diff
bodies. The prompt treats the map and PR intent as
untrusted data and tells each reviewer to report findings only on its current
diff.

The default cohort budget is 48,000 diff characters and six changed paths.
`GRUG_REVIEW_COHORT_CHARS` is clamped to 8,000-100,000 and
`GRUG_REVIEW_COHORT_FILES` is clamped to 1-20. Eligible full-file context has a
separate 40,000-character per-file cap in addition to the existing 800-line
cap. Together these prevent an accidental configuration or a few very wide
source lines from restoring a massive prompt.

### Review

Each cohort runs independently through the existing `GRUG_REVIEW_DEPTH` policy:

- `tiered`: Cave coder now; selective reasoner later.
- `deep`: coder and reasoner concurrently inside each cohort.
- `fast`: coder first, then reasoner only on failure.

Only full-file contents for the current cohort's changed paths are included.
The bounded cross-file snippets and runtime signal remain shared evidence.
Discovery cohorts run sequentially because each model has one generation slot;
parallel requests to the same model would spend their transport timeout waiting
in its queue. Before starting another cohort, the scheduler reserves one full
model timeout plus the bounded cloud-fallback allowance. If that work would
cross `GRUG_STAGED_REVIEW_BUDGET_S` (700 seconds by default), remaining cohorts
become explicit failures and completed findings publish as a partial review.

The post-publish reasoner append uses the same cohort plan when invoked
directly. Tiered reviews that already required staging do not start that second
discovery pass inside the same durable job: their findings still receive the
hot reasoner's evidence-scoped adjudication, while the 800-second watchdog
retains time to publish and clean up.

### Adjudicate

Discovery and adjudication are separate model roles. The code-specialist model
on the cold Spark finds candidates inside bounded cohorts. Each judge batch is
then reduced again to an evidence packet containing only the changed hunks and
full-file contents for the candidate files. The permanently resident
`qwen3.5:122b` reasoner on sparkicus judges that packet; it does not reread the
whole PR and does not compete with discovery for the cold Spark's model slot.

An owned-judge response is usable only when it contains exactly one verdict for
every candidate index. Empty, partial, duplicated, or out-of-range verdict sets
fall back to the existing cloud judge with secret-redacted evidence. A total
judge failure remains fail-open: it cannot hide a finding. The high-severity
adversarial refute gate uses the same evidence boundary and fallback policy.

### Reduce and validate

Cohort responses reduce into the existing `LlmReviewResponse` interface.
Duplicate `(path, line, rule)` findings merge exactly as multi-model findings
already did: strongest severity wins, useful remediation fields survive, and
all producer span origins remain attached.

The reduced response now also carries structured coverage: total, completed,
and failed cohort indexes, cohort labels, and reviewability concerns. Each
finding origin records its model, bounded evidence paths, cohort coordinates,
and immutable head SHA. GitHub comments expose this in a collapsed provenance
section; the check summary reports exact coverage rather than only a generic
partial warning.

Reviewability concerns are separate from model capacity. Elder flags an
indivisible oversized hunk or a module that spans multiple bounded units as a
proof/maintainability risk. It does not call a cohesive change bad merely for
being large.

If one cohort fails but another returns a valid review, successful findings are
kept and the response carries a partial-review error. Validated findings still
publish, while the GitHub check is explicitly marked partial and forced
advisory even for repositories that normally block on Elder. The structured
warning `llm_staged_review_partial` records failed cohort indexes. If every
cohort fails, the existing `parse_failed` versus `all_failed` distinction is
preserved.

All downstream controls run once over the merged result: diff-line anchoring,
evidence-scoped LLM adjudication, repository verification, the high-severity
refute gate, deterministic complexity and claim checks, prior-comment
deduplication, check-run publication, and reaction learning.

## Consequences

### Positive

- One pathological file or model generation no longer necessarily erases the
  rest of a large review.
- Review prompts have a stable upper bound while original hunks remain intact.
- The hot reasoner spends its context proving or refuting concrete candidates,
  instead of repeating code discovery over unrelated files.
- Small PR behavior, API shape, scoring, and publication remain compatible.
- `elder_eval --production` compares the shipped staged discovery path instead
  of only a monolithic backend prompt. `--production --published` separately
  scores findings surviving anchor validation, judge suppression, diff-only
  verification, and the high-severity refute gate. Full-file-dependent verifier
  rules remain inconclusive until the follow-up below. Both modes refuse to
  score partial coverage as misses or as a clean zero.
- Conventionally matched implementation and test files share one semantic
  cohort when they fit. When that proof unit exceeds its bound, review coverage
  carries a `cross-cohort-proof` concern asking authors to split the change or
  reduce coupling.
- Every candidate model, including Nemotron, gets a more representative test:
  bounded review cohorts rather than a trivial smoke or a maximum-size prompt.

### Tradeoffs

- A bug whose proof requires reading changed code in two different cohorts may
  be harder to detect. The shared map and cross-file snippets reduce, but do not
  eliminate, that risk.
- Large reviews make more model calls. Sequential scheduling protects each
  single-slot model, and the review-wide budget may deliberately leave later
  cohorts uncovered rather than let the watchdog erase all completed work.
- A partial review cannot block a merge. It still publishes validated findings
  from completed cohorts, so operators and developers see both the useful
  evidence and the incomplete-coverage warning.

## Follow-ups

- Feed a trusted, snapshot-scoped Teller summary into the REVIEW MAP without
  adding a second summarization call or a hidden SaaS dependency.
- Extend `elder_eval` reporting with per-cohort latency and fetch immutable
  full-file snapshots so its repository verification matches dispatch exactly.
- Replace filename-based implementation/test pairing with dependency-graph and
  symbol clustering for relationships that naming conventions cannot express.
- Re-run the 22-PR real corpus against the staged endpoint before changing the
  production discovery-model assignment. Report discovery catch rate and final
  post-adjudication precision separately.

## Rollback

Set `GRUG_REVIEW_COHORT_CHARS=100000`. The normal 200,000-character dispatch
cap may still create two cohorts; a code rollback is required to restore the
old single-call behavior for the maximum-size diff. `GRUG_REVIEW_DEPTH` remains
an independent arm-policy rollback described in ADR-0019.
