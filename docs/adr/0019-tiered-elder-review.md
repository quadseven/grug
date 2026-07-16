# ADR-0019 - Tiered Elder review (single-arm default + selective deep)

## Status

Accepted (2026-07-15). Implements epic #645. Amends the production default
described in CONTEXT.md / specs/DESIGN.md for `GRUG_REVIEW_DEPTH` (was
always-on dual-arm `deep`).

## Context

Elder production review ran both owned Cave arms on every PR: the coder arm
and the reasoner arm, concurrent, findings merged. That design maximizes
recall when capacity is free, but under multi-tenant load it:

1. Holds **two** Cave slots for the wall-clock of the **slower** arm on every
   review.
2. Makes time-to-first-findings (and required check completion) wait on the
   reasoner even when the coder arm already returned usable findings.
3. Combines poorly with long-context prefill + reasoning traces on a runtime
   whose latency degrades roughly linearly with concurrency.

Multi-model operational review (2026-07) and live queue/TTFB symptoms agreed:
the bottleneck is **concurrency policy and arm fan-out**, not lack of silicon.
Competing review products feel fast because ordinary PRs get a single fast
pass and deep work is selective.

Constraints that stay load-bearing:

- Self-hosted Cave first; SaaS only as overflow/outage insurance
  (existing overload fallback).
- Guard (deterministic security suite) remains always-on and independent.
- Required GitHub checks must not be silently dropped (Apex in_progress +
  completion path).
- A previous always-hot reasoner path was chosen after a cold large-model
  load path timed out reviews; any future "premier" deep model must stay
  **resident** or keep the current reasoner as one-config rollback.

## Decision

**Production default is `GRUG_REVIEW_DEPTH=tiered`.**

| Mode | Behavior |
|---|---|
| `tiered` (default) | Always run Cave **coder**. Run Cave **reasoner** only when escalation fires. Merge when both run. |
| `deep` | Always both arms concurrent + merge (rollback / max recall). |
| `fast` | Coder first; reasoner only if coder fails (no sample deep). |

### Escalation triggers (v1, env-tunable)

Implemented in `llm_client.decide_deep_escalation`:

- **Diff size:** added lines (unified-diff `+` lines) >= `GRUG_DEEP_DIFF_LINES`
  (default 300).
- **High-risk paths:** changed path contains a marker from
  `GRUG_DEEP_PATH_MARKERS` (defaults cover auth/crypto/payment/secret/IaC).
- **Explicit request:** PR title or body matches `deep-review` /
  `deep_review` / `deep review` (label plumbing may replace this later).
- **Calibration sample:** deterministic hash of `(repo, pr_number, head_sha)`
  under `GRUG_DEEP_SAMPLE_RATE` (default 0.12) so retries do not flip-flop.

Escalation decisions log `llm_tiered_escalation` with reasons for DD.

### Quality floor without always-on dual-arm

- Guard persona remains the mechanical recall floor.
- Judge-gated publication remains the precision gate.
- Random deep sample supplies continuous dual-arm vs single-arm data.
- `elder_eval` / SAST benchmark remain promotion gates for model/runtime swaps.

### Serving follow-ons (out of scope for the policy cut, in-scope for #645 children)

- Replay harness on real long-context review prompts before claiming capacity.
- Optional vLLM (or other) serving for coder/reasoner only after baseline
  numbers; pin image digests.
- Any large "premier" deep model only with **always-hot** residency and
  eval within an agreed band of the current reasoner; keep current reasoner
  warm-loadable as rollback.
- Preferred later check semantics: required check completes on Tier 1;
  deep **appends** asynchronously (not yet required for this ADR).

### Rollback

Set `GRUG_REVIEW_DEPTH=deep` on webhook + consumer (both must match).

## Consequences

### Positive

- Ordinary PRs free the reasoner arm; queue wait and TTFB drop without new GPUs.
- Escalation + sample preserve a path to dual-arm recall where it matters.
- One env flip restores the previous dual-arm default.

### Negative / tradeoffs

- Some bugs only the reasoner would have caught on non-escalated PRs may
  miss until sample/escalation or human review. Mitigated by Guard, judge,
  sample rate, and eval monitoring.
- Explicit deep opt-in via title/body is weaker than a GitHub label until
  label plumbing lands.

### Neutral

- SaaS overload fallback unchanged.
- Prompt variant: tiered uses recall-oriented `v2` like deep; `fast` keeps
  the SSM experiment selector.
