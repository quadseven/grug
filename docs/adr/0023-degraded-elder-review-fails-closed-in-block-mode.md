# ADR-0023 - A degraded Elder review fails closed in BLOCK mode

## Status

Accepted (2026-10-17). Decided by the owner on grug#1212. Refines the
advisory-first gate in `_publish_shape` (personas/code_reviewer/dispatch.py)
for Elder only; Guard and Smasher share that gate and are unchanged.

## Context

Since #1205, a repo whose Elder is blocking (`code_reviewer_blocking`) gets
`Grug - Elder` as a required status check. GitHub treats a required check
that concludes `neutral` as passing.

Elder concluded `neutral` whenever its review was degraded: every model
call failed (`all_failed`), the answer did not parse (`parse_failed`), a
partial review (`partial_review`, including files not walked), the
low-confidence result from #1204 (`low_confidence_review`), a diff that
could not be fetched or parsed, and the durable lane's fail-open terminal
checks. So during a model outage, or on a large PR where cohorts dropped, a
PR could merge with `Grug - Elder` required and no real review behind it.
It was seen on a private repo: a first head concluded `neutral` with 9
files not walked, and only a hand-held merge waited for the re-review.

### Data

402 `Grug - Elder` check runs started 2026-10-10..17 on 10 repos (the 25-40
most recently updated PRs per repo, last 6-15 commits each): 319 success,
32 failure, 51 neutral.

```
Neutral cause (check title)                 runs  degraded?  code path                                         owner
------------------------------------------  ----  ---------  ------------------------------------------------  -----------------
coverage partial (some cohort not done)       22  yes        llm_client staged scheduler + merge               see split below
  - only "never attempted" cohorts            10  yes        _run_staged_cohorts reserve (_Tier1Review)        #1216 / PR #1219
  - cohorts failed AND never attempted         6  yes        both of the above and below                       #1216 + #1196
  - cohorts failed only (ladder exhausted)     6  yes        _ChunkLadder / cloud chain / SaaS fallback        #1196 + backends
superseded - new head                          9  no         dispatch fail-open on a stale head                by design
eyes clouded (parse_failed), whole review      7  yes        _Tier1Review.run_whole_diff -> parse              #1196
no_diff (incl. 4 Living Hunt deltas)           6  no         empty diff / empty delta                          by design
low confidence review                          4  yes        degenerate-empty rule kept as low confidence      #1097 (#1204)
eyes clouded (all_failed), whole review        2  yes        every ladder tier failed                          #1196 + backends
other (Cave omens title)                       1  ?          -                                                 -
------------------------------------------  ----  ---------
total neutral                                 51  35 degraded, 15 by design, 1 unclear
```

Backend state behind the "failed" rows, from about 1.6h of consumer logs
(2026-10-17 20:26-22:02 UTC, 56 reviews dispatched, 22 staged):

```
backend / tier                 result                                   count
-----------------------------  ---------------------------------------  -----
OpenCode Go (3 models)         401, API key expired (models benched)       36
OpenRouter free model          429 per-day cap (local limiter ~600/1000)  139
OpenRouter paid fallback       403 key total limit reached                   11
Cave gateway                   503 (one inference host being rebuilt)     129  (12 OK)
Poolside                       ReadTimeout at the 45s tier timeout          26  (159 OK)
```

What the data says:

- Of the 35 degraded neutrals, 18 have a later run on the same PR in the
  sample: 13 of those concluded success and 5 were degraded again (17 have
  no later run yet). Most degradation is transient, but not all of it
  clears on the next push.
- The "never attempted" rows are a scheduler bug (#1216, PR #1219), not an
  outage; once fixed they should mostly disappear.
- "low confidence" (4) reviewed every cohort; on its face it is the weakest
  case for failing closed.

## Decision

**In BLOCK mode a degraded Elder review concludes `failure`, for every
degraded reason. WARN mode is unchanged and stays `neutral`.**

- Covered: every model call failed, a parse failure, a partial review,
  files not walked, `low_confidence_review`, a diff that could not be
  fetched or parsed (including one too large for GitHub to serve), a deep
  arm that was escalated but did not run, and the durable lane's terminal
  completions for the current head (GitHub unavailable at the freshness
  check, an unknown skip, a PR that kept changing mid-review, a job that
  dead-lettered).
- Not covered, by design: `no_diff` (there was nothing to review) and a
  superseded head (it is not the head that merges; the current head has its
  own review queued).
- The first line of every degraded summary, in both modes, reads:
  "Degraded review (<reason>): not a verdict. Comment `/grug improve` to
  re-run once the backend recovers."
- The inline review event of a degraded review stays `COMMENT`: it is not a
  verdict to request changes on.
- **Long outage:** the operator drops that repo's Elder to WARN in the
  dashboard, then back to BLOCK after.

The policy lives in one module, `personas/code_reviewer/degraded_gate.py`:
`degraded_conclusion(blocking)`, `degraded_lead_line(reason)` and the
merge sentence the summaries use. Every exit asks it. `_publish_shape`
takes `degraded_fails_closed=True` at Elder's call sites only, and the
`attest_code_reviewer_dispatch.py` attester checks that the fail-closed
branch exists, comes before the neutral branch, and that every Elder call
opts in.

Where a terminal path cannot read the repo's mode (the dead-letter
announcement after the same outage, or a config-store error), it fails
closed: a WARN repo's Elder check is not required, so a red mark there
holds nothing, while a `neutral` on a blocking repo would pass a required
check with no review behind it.

## Reasoning

A required check must mean a review happened.

Why not stay neutral for low-confidence clean results: on 2026-10-17 every
low-confidence result came from all but one model failing. The cause was
an expired key and a review model no backend served, so the "clean" answer
was one unconfirmed model. That is an outage signature, not a clean review.

Why `failure` over `action_required`: `action_required` expects an action
button and reads as a bug. `failure` with the re-run line is plain.

## Consequences

- Merges on BLOCK repos wait out a backend outage. They resume with one
  `/grug improve`, and an outage that blocks merges gets fixed, while one
  that passes them goes unnoticed.
- Elder's own check on this repo gets stricter in the same way.
- The Cave fallback heal (`cave_fallback.py`) still publishes `neutral`
  after a successful Cave review; that is a review that happened, not a
  degraded one, and its blocking-aware conclusion remains the follow-up its
  own comment names.
