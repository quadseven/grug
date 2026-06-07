# ADR-0004 — Re-run failed Grug jobs via an SQS FIFO queue

## Status

Accepted (2026-06-06)

## Context

The 2026-06 Elder outage (both LLM backends failing) left Elder posting nothing for ~5 days across many PRs — and there was **no way to re-run** those reviews. Re-delivering the original webhook event is a no-op: the #272 async offload is idempotent on the delivery id (`claim_delivery`). The Activity feed (PRD #301) adds a **Re-run** action on `errored` rows (single + a "Re-run all errored" batch) to provide that missing backfill path.

Re-run must run the persona's LLM review, which needs the webhook Lambda's 420s budget — the api Lambda (where the dashboard button lives) has a 15s timeout, so it cannot run the review itself. It must hand off to async, durable, rate-limited execution.

Options considered for the hand-off/queue:

- **Fire-and-forget `lambda.invoke(Event)`** (what the #272 offload does today) — no durability/visibility; drops on throttle (the `elder_enqueue_failed` monitor exists precisely because of this). Bad for batch backfill.
- **Self-hosted worker over a private network** — the operator's own cluster. Splits the architecture across AWS↔private-network, adds a network dependency + failure mode, and buys nothing: re-run work is **I/O-bound** (waiting on the LLM HTTP call), not compute-bound, so the cluster's power sits idle.
- **Kafka / MSK** — built for high-throughput streaming/replay. Massive overkill for dozens of human-triggered jobs/month, at real cost (MSK $$) or ops burden (self-host).
- **SQS** — managed queue with native Lambda integration, DLQ, FIFO dedup, retry.

## Decision

**Re-run jobs go through an SQS FIFO queue (`grug-rerun-jobs`) + a DLQ, consumed by the webhook Lambda.**

- API endpoint `POST /installations/{id}/repos/{repo_id}/rerun` returns **202** and enqueues; a batch variant enqueues every current `errored` row.
- **FIFO + content-based dedup** on `(install, repo, pr, persona)` → a double-click within the 5-min window is dropped (free double-click guard).
- **`MessageGroupId = install_id`** → per-install serialization, so a batch backfill drains at a controlled rate instead of firing N concurrent LLM calls (protects rate limits + cost).
- **Consumer** = SQS event-source mapping → the webhook Lambda (reuses dispatch + GitHub + LLM clients and the 420s budget), batch size 1. Fetches the PR's **current** head, runs the single named persona, upserts the `CheckVerdictRecord` (heals the row).
- **DLQ** with redrive (`maxReceiveCount ~3`) + a Datadog monitor on DLQ depth, so a stuck re-run pages instead of vanishing.
- All Pulumi-managed (queue, DLQ, IAM: api → enqueue, webhook → consume).

## Consequences

### Positive

- The backfill path the outage proved was missing: one click recovers a failed review; "Re-run all errored" recovers a batch.
- Durable + retried + DLQ-backed — strictly better than the fire-and-forget offload.
- Free double-click guard and built-in rate control (FIFO group ordering).
- ~$0: SQS free tier is 1M requests/month (always-free); volume is dozens/month. Zero ops; stays in the all-AWS-serverless architecture.

### Negative

- Introduces SQS to grug (first queue) — new infra + IAM + a DLQ monitor to own.
- FIFO per-install serialization makes a large batch drain slowly (acceptable — it's the rate control we want).
- A new api → webhook coupling (api enqueues work the webhook runs).

### Reconsideration triggers

- Re-run volume grows enough that per-install FIFO serialization is too slow (move to a standard queue + explicit concurrency cap).
- The #272 Elder *offload* is migrated onto the same queue (kills the drop-on-throttle failure mode) — a noted future consolidation.

## References

- PRD #301 (Activity feed backend + re-run)
- ADR-0003 (verdict model — `errored` rows are what get a re-run button)
- `CONTEXT.md` — `Re-run`
- 2026-06 Elder outage (root cause + the missing-backfill gap that motivated this)
