# ADR-0011 - Gate Elder finding publication on the exploitability judge verdict

## Status

Accepted (2026-07-03). Implements #467 (epic #464 slice 3, #346 cross-cutting
item 1). Independent of the registry slices (#465/#466).

## Context

The Elder review already runs a SECOND LLM pass - the judge
(`llm_client.judge_findings`, orchestrated by `judge.run_judge`) - that grades
every published finding real-vs-false-positive. But the verdict only ever fed
DD LLM-Obs evals: a finding the judge itself called a false positive was STILL
posted as an inline comment. The `v2` review prompt even claims filtering
happens. Wiring the judge to actually gate publication is the single biggest
precision win available with zero new infrastructure (#346).

Two constraints shape the design:

- **The judge is an LLM and can be wrong or unavailable.** A judge outage, a
  parse failure, or a wrong "not-real" label on a real bug must never HIDE a
  finding the developer needed - especially a high-severity one.
- **The judge call was post-publish.** Gating requires the verdicts BEFORE the
  inline comments post, so the judge LLM call moves onto the pre-publish path
  of the async offload job (never the webhook ACK path).

## Decision

**1. `FindingJudgement` gains a `confidence: float` (0.0-1.0).** The judge
prompt now emits `{"index", "is_real_bug", "confidence", "reasoning"}`. Parsing
defaults a missing/malformed confidence to `0.0` - below any floor - so an
old-shape or garbled verdict can NEVER cause suppression (fail-safe toward
publishing).

**2. A finding is suppressed iff ALL hold** (`judge.partition_findings`):
- the judge returned a verdict for it,
- `is_real_bug is False`,
- `confidence >= _JUDGE_CONFIDENCE_FLOOR` (0.7),
- `severity in {low, medium}`.

HIGH/CRITICAL findings ALWAYS publish regardless of the verdict - a judge FP on
a critical must never bury it (#346 risk control). A finding with no verdict
(judge outage, hallucinated index, findings over the judge budget) is KEPT
(fail-open).

**3. The judge call moves pre-publish, split into three primitives**
(`judge.py`, mirrored):
- `grade_findings(...)` - the LLM call, gated on findings-present + the
  existing `_JUDGE_MAX_FINDINGS` budget; fail-open to `()` on any error.
- `partition_findings(findings, verdicts, *, confidence_floor)` -> `(kept,
  suppressed)`, pure.
- `submit_evals(findings, verdicts, *, review_span_context)` - the DD LLM-Obs
  eval submit (unchanged loop), still gated on a review span, still redacting
  exposed-secret reasoning. `run_judge` is retained as the `grade_findings`
  + `submit_evals` compose (the eval-only path, no filtering) so its contract
  and tests are unchanged.

**4. Dispatch grades pre-publish, publishes survivors, submits evals for ALL
findings post-publish.** The check-run and inline review carry only `kept`; the
DD eval denominator still counts `kept + suppressed`, so the precision metric
(`judge-confirmed-real / published`) stays computable and the learning corpus
(#361) does not lose the suppressed rows. The check-run summary gains a
transparency line - "Grug held back N weak finding(s) his judge doubted" - so a
suppressed finding is never a silent gap.

**5. `CodeReviewEvaluation.with_findings(findings)`** (persona.py) replaces the
finding set and re-derives `conclusion` by the SAME rule as
`with_extra_findings`. Because suppression only ever removes advisory-severity
findings, the conclusion is provably unchanged - but re-deriving keeps the
invariant honest rather than relying on it.

## Consequences

### Positive

- Precision up: judge-confirmed false positives at medium-and-below stop
  reaching the PR. Recall of true positives is unchanged (high/critical always
  publish; low-confidence verdicts never suppress).
- Fail-open everywhere: a judge outage degrades to today's behavior (publish
  all), logged.
- The suppressed-count summary line makes the filter auditable per review.

### Negative / accepted

- One judge LLM call is now on the async job's pre-publish path (it was
  post-publish). This adds the judge's round-trip to the async review latency,
  NOT to the webhook ACK - the developer already waits for the async review.
  Bounded by `_JUDGE_MAX_FINDINGS` (over budget -> no grading -> publish all).
- The confidence floor is a single global constant (0.7). Per-repo thresholds
  learned from reactions are #361, deferred.
- The judge backend/model is unchanged - the in-house Cave exposed-secret judge
  is #439, out of scope.

## References

- #467 (this slice), #464 (epic), #346 (capability PRD), #361 (learning loop),
  #439 (in-house judge)
- ADR-0006 (SAST detection + exploitability judge), ADR-0003 (verdict model)
- `specs/DESIGN.md` "LLM-as-a-judge" + "Judge-gated publication" rows
