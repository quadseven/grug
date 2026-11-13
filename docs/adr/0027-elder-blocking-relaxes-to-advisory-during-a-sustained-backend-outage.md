# ADR-0027 - Elder blocking relaxes to advisory during a sustained backend outage

## Status

Accepted (2026-11-13). Decided by the operator the same day. Amends
ADR-0023 (a degraded Elder review fails closed in BLOCK mode): the rule
stays, with one bounded exception. Guard and Smasher are unchanged.

## Context

ADR-0023 made a degraded Elder review conclude `failure` in BLOCK mode, so
a required `Grug - Elder` check never passes with no review behind it. Its
escape for a long outage was manual: the operator drops the repo to WARN in
the dashboard, then back to BLOCK.

On 2026-11-13 that escape was too slow and too narrow. The owned fallback
(the Spark hosts) had been offline for weeks and the subscription lane (a
laptop) was asleep, so Elder fell to a single hosted model and every review
came back degraded: `WARN Elder review low confidence` and partial reviews.
Every blocking repo froze for hours, including a time-critical release roll,
and nobody could review anything because no backend was able to.

The operator's decision: do not block while the review backends are down,
and restore blocking by itself when they are back.

## Decision

In BLOCK mode a degraded review still fails closed (ADR-0023), unless it is
part of a **sustained backend outage**. Then Elder publishes an advisory
instead.

- **Classify every Elder result** (`personas/code_reviewer/outage_advisory.py`):
  - `healthy`: a real review, no degraded reason.
  - `outage`: `all_failed`, `parse_failed`, `subscription_limit_reached`,
    `low_confidence_review`, or `partial_review` where a cohort ran and
    failed.
  - `other`: everything else, such as a `partial_review` caused only by
    budget or the token cap (a big diff), and `no_diff`.
- **One streak per install** of consecutive `outage` results. A `healthy`
  result ends it. An `other` result neither extends nor ends it. No `outage`
  result for the gap (default 60 minutes) ends it. It is stored in
  `shared_benches` (the store Elder already uses for its backend benches and
  breakers; kind `outage`) when `GRUG_SHARED_BENCHES` is on, and in process
  memory otherwise.
- **Advisory** when the review is a BLOCK-mode `outage` result and the streak
  is at least the window old (default 20 minutes). The check concludes
  `neutral` (GitHub counts that as passing), is titled
  `Elder advisory: review backends down`, and its summary says which backends
  failed and since when. The first result of a streak, and every result before
  the window, still fails closed.
- **Restores itself.** A real review ends the streak, so the very next degraded
  review fails closed as before. Nothing for a person to do.
- **Re-evaluates** on the next push or `/grug improve`, which run a fresh
  review through the same path.
- A degraded review that is not an outage (a genuinely partial review of a huge
  PR with healthy backends) keeps failing closed exactly as ADR-0023 says.
- **Honest merge gate.** The advisory is never titled `Elder clear`, and its
  summary opens with the `## Elder advisory: review backends down` heading and
  states that nothing was reviewed. A person or script that wants a real review
  matches on the title. The fleet merge gate rule (`Grug - Elder` success on the
  head; neutral is not enough) is unchanged, so an agent that follows it still
  waits, and a human may merge on the advisory knowingly.

Settings (per deployment, environment):

| Name | Default | Meaning |
|---|---|---|
| `GRUG_ELDER_OUTAGE_ADVISORY` | on | `0`, `false`, `no` or `off` restores ADR-0023 exactly. |
| `GRUG_ELDER_OUTAGE_MINUTES` | `20` | Consecutive degraded time before the advisory. |
| `GRUG_ELDER_OUTAGE_GAP_MINUTES` | `60` | No outage result for this long ends the streak. |

Not covered, by design: the terminal paths that complete a check without a
fresh review (a diff that could not be fetched, a dead-lettered job, a
GitHub freshness outage). They still fail closed per ADR-0023, because they
are not evidence about the review backends. The deep append reads the streak
but never extends it. WARN mode and an unreadable mode are unchanged.

## Reasoning

A required check should mean a review happened, and a long outage should not
freeze every merge. The two are reconciled by time: a short blip still
blocks, so a PR cannot merge unreviewed because one call failed; an outage
that has outlasted the window is a platform problem the author cannot fix.

The streak uses results, not breaker state, as its evidence. A host that is
simply offline answers with errors and never opens a breaker, which is exactly
what hid this outage from a health check built on `_backend_usable`. Failed
backends are still named in the summary from breakers and the models the review
tried.

## Consequences

- During an outage, a PR can merge with a neutral Elder and no review. The
  advisory title and summary say so, and the check turns blocking again by
  itself.
- A PR that is red from before the window stays red until its next push or
  `/grug improve`.
- The streak is install-wide. A single huge PR that keeps losing cohorts cannot
  open it alone while other reviews succeed, because each success ends it.
- A misclassified `low_confidence_review` (a short empty answer that was in
  fact clean) counts as an outage result. It needs the whole window of
  consecutive degraded results to matter.

## Reconsider when

- A merge lands on an advisory and a defect that Elder would have caught ships.
- The streak opens while the backends were healthy (a false outage). The
  classifier in `classify` then needs the failed-model evidence, not only the
  reason.
- The backends gain a reliable health probe, which could replace the streak
  with a direct "no usable backend" test.
- The window needs to differ per repo, which would move the setting into
  `RepoConfig`.
