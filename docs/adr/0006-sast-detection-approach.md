# ADR-0006 — Elder SAST-equivalent detection: OSS engine spine + LLM exploitability judge

## Status

Accepted (2026-06-16). Decides #398 for PRD #392. Build slices: benchmark harness #399, single-class tracer #400, full-class coverage #401.

## Context

GitHub Advanced Security (CodeQL SAST + secret scanning) is **free only on PUBLIC repos**. The operator's repos are mostly private, so they get no free taint/dataflow analysis. Grug Elder is the operator's own LLM PR reviewer (the GitHub App at grug.lol) and already runs on every allowlisted install, public and private. Making Elder a SAST-equivalent reviewer gives every repo that coverage for free and self-hosted.

The motivating failure is concrete and points at **recall, not precision**. On PR #391, CodeQL flagged a HIGH `py/clear-text-logging-sensitive-data`; Elder reviewed the SAME commit and returned `neutral` ("Grug find nothing"). Elder is an LLM reasoning reviewer, not a dataflow/taint engine, so it does not reliably catch the vuln classes SAST is built for. Conversely, that CodeQL finding was itself a substance false-positive (it logged an SSM param *name* — a config path already public in the k8s manifest — never a secret value), which is exactly the precision edge an LLM can exploit and a pure engine cannot.

So the goal is **SAST recall + LLM precision**: catch the taint/injection/secret classes deterministically, then suppress the non-exploitable flows with a stated reason.

Two facts about grug *today* (verified at decision time) shape the choice:

- **Elder already has the precision machinery.** The pipeline is `parse_diff → review_diff (LLM) → evaluate_diff → CodeReviewEvaluation`, with an **anti-hallucination filter** (`evaluate_diff` drops any `Finding` whose `(file, line)` is not inside the diff), an **LLM-as-judge** stage (`judge.py`, runs on the review span), and an **advisory/blocking publish** path (`code_reviewer_blocking` defaults advisory). An "exploitability judge" is an extension of an existing pattern, not a new mechanism.
- **The Lambda image-footprint objection is gone.** PRD #392 was drafted when grug ran as a Lambda (image-mode, where a heavy engine dependency hurt cold starts). Post-#354 grug runs as a **Kubernetes pod** (`python:3.13-slim`); an engine dependency is a one-time image pull with **zero per-review cost**. The main argument against bundling an OSS SAST engine no longer holds.

