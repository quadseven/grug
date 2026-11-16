# ADR-0028 - Elder posts its verdict as a formal review state

## Status

Accepted (2026-11-16). Decided by the operator. Amends the advisory-first
contract in `personas/code_reviewer/dispatch.py`: `code_reviewer_blocking`
keeps choosing the check-run conclusion, and no longer chooses the review
event. Guard and Smasher are unchanged. Tracked in #1758.

## Context

Grug is the single reviewer on every quadseven repository, and merge gates
count reviews by reading GitHub review state. Elder only ever posted
`COMMENT` or `REQUEST_CHANGES`, and in advisory mode only `COMMENT`. A clean
head left no review state at all, so a gate could not tell "Elder reviewed
this head and found nothing blocking" from "Elder never looked".

## Decision

On every head Elder reviews, the Tier-1 pass posts one formal review whose
state is the verdict (`personas.review_publish.verdict_event`):

- `APPROVE`: the review is clean. No blocking finding stands, carried-forward
  findings included; the carry-forward fold ran (or there was nothing earlier
  to fold); every lock file was read; and the run was not degraded.
- `REQUEST_CHANGES`: a blocking (high or critical) finding stands, new or
  carried forward.
- `COMMENT`: the run is degraded (any `degraded_reason`, `no_diff` included)
  or superseded. A degraded run never approves: fail closed. A degraded run
  with a blocking finding still posts `COMMENT`, as ADR-0023 decided.

With nothing new to say inline, the `APPROVE` or `REQUEST_CHANGES` review is
posted body-only, so the state always lands.

**The blocking flag does not gate the state.** `code_reviewer_blocking` picks
only the check conclusion (`plan_publish`). The review state is posted in both
modes, because gates read the state. The consequence is deliberate: a
`REQUEST_CHANGES` from Grug blocks GitHub's merge button (as it did in
blocking mode, #1581) on a repository in advisory mode too, until a clean
re-review (`/grug improve`) approves and withdraws it, or a maintainer
dismisses it. An `APPROVE` counts toward a ruleset's required approvals
only where that ruleset counts app reviews; the quadseven rulesets require
none, so there only gates that read the state count it. A degraded run
never requests changes, so a backend outage cannot block a merge this way.

**Withdrawals.** The newest Elder verdict is the standing one:

- A run that approves dismisses Elder's own older `REQUEST_CHANGES` (as
  #1581 did for a `success` check, now in either mode).
- A re-review that posts `COMMENT` dismisses Elder's own older `APPROVE`, so
  a degraded run cannot leave an approval standing.
- The deep append never approves a second time. It escalates to
  `REQUEST_CHANGES` on a blocking finding, and when the head no longer earns
  Tier-1's approval (a blocking finding, or the deep arm degraded the review)
  it dismisses that approval.
- A superseded run never re-blocks and never withdraws. Besides the check-run
  test (#1344), the deep append treats a newer Elder `APPROVED` or
  `CHANGES_REQUESTED` review on the head as superseding it, since an advisory
  check always concludes `neutral` and cannot show a newer run.

GitHub refuses a verdict review from a pull request's own author, so a pull
request the Grug app opened gets `COMMENT`.

## Consequences

- Gates can require Grug's `APPROVED` state on the head commit. A gate must
  match the review's `commit_id` to the head: between a push and the next
  review an older approval can still be standing where the ruleset does not
  dismiss stale reviews on push.
- A pull request with nothing to review (`no_diff`) gets no approval.
