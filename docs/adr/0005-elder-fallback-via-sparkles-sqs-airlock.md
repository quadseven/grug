# ADR-0005 — Elder fallback to a self-hosted LLM behind an SQS airlock

## Status

Accepted (2026-06-06). **Amends [ADR-0004](0004-rerun-via-sqs.md)** — specifically reverses its rejection of a "self-hosted k8s worker over Tailscale" (see Context).

## Context

The 2026-06 degradation (`code_review_llm_degraded` monitor, P2) recurred: **both** Elder LLM backends failed at once — OpenRouter `http_402` (out of credits) and Poolside `ReadTimeout` — so `review_diff` returned `all_failed`, Elder posted only a neutral "skipping" check, and the Activity row stayed `errored`. Reviews were silently dropped.

Root cause is structural, not a one-off: both backends are **external paid SaaS sharing a failure surface** (credits run dry, vendor timeouts). There is no *owned* fallback that cannot run out of credits. The operator owns a strong LLM host — `srv-sparkles` (Ollama, 100.78.49.57:11434) — but it carries a **hard constraint: it must never be publicly exposed by any means.** It is a cross-tailnet *shared* peer, visible only to the operator's **user-account** Tailscale identity (a `tag:*` device can `ping` it but TCP-connects time out — see the egress-proxy pattern).

That constraint collapses the design to one question: **who dials whom.** The webhook Lambda runs in AWS and cannot reach a tailnet-private resource unless either (a) the Lambda *joins* the tailnet, or (b) something *inside* the tailnet reaches out to AWS. There is no third topology.

[ADR-0004](0004-rerun-via-sqs.md) rejected a "self-hosted k8s worker over Tailscale" on the grounds that re-run work is I/O-bound, so the cluster's compute "buys nothing." **That rationale does not hold here:** the cluster buys the one thing AWS structurally cannot have — network reachability to a tailnet-private LLM. So ADR-0004's rejection is scoped to the re-run case and is reversed for the fallback case here.

Options considered:

