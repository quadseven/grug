# Elder production-staged bakeoff

Date: 2026-07-21

## Result

The shipped staged discovery path caught 33 of 71 known finding cells across
all 22 scorable historical pull requests: **0.465 catch**, versus the committed
monolithic baseline's **0.183 catch**. That is a 2.54x relative increase. Both
runs reported 0.00 noise under the corpus's narrow known-false-positive metric.

| Finding class | Monolithic baseline | Production-staged |
| --- | ---: | ---: |
| correctness | 0.125 | 0.438 |
| security-scope | 0.364 | 0.273 |
| silent-failure | 0.067 | 0.667 |
| simplification | 0.000 | 0.000 |
| test-gap | 0.250 | 0.583 |
| type-design | 0.000 | 0.000 |
| upstream-semantics | 0.429 | 0.857 |
| **overall** | **0.183** | **0.465** |

This proves materially better discovery recall on this corpus. It does not by
itself prove final published precision because the production replay currently
stops after staged discovery; repository verification, the judge/refute gates,
and GitHub publication are not yet replayed end to end.

## Method

- Corpus: the committed Elder ledger, fetched again from the real GitHub pull
  request diffs; 22 scorable cases and one unscorable case skipped explicitly.
- Baseline: `services/webhook/elder_eval/baseline.json`, recorded against the
  historical monolithic prompt path (22 cases, two hunk-bounded diffs).
- Candidate: `python -m elder_eval --production`, which calls the real
  `review_diff` staged path with the production code-specialist model and does
  not truncate the diff before the cohort planner.
- Honest coverage rule: a partial staged case is excluded from scoring instead
  of turning unreviewed cohorts into misses or a false clean result.
- The first run scored 21 complete cases at 0.478 catch. The largest first case
  cold-loaded the 91.7 GB model and completed five of eight cohorts before the
  production review budget reserved its finalization window. A warm isolated
  replay completed all eight cohorts and caught one of that case's four known
  cells. Combining the exact cell counts gives 33/71 = 0.4648.

## Operational smoke evidence

- The remote gateway returned HTTP 200 for every completed cohort request.
- The code specialist loaded through Ollama; no vLLM runtime participated.
- Structured coverage correctly named the three skipped cold-start cohorts,
  and the evaluator refused to score that partial attempt.
- The warm retry completed the previously skipped case without truncation.

The cold-start result is a real product risk: model loading currently consumes
the same wall-clock budget as review generation. The scheduler should account
for readiness separately or keep the assigned specialist warm before large PRs.

## Precision caveat observed live

The corpus noise metric only counts emissions in classes already labeled as
false-positive-only for a case. It is not general precision. During this same
session, Elder's live review of PR #720 emitted three findings that were
contradicted by the current code or runtime:

- it claimed `time.monotonic()` was initialized to zero when the code assigns
  the current monotonic value;
- it proposed enabling role/user mentions even though both send boundaries use
  `AllowedMentions.none()`;
- it called a logged, user-visible detached-task exception boundary a silent
  broad-exception failure and incorrectly included `CancelledError`, which is a
  `BaseException` on the tested runtime.

All three threads were answered with evidence and resolved without changing
the code. This is why the next gate must replay final adjudication and measure
blinded accepted/fixed/rejected precision, not optimize discovery recall alone.

## Next measurable improvements

1. Extend production replay through judge, repository verification, refute,
   anchor validation, and final publication selection.
2. Preserve verifier outcomes in each finding's provenance and render them in
   the collapsed evidence section.
3. Add a cold-readiness phase so model load cannot silently consume cohort
   coverage budget.
4. Add adjudicated false-positive cases from resolved review threads to the
   corpus, scoped by rule and evidence pattern rather than broad prompt bans.
5. Gate changes on full coverage, discovery recall, final precision, latency,
   and accepted/fixed/rejected outcomes together.
