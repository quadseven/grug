# ADR-0025 - Durable retry for a failed deep-review publish

## Status

Accepted (2026-10-25). Implements issue #738.

## Context

The async deep review (ADR-0019, #730) runs the reasoner model after the
Tier-1 review has published, then posts its novel findings with
`post_review`. When that POST failed on the wire, the deep append logged
`elder_async_deep_review_publish_failed` and returned. The model output,
the captured inline-comment context and the stack refresh were gone, and
nothing would bring them back: the deep append runs after the required
check has published, and by design its failures never change the persona
result, so no redrive follows.

The synchronous Tier-1 path is in a different position. A transient
review-publish failure makes the persona result `publish_failed`, which
`rerun.py` turns into a raise, so SQS redrives the whole job (up to its
DLQ). That redrive is already durable across process restarts. A
permanent 4xx is classified out of it (#770).

## Decision

1. Scope: deep path only. The synchronous path keeps its SQS redrive. It
   is already durable, it re-reads the pull request on redrive (so a moved
   head is reviewed fresh, not republished stale), and it re-runs only the
   Tier-1 model. Moving it onto this outbox would duplicate a working
   recovery and change its check-run semantics. The deep path is the one
   with no recovery and the more expensive model call. This is a decision,
   not a default: if the Tier-1 re-run cost ever matters, the same outbox
   can take the synchronous review too.
2. Idempotency key. Before the first POST, the deep append derives a key:
   the first 32 hex characters of SHA-256 over the installation id, the
   repository, the pull number and the full review payload (commit, event,
   body, every inline comment). The key is appended to the review body as
   a hidden marker, `<!-- grug-publish:KEY -->`. GitHub's reviews API takes
   no idempotency key, so the marker is the key's server-side record.
3. Double-post guard. Before any retry POST, the retry lists the pull
   request's reviews. A review carrying the marker is the original attempt
   that landed on GitHub while its response was lost. The retry adopts
   that review's id and posts nothing. The same check covers a retry that
   posted and then died before marking its row done.
4. Durable state. A transient failure (transport, 5xx, 429, a throttling
   403) stores a `PendingPublication` in `grug_kv` under pk
   `PUBLISH_RETRY`, sk = the key. The row holds the review payload with
   its marker, the posted findings and span context for comment capture,
   and the stack comment body, which is built before the POST. A permanent
   4xx is not stored, for the reason given in #770. Enqueue is
   `INSERT ... ON CONFLICT DO NOTHING`, so the same publication queues
   once.
5. Retry worker. The `grug-poller` CronJob (every ~15 min) runs
   `retry_due_publications`. Each due row is claimed under a 300 s lease by
   a single compare-and-set statement, so two pollers never work one row.
   On success the row is deleted. On failure it stays `pending` with
   `attempts + 1` and waits `min(60 * 2^attempts, 3600)` s. After 8 failed
   retries it is parked as `abandoned` (an error log line) and kept until
   its 7-day TTL for the operator to read.
6. Staleness. A retry is dropped (`superseded`) when the pull request is
   no longer open or its head moved: a newer review owns that head, and a
   late stack refresh would overwrite its projection with an older one.
7. After a delivered (or adopted) review, the retry runs comment capture
   and the stack refresh once, as the original path would have. Capture is
   still best-effort and is not retried by itself (out of scope). The
   Hunt Board record of the deep pass is not replayed: it needs the model
   response, which the row does not carry.

## Consequences

- A GitHub brownout during a deep publish delays the deep findings by up
  to a few poller ticks instead of losing them.
- Every deep review body carries one hidden marker line.
- An adopted review gets its comments captured again; the comment record
  upsert makes that a rewrite of the same rows.
- New log events: `elder_deep_publish_retry_queued`, `_failed`,
  `_delivered`, `_superseded`, `_rejected`, `_abandoned`,
  `_queue_failed`, `_sweep`. The poller summary gains
  `deep_publish_retried` and `deep_publish_retry_failed`.