- **Refill credits / add a provider** — necessary first aid (and done separately, plus a credit-balance monitor), but does not remove the shared-failure-surface; both clouds can still die together.
- **Add a cloud third backend (e.g. Bedrock)** — reliable and in-AWS, but still paid cloud (not the owned GPU), and not OpenAI-compatible, so it is a new call path rather than a `_BACKEND_CONFIGS` entry. Left open as a *possible additional* tier; does not satisfy "use the hardware we own."
- **Reach-in: `tailscaled` in the Lambda** — userspace `tailscaled` + SOCKS5 baked into the webhook image (image-mode Lambdas can't attach layers), Lambda joins the tailnet, dials sparkles. Rejected: puts cold-start tailnet bring-up on the *synchronous* review path; spawns an ephemeral tailnet node per cold start (zombie churn); the 420s budget can't absorb a synchronous third tier (today's retry×fallback math is already 3×60 + 3×60 = 360s); and it places a residential-WAN home box on the sync path.
- **Reach-out: tailnet worker pulls from a queue** — chosen.

## Decision

**Elder's owned fallback runs in the tailnet and is fed by an SQS airlock; Grug and sparkles never connect to each other — they only ever touch the queues.**

- On `all_failed`, the webhook Lambda enqueues a fallback job to **`grug-sparkles-jobs`** (SQS FIFO) and posts/keeps the `errored` verdict (honest per [ADR-0003](0003-activity-verdict-model.md)'s "no lies" rule — the fallback is eventual, not synchronous).
- A cluster workload **`sparkles-grug-connector`** long-polls `grug-sparkles-jobs`, runs the review against `srv-sparkles` Ollama, and writes findings to **`grug-sparkles-results`** (SQS FIFO).
- The webhook Lambda consumes `grug-sparkles-results` (event-source mapping) and **publishes the check-run + heals the `CheckVerdictRecord`** (`errored` → `reviewed`), reusing the ADR-0003/0004 heal/upsert path. **GitHub App credentials never leave AWS** — the connector never touches GitHub.
- **Group, not Fuse:** the connector is a *separate* pod that reaches sparkles *through* the existing single-purpose `sparkles-egress` relay (which holds the high-value user-account auth-key). The connector parses external input (diffs, queue messages) but is isolated from the credentialed relay's blast radius.
- **Free-tier discipline:** FIFO (free dedup + per-install `MessageGroupId` ordering, as ADR-0004), **long polling mandatory** (`WaitTimeSeconds=20` — short polling would burn the 1M-request/month free tier), DLQ + Datadog monitors on queue age and DLQ depth.
- **The SQS queue is the airlock:** sparkles/Ollama receives **no inbound connection from anywhere** (stronger than reach-in). The only new credential on the cluster is SQS send/receive on these two queues.

Naming (per the project-wide rule recorded with this work): Grug's AWS resources are project-rooted `grug-sparkles-*`; the cluster workload is identity-rooted `sparkles-grug-connector`. **No cluster name is embedded** — workloads migrate between clusters (the worker targets `k8s-oke` but `k8s-ts` is being retired), so embedding `k8s-oke`/`k8s-ts` would force a rename on the next migration, exactly the tax `sparkles-egress` avoids today.

## Consequences

### Positive

- An owned fallback that **cannot run out of credits**; removes the both-clouds-down → zero-review failure mode.
- **Ollama has zero inbound** — the strongest possible reading of "never public." No tailscaled-in-Lambda, no public ingress, no Funnel.
- Reuses the SQS + heal machinery accepted in ADR-0004/0003 — the `errored` row simply heals when the fallback answers.
- The flaky residential-WAN home box is **off the synchronous review path**; a slow/unavailable sparkles never delays or blocks a review.
- The two queue names tell the whole story symmetrically (`grug → grug-sparkles-jobs → sparkles-grug-connector → grug-sparkles-results → grug`).

### Negative

- **Reverses ADR-0004's tailnet-worker stance** (documented here so the reversal is intentional, not drift).
- New surface: two SQS FIFO queues + a DLQ, a new result-consumer event-source mapping on the webhook Lambda, and a new cluster workload + a scoped AWS credential in the cluster.
- The fallback is **eventual, not synchronous** — when both clouds fail, the verdict is `errored` until the connector heals it.
- When sparkles is **also** down (DGX off, home WAN down, cross-tenant share revoked), the fallback cannot fire and the verdict stays `errored`. Accepted: "errored, heals when sparkles returns" is the agreed worst case.
- Liveness depends on the OKE migration: the connector needs a `k8s-oke` node that can reach sparkles. The AWS side ships independently behind a flag; the connector deploy + flag flip wait on `k8s-oke` readiness.

### Reconsideration triggers

- A *synchronous* fallback becomes required (revisit reach-in, or add Bedrock as an in-AWS synchronous tier).
- `srv-sparkles` availability drops enough that a second owned backend is warranted (today the "sparkles only, no load-balancing" policy holds).
- A non-Grug project wants the same fallback — at that point the `grug-` root no longer fits and the queues should be re-rooted.
- The ADR-0004 future-consolidation (migrate the #272 Elder *offload* onto SQS) lands and these queues should fold into that topology.

## References

- PRD #309 (tracking parent) + slices #310–#313
- [ADR-0004](0004-rerun-via-sqs.md) — re-run via SQS (amended: tailnet-worker rejection reversed for the fallback case)
- [ADR-0003](0003-activity-verdict-model.md) — verdict model (`errored` rows + the heal/upsert path the fallback reuses)
- `CONTEXT.md` — `Elder`, `Verdict`, `Re-run` (the `grug-sparkles-*` + `sparkles-grug-connector` terms land in CONTEXT.md in the implementation PR, per its same-PR discipline)
- The cross-tailnet sparkles egress-proxy pattern (user-account-only visibility; `sparkles-egress` relay)
- 2026-06 `code_review_llm_degraded` outage (OpenRouter 402 + Poolside ReadTimeout)
