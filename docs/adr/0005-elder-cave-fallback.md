# ADR-0005 — Elder fallback to a self-hosted LLM ("the Cave") behind an SQS airlock

## Status

Accepted (2026-06-06). **Amends [ADR-0004](0004-rerun-via-sqs.md)** — specifically reverses its rejection of a "self-hosted worker over a private network" (see Context).

## Context

The 2026-06 degradation (`code_review_llm_degraded` monitor) recurred: **both** Elder LLM backends failed at once — OpenRouter `http_402` (out of credits) and Poolside `ReadTimeout` — so `review_diff` returned `all_failed`, Elder posted only a neutral "skipping" check, and the Activity row stayed `errored`. Reviews were silently dropped.

The root cause is structural: both backends are **external paid SaaS sharing a failure surface** (credits run dry, vendor timeouts). There is no *owned* fallback that cannot run out of credits. The operator runs a self-hosted LLM — **"the Cave"**, an OpenAI-compatible (Ollama) endpoint on the operator's own infrastructure — but it carries a **hard constraint: it must never be publicly exposed by any means.** It is reachable only over the operator's **private network**, not from the public internet.

That constraint collapses the design to one question: **who dials whom.** The webhook Lambda runs in AWS and cannot reach a privately-networked resource unless either (a) the Lambda *joins* the operator's private network, or (b) something *inside* that network reaches out to AWS. There is no third topology.

[ADR-0004](0004-rerun-via-sqs.md) rejected a "self-hosted worker over a private network" on the grounds that re-run work is I/O-bound, so the operator's compute "buys nothing." **That rationale does not hold here:** the worker buys the one thing AWS structurally cannot have — network reachability to a privately-networked LLM. So ADR-0004's rejection is scoped to the re-run case and is reversed for the fallback case here.

Options considered:

- **Refill credits / add a provider** — rejected as a *non-strategy*: the operator deliberately does not fund the SaaS backends, and adding another paid provider keeps the shared-failure-surface.
- **Add a cloud third backend (e.g. Bedrock)** — reliable and in-AWS, but still paid cloud (not the owned LLM), and not OpenAI-compatible, so it is a new call path rather than a `_BACKEND_CONFIGS` entry. Does not satisfy "use the hardware we own."
- **Reach-in: the Lambda joins the private network** — the webhook Lambda would tunnel into the operator's private network to dial the Cave directly. Rejected: it puts network bring-up on the *synchronous* review path, churns ephemeral nodes per cold start, and the 420s budget can't absorb a synchronous third tier (today's retry×fallback math is already 3×60 + 3×60 = 360s).
- **Reach-out: a worker on the private network pulls from a queue** — chosen.

## Decision

**Elder's owned fallback runs on the operator's private infrastructure and is fed by an SQS airlock; Grug and the Cave never connect to each other — they only ever touch the queues.**

- On `all_failed`, the webhook Lambda enqueues a fallback job to **`grug-cave-jobs`** (SQS FIFO) and keeps the `errored` verdict (honest per [ADR-0003](0003-activity-verdict-model.md)'s "no lies" rule — the fallback is eventual, not synchronous).
- A worker on the operator's private network — **`grug-cave-connector`** — long-polls `grug-cave-jobs`, runs the review against the Cave, and writes findings to **`grug-cave-results`** (SQS FIFO).
- The webhook Lambda consumes `grug-cave-results` (event-source mapping) and **publishes the check-run + heals the `CheckVerdictRecord`** (`errored` → `reviewed`), reusing the ADR-0003/0004 heal/upsert path. **GitHub App credentials never leave AWS** — the connector never touches GitHub.
- **Free-tier discipline:** FIFO (free dedup + per-install `MessageGroupId` ordering, as ADR-0004), **long polling mandatory** (`WaitTimeSeconds=20` — short polling would burn the 1M-request/month free tier), DLQ + Datadog monitors on queue age and DLQ depth.
- **The SQS queue is the airlock:** the Cave receives **no inbound connection from anywhere** (stronger than reach-in). The only new credential on the operator's network is SQS send/receive on these two queues.

Naming (per the project-wide rule): Grug's AWS resources are project-rooted `grug-cave-*`; the worker is identity-rooted `grug-cave-connector`. **No cluster name is embedded** — workloads migrate between clusters, so embedding the current cluster would force a rename on the next migration.

> The mechanics of *how* the connector reaches the Cave over the operator's private network are deployment-specific and live in the operator's private infrastructure repo, not here. This ADR is the public, deployment-agnostic architecture.

## Consequences

### Positive

- An owned fallback that **cannot run out of credits**; removes the both-clouds-down → zero-review failure mode.
- **The Cave has zero inbound** — the strongest possible reading of "never public." No tunnel into the Lambda, no public ingress.
- Reuses the SQS + heal machinery accepted in ADR-0004/0003 — the `errored` row simply heals when the fallback answers.
- The self-hosted box is **off the synchronous review path**; a slow/unavailable Cave never delays or blocks a review.
- The two queue names tell the whole story symmetrically (`grug → grug-cave-jobs → grug-cave-connector → grug-cave-results → grug`).

### Negative

- **Reverses ADR-0004's self-hosted-worker stance** (documented here so the reversal is intentional, not drift).
- New surface: two SQS FIFO queues + a DLQ, a new result-consumer event-source mapping on the webhook Lambda, and a new worker + a scoped AWS credential on the operator's network.
- The fallback is **eventual, not synchronous** — when both clouds fail, the verdict is `errored` until the connector heals it.
- When the Cave is **also** down (box offline, network down), the fallback cannot fire and the verdict stays `errored`. Accepted: "errored, heals when the Cave returns" is the agreed worst case.
- Liveness depends on the operator's cluster being able to reach the Cave; the AWS side ships independently behind a flag, and the connector deploy + flag flip are gated on that cluster being ready.

### Reconsideration triggers

- A *synchronous* fallback becomes required (revisit reach-in, or add Bedrock as an in-AWS synchronous tier).
- Cave availability drops enough that a second owned backend is warranted.
- A non-Grug project wants the same fallback — at that point the `grug-` root no longer fits and the queues should be re-rooted.
- The ADR-0004 future-consolidation (migrate the #272 Elder *offload* onto SQS) lands and these queues should fold into that topology.

## References

- PRD #309 (tracking parent) + slices #310–#313, #316
- [ADR-0004](0004-rerun-via-sqs.md) — re-run via SQS (amended: self-hosted-worker rejection reversed for the fallback case)
- [ADR-0003](0003-activity-verdict-model.md) — verdict model (`errored` rows + the heal/upsert path the fallback reuses)
- 2026-06 `code_review_llm_degraded` outage (OpenRouter 402 + Poolside ReadTimeout)
