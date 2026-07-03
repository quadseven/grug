# ADR-0009 — Synchronous Cave backend via the in-cluster spark-gateway

## Status

Accepted (2026-07-03). Amends [ADR-0005](0005-elder-cave-fallback.md) (the SQS
airlock): its "no third topology" premise is obsolete now that grug runs on the
operator's own cluster. Does NOT retire the airlock — the async fallback stays.
Enables the first synchronous flow (the exposed-secret judge, #439) and a
future Cave-primary backend.

## Context

ADR-0005 mandated the SQS airlock (Cave reachable only via `grug-cave-jobs` /
`grug-cave-results` queues) on a hard premise (ADR-0005:13):

> "the webhook Lambda runs in AWS and cannot reach a privately-networked
> resource unless either (a) the Lambda joins the operator's private network,
> or (b) something inside that network reaches out to AWS. **There is no third
> topology.**"

That premise is now FALSE. Grug's pods run on the operator's OKE cluster, and
the **spark-gateway** — an OpenAI/Anthropic-compatible gateway fronting the two
NVIDIA Sparks — is deployed **on the same cluster** (namespace `spark-gateway`,
`Service :8080`, verified live 2026-07-03; `https://sparks.ts.ehumps.me/v1/*`
over the tailnet). A cluster-internal Service call from the `grug` namespace to
the gateway is the "third topology" ADR-0005 said could not exist: it is
neither public exposure (the gateway has no public ingress) nor reach-in
networking (no bring-up on the request path, no new inbound to the Cave — the
gateway dials the Sparks; grug dials the gateway).

Why this matters:
- **Cost.** Elder's default backends are SaaS (Poolside / OpenRouter). Owned
  Sparks are free (`feedback_no_saas_llm_credits_owned_fallback`).
- **Privacy.** Every review + judge call ships the diff (and, for the judge,
  the raw file content that can quote a credential) to a third party — the
  reason #438/#439 redaction work exists. A synchronous Cave keeps the raw
  secret on owned infra; #439's exposed-secret judge becomes trivially private.
- **Benchmarkability.** The Cave becomes a first-class `Backend` measurable in
  the primary path, not just an async fallback.

## Decision

**Allow synchronous in-cluster calls from grug to the spark-gateway Service, as
long as the gateway is never publicly exposed** (cluster-internal / tailnet
only, no public ingress). Two consuming shapes, sequenced:

1. **First flow — the exposed-secret judge (#439).** Route the
   exposed-secret-class judge call to the Cave synchronously via the gateway,
   so the raw credential never reaches SaaS. Smallest, highest privacy payoff,
   already best-effort + single-backend (`judge_findings`), so a Cave timeout
   degrades exactly like today (returns `()`).

2. **Later — Cave as a first-class review Backend.** Add `CAVE` as the third
   `BackendConfig` in `llm_client` — the designed extension point ("adding a
   third backend is one new entry"), gated today by `assert len(Backend) == 2`
   in `select_backend`. Policy (Cave-primary with SaaS fallback? Cave for
   odd-installs?) is a follow-up slice with its own latency budget once the
   Sparks' tok/s under grug's prompt sizes is measured.

**Config (public-repo discipline).** The gateway base URL is a **private
tailnet address** and MUST arrive via SSM (e.g. `/grug/cave-gateway-url`),
never a literal in the repo — same rule as the benchmark's
`GRUG_BENCH_CAVE_URL`. The webhook pod already reaches the gateway (the
connector proves the network path); grug's pods need the tailnet sidecar/route
the connector uses, OR the gateway's in-cluster Service DNS name
(`spark-gateway.spark-gateway.svc`) if grug and the gateway share the cluster
(preferred — no tailnet hop, no secret URL).

**Keep the airlock (unchanged).** The async SQS fallback stays for: the
`all_failed` fallback burst-absorption, the rerun lane, and any future public
deployment where ADR-0005's Lambda premise returns. Synchronous is ADDITIVE.

## Consequences

### Positive
- Owned, free, private LLM for the judge (and later, reviews) with no diff
  egress to SaaS.
- Uses the in-cluster Service — no new inbound to the Cave, no public exposure,
  no reach-in bring-up. ADR-0005's security invariant ("never publicly
  exposed") is preserved.
- The Cave becomes benchmarkable in the primary path.

### Negative
- A synchronous Cave call is on the review/judge latency path. Bounded by a
  hard per-call timeout with SaaS/degrade fallback (the judge already returns
  `()` on failure; the review already falls to the next backend). The Sparks'
  throughput under grug's prompt sizes must be measured before Cave-primary.
- A second network dependency for the hot path (the gateway). Mitigated: it is
  in-cluster (same failure domain as the pod) and every synchronous use has a
  SaaS/degrade fallback.
- Cross-namespace reach: grug pods must be able to resolve/reach the
  `spark-gateway` Service (NetworkPolicy / tailnet). Infra follow-up.

### Reconsideration triggers
- grug is ever deployed to public SaaS AWS again (ADR-0005's Lambda premise
  returns) -> that deployment uses the airlock, not sync.
- The gateway grows a public ingress -> STOP; that violates the invariant this
  ADR preserves.
- Spark throughput can't meet the review latency budget -> keep Cave to the
  judge + async only, do not make it review-primary.

## References
- [ADR-0005](0005-elder-cave-fallback.md) — the airlock this amends
- #473 — the decision issue this ADR resolves
- #439 — the first synchronous flow (exposed-secret judge in-house)
- `spark_cave/` envelope (#454) — the async lane, unchanged
