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
  to fold); every lock file was read; the code-only security pass on the
  sensitive paths finished, where one applies (#1780); and the run was not
  degraded.
- `REQUEST_CHANGES`: a blocking (high or critical) finding stands, new or
  carried forward.
- `COMMENT`: the run is degraded (any `degraded_reason`, `no_diff` included)
  or superseded. A degraded run never approves: fail closed. A degraded run
  with a blocking finding still posts `COMMENT`, as ADR-0023 decided.

**An unfinished security pass never approves** (#1780). When the diff touches
a sensitive path and the code-only security pass (grug#1569) failed, timed
out, or was skipped by a cancel, the run cannot vouch for the head: it posts
`COMMENT`, or `REQUEST_CHANGES` when a blocking finding stands, and its
summary says the security pass did not finish. The rule holds on the Tier-1
verdict, on the deep append's verdict, and at the deferred `APPROVE` itself.
The pass switched off by configuration is not an unfinished pass.

With nothing new to say inline, the `APPROVE` or `REQUEST_CHANGES` review is
posted body-only, so the state always lands.

**The blocking flag does not gate the state.** `code_reviewer_blocking` picks
only the check conclusion (`plan_publish`). The review state is posted in both
modes, because gates read the state. In particular Elder posts `APPROVE` on a
clean head in advisory mode too: that is intended by the operator decision,
not a leak of blocking behavior into advisory mode. The consequence is deliberate: a
`REQUEST_CHANGES` from Grug blocks GitHub's merge button (as it did in
blocking mode, #1581) on a repository in advisory mode too, until a clean
re-review (`/grug improve`) approves and withdraws it, or a maintainer
dismisses it. An `APPROVE` counts toward a ruleset's required approvals
only where that ruleset counts app reviews; the quadseven rulesets require
none, so there only gates that read the state count it. A degraded run
never requests changes, so a backend outage cannot block a merge this way.

**Withdrawals.** The newest Elder verdict is the standing one:

- A run that approves dismisses Elder's own `REQUEST_CHANGES` on the head
  it approves from runs that started earlier (as #1581 did for a `success`
  check, now in either mode). It never dismisses a block on another commit
  (#1781): that block may be a newer run's verdict on a newer push, and on
  an older commit Elder's newer `APPROVE` already replaces it as Elder's
  standing review state on GitHub.
- A re-review (`synchronize`, `reopened`, `review`, `rerun`) withdraws
  Elder's own earlier `APPROVE`s on the pull request before any early exit
  (stale input, `skip_prs`, carried verdict, degraded, re-run pending), so a
  run that cannot vouch for the head leaves no approval behind (#1781). A
  clean finish approves the head again. A re-review that posts `COMMENT`
  withdraws them once more at the end.
- **Never approve a head that is not current** (#1781). Right before Tier-1
  posts `APPROVE`, and right before the deferred `APPROVE`, Elder reads the
  pull request's head from GitHub. If a push moved it past the reviewed
  commit, or the lookup fails, the run posts no approval. Withdrawing
  approvals stands down when the head has moved: an approval on the new head
  belongs to a newer run.
- At the end of every deep append, clean or failed, cancelled or degraded,
  Elder withdraws its approvals on commits other than the reviewed head, so
  a late approval from an older run cannot survive (#1781).
- **An escalated head is approved only by the deep pass.** Tier-1 decides
  before it publishes whether a deep pass will follow on the head. If one
  will, Tier-1 does not approve: an `APPROVE` standing while the reasoner is
  still looking would let a gate merge before the deep pass reports a
  blocking finding. The deep append then posts `APPROVE` only when the
  combined review finishes clean (and withdraws Elder's own older blocks), or
  `REQUEST_CHANGES` on a blocking finding. If the deep pass fails, degrades,
  is cancelled, is superseded, or cannot confirm it is not superseded, the
  head gets no approval (fail closed). The deferred approval is posted once
  and never queued for retry, so a late approval cannot land over a newer
  verdict. Tier-1 still posts `REQUEST_CHANGES` at once when it finds a
  blocking problem itself.
- A superseded run never re-blocks and never withdraws. Besides the check-run
  test (#1344), the deep append treats a newer Elder `APPROVED` or
  `CHANGES_REQUESTED` review on the head as superseding it, since an advisory
  check always concludes `neutral` and cannot show a newer run.

**Recency is the run start, not the post time** (#1764). Two runs
can review one head at once, and a slow run that started first can post
after a faster run that started later. Every Elder review carries a run
marker (`<!-- grug:elder-run started=... -->`) naming when its run started:

- A verdict supersedes this run only if its marker shows a run that started
  after this one. A review with no marker never counts as later.
- A blocking deep pass never stands down for an earlier-started run's
  approval. It posts `REQUEST_CHANGES` and dismisses Elder's approvals from
  earlier-started runs, on this head and earlier commits, unless the head
  has moved past this run (#1781). An approval with
  no marker cannot prove its run, so a block dismisses it too.
- Right before posting a deferred `APPROVE`, the deep append re-checks the
  head. If the pull request's head is no longer the reviewed commit, the
  latest Elder check is not its own and started after this run
  started, or a later-started run has posted a verdict, or any lookup
  fails, it does not approve.
- A later-posted Elder check (#1344) still decides the check surface and
  refuses approvals, but it no longer silences a block.

GitHub refuses a verdict review from a pull request's own author, so a pull
request the Grug app opened gets `COMMENT`.

## Consequences

- Gates can require Grug's `APPROVED` state on the head commit. A gate must
  match the review's `commit_id` to the head: between a push and the start
  of the next review an older approval can still be standing where the
  ruleset does not dismiss stale reviews on push.
- A carried verdict (#1648) on a new head keeps its check but gets no
  `APPROVE`, since the re-review withdrew the old one before carrying.
- A pull request with nothing to review (`no_diff`) gets no approval.