Options considered (the PRD's A / B / C):

- **A — pure-LLM ruleset.** Extend Elder's prompt/rule layer with explicit per-class detection + a required exploitability rationale. Zero new deps, reuses the whole pipeline, precision is Elder's strength. **Rejected as the spine:** recall depends entirely on the model — it does not *structurally* fix the #391 miss (it just asks the model to try harder), and model-dependent recall cannot be guaranteed or regression-tracked by the #399 benchmark. Kept as a *layer* (see Decision).
- **B — OSS engine + LLM judge.** A free, private-repo-friendly OSS SAST engine runs a deterministic first pass over the changed files; Elder judges each candidate for exploitability. Closes the recall gap structurally; precision via the existing judge pattern. **Chosen as the spine.**
- **C — both (B spine + A layer).** B for the classes the engine covers, A layered for classes the engine misses (logic/design, engine gaps). **Chosen, staged** — see Decision.

## Decision

**Elder gains SAST-equivalent detection as an OSS-engine spine plus an LLM exploitability judge, with a pure-LLM per-class layer on top — built in that order (Option C, staged B-first).**

### Pipeline

```
changed files @ head SHA
   │
   ├─ (1) OSS SAST engine scan ─────────► raw candidates (class, file, line, source→sink)
   │                                          │
   │        (2) Elder exploitability judge ◄──┘   keep / suppress + REASON
   │                                          │
   ├─ (3) pure-LLM per-class pass ─────────► additional findings (classes the engine misses)
   │                                          │
   └──────────────────────────────────────────┘
                       │
        existing contract: Finding → evaluate_diff anti-hallucination
        (drop findings whose (file,line) ∉ diff) → advisory/blocking publish
```

1. **Engine spine (recall).** A deterministic OSS SAST engine scans the changed files at the head SHA and emits normalized **candidates** (vuln class, file, line, a source→sink summary). Deterministic = benchmarkable + regression-trackable (#399).
2. **Exploitability judge (precision).** Each candidate is judged by Elder (LLM) for *real exploitability* and either kept or **suppressed with a stated reason** — extending the existing `judge.py` pattern. The precision bar is explicit: the #391 shape (logging a public config path / param name) MUST be suppressed with a reason. A kept candidate becomes a `Finding`.
3. **Pure-LLM per-class layer (coverage).** Elder's existing review prompt gains explicit per-class detection instructions + a required exploitability rationale, catching classes the engine's rules miss (and logic/design review, unchanged). Same `Finding` output.

All three sources converge on the **existing** `Finding` shape and flow through the **unchanged** anti-hallucination filter (`(file,line)` must map to the diff), dedup, and advisory-by-default publish (the `code_reviewer_blocking` toggle is honored). **No parallel posting mechanism** — findings render and gate exactly as Elder's do today.

### Engine choice + vendor neutrality

- **Engine: Semgrep OSS** (Apache-2.0; large community rule corpus; no per-repo licensing; runs fully self-hosted; private-repo-friendly). It covers the target classes: clear-text logging of secrets, hardcoded credentials/keys, SQL/command/template injection, SSRF, path traversal, unsafe deserialization, weak crypto, XXE/SSTI, secrets-in-source.
- **The engine is a config value, not an import** (per `standard-vendor-neutral-interfaces` + #398 AC2). The detection slice wraps the engine behind a `scan_candidates(changed_files) → tuple[Candidate, ...]` boundary so the vendor can be swapped without touching the judge, the `Finding` contract, or the publish path. `GRUG_SAST_ENGINE` (or equivalent config) selects the engine; Semgrep OSS is the default.

### Owned-LLM / no-SaaS compliance

- Semgrep OSS is **free and self-hosted** — no SaaS, no per-repo cost, no credits.
- The exploitability judge and the per-class layer use Elder's **existing** OpenRouter + Poolside backends (and the Cave fallback, ADR-0005) — no new LLM provider, no paid top-ups (honors `feedback_no_saas_llm_credits_owned_fallback`).

### Footprint + cost bounds (k8s pod)

- The engine dependency ships in the `grug-webhook` / `grug-consumer` pod image — a one-time pull, no per-review cost. It also rides into the `grug-poller` image (same image), unused there; acceptable.
- **Bound the scan:** cap scanned file count / total bytes per PR so a huge diff can't blow review latency or LLM spend (PRD user-story #16). The cap and its drop-logging are part of the detection slice's definition of done; a skipped-because-too-large scan must `log` what it dropped (never a silent truncation).

### Build sequence (so #399/#400/#401 need no further design)

1. **#399 benchmark harness first.** A committed, re-runnable corpus: one canonical sample per vuln class (clear-text secret log, hardcoded credential, SQLi, command injection, SSRF, path traversal, unsafe deser, weak crypto) PLUS the known false-positive shape (the #391 public-config-path log). It asserts Elder's recall on true-positives ≥ the engine's raw recall on the same input, and that the seeded FP is suppressed with a reason. This is the acceptance gate the detection slices build against.
2. **#400 single-class tracer.** One class (clear-text-secret-log) end-to-end through the full pipeline above (scan → judge → Finding → publish), proving the spine + judge + contract reuse on the smallest vertical slice.
3. **#401 full-class coverage.** The remaining classes, scored against the #399 harness.

## Consequences

### Positive

- **Closes the recall gap structurally** — the #391-class miss can't recur silently, because a deterministic engine catches it before the LLM ever weighs in.
- **Keeps Elder's precision edge** — the judge suppresses non-exploitable flows (e.g. the #391 public-config-path log) with a reason, the thing a pure engine cannot do.
- **Reuses the entire existing contract** — `Finding`, anti-hallucination, judge, advisory/blocking publish — so the blast radius is "add a scan + a judge," not "fork the review path."
- **Free + owned + portable** — no GitHub Advanced Security, no SaaS, works on private repos, engine is swappable.
- **Benchmarkable** — deterministic engine recall makes #399 a real regression gate, not a one-shot assertion.

### Negative

- **New dependency** (Semgrep OSS) in the shared pod image — larger image, and a subprocess/scan boundary on the review path. De-risked by the k8s-pod move (one-time pull) but real.
- **Two-to-three finding sources** (engine candidates + per-class LLM) must dedup cleanly into one `Finding` set — the detection slice owns that dedup.
- **Scan latency on large diffs** — mitigated by the file/byte cap, but the cap means very large PRs get bounded (not unbounded) coverage; the drop is logged, never silent.
- **Advisory-by-default** — SAST findings do not block merges until the operator flips `code_reviewer_blocking`; intentional (precision must earn trust first, PRD user-story #14) but means a real finding can ride in on an advisory verdict.

### Reconsideration triggers

- The engine dependency's image/latency footprint becomes painful on the pod (revisit a sidecar scan service, or a slimmer engine).
- Benchmark #399 shows Semgrep OSS recall materially below an alternative engine on the target classes (swap via the config-valued boundary — the whole point of keeping it vendor-neutral).
- The pure-LLM layer (step 3) proves to add no recall over the engine on the benchmark (drop it; fall back to Option B).
- A future model makes pure-LLM recall benchmark-competitive with the engine (revisit Option A as the spine to shed the dependency).

## References

- PRD #392 (parent) — Elder as self-hosted SAST-equivalent reviewer on every commit
- #398 (this decision) → unblocks #399 (benchmark), #400 (one-class tracer), #401 (full coverage)
- [ADR-0003](0003-activity-verdict-model.md) — verdict model the publish path reuses
- [ADR-0005](0005-elder-cave-fallback.md) — the owned LLM fallback the judge inherits
- Elder pipeline: `services/webhook/personas/code_reviewer/{dispatch,persona,judge,diff_parser}.py` (`evaluate_diff` anti-hallucination filter; `judge.py` LLM-as-judge)
- Motivating artifact: PR #391 (CodeQL HIGH Elder passed; the finding was itself a non-exploitable false-positive)
- #397 — every-commit review (the first #392 slice; ensures every head SHA reaches this pipeline)
