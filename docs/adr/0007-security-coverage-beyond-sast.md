# ADR-0007 — Extending Elder's owned security coverage beyond SAST

## Status

Accepted (2026-06-17). Supersedes the per-PR DAST idea (#429). Builds on
[ADR-0006](0006-sast-detection-approach.md) (SAST). Follows from a
decision-critic review of "add DAST."

## Context

SAST shipped (ADR-0006): an OSS engine for recall + an LLM exploitability
judge for precision, advisory-by-default, reusing the existing Finding /
anti-hallucination / publish path. The next request was to add **DAST**
(Dynamic Application Security Testing) and reach a best-in-class bar.

A decision-critic pass falsified **per-PR DAST** on three independent axes:

- **No target pre-merge.** DAST is black-box — it needs a *running* target.
  Deploys happen on merge; at PR-review time the PR's changed code is not
  deployed anywhere, so a per-PR dynamic scan attacks the *previously
  deployed* code and is structurally blind to the diff under review.
- **No target at all for ~every repo.** Almost none of the repos Elder
  reviews spin a per-PR ephemeral environment with a reachable URL, so a
  per-PR dynamic scan has nothing to hit and produces zero findings.
- **The quality bar points elsewhere.** Best-in-class AI code review is set
  by *static* depth — whole-repo / cross-file reasoning and low false-positive
  precision over the diff — plus supply-chain coverage. Dynamic scanners are a
  separate category that runs in CD against deployed targets on a schedule,
  not as per-PR review.

Dynamic scanning also *actively attacks* a target, which adds an
authorization + blast-radius surface (what is Elder allowed to attack?) that
read-only review never had — fraught under the egress-restricted, public-repo,
owned-infra posture.

The reframe: the real goal is not "do DAST," it is **extend Elder's owned,
free security coverage to the classes pattern-SAST misses, at the cadence
where a target actually exists.** "DAST" was one technique floated for one
slice of that goal.

## Decision

**Do not build per-PR DAST.** Pursue owned security coverage on two tracks:

### Track 1 — per-PR depth (owned, free, no running target) — the priority

Extend Elder's per-PR review to the high-value classes pattern-SAST misses but
a leading reviewer catches, reusing the ADR-0006 engine-recall + LLM-judge
pattern and the existing publish path:

1. **Dependency-CVE scanning (SCA)** of the diff's manifest/lockfile changes —
   a known-vulnerable dependency introduced by the PR, judged for real
   reachability/impact. Highest ROI; an OSS scanner + the judge, no target.
2. **IaC + secret scanning** on the diff (misconfig, committed secrets).
3. **Cross-file / repo-graph static depth** — reasoning beyond the single diff
   hunk (the static-depth axis that defines best-in-class review).

Each is per-PR, needs no running target, honors the owned/no-SaaS-spend
posture, and flows the existing Finding / anti-hallucination / advisory-publish
contract — no parallel posting.

### Track 2 — real dynamic testing (deferred, correct cadence/target)

If dynamic testing is later wanted, run it **post-merge / scheduled against a
target the operator owns and is authorized to attack** (start with the hosted
app itself), with an OSS dynamic engine + LLM triage — **never per-PR**. This
gets its own ADR when prioritized; it is out of scope here.

Engine vendors stay config values behind the existing `scan_candidates`-style
boundary, vendor-neutral (per ADR-0006).

## Consequences

### Positive

- Coverage is added where it is actually *buildable and safe* (per-PR, no
  target, read-only-ish), and where it raises Elder to the best-in-class bar
  (static depth + supply-chain) rather than chasing a capability the bar's
  reference tools do not have.
- Reuses the shipped engine+judge pattern + publish path — small blast radius.
- Avoids shipping a per-PR feature that would produce zero findings for ~all
  repos and, where a deploy exists, test post-merge-old-code (noise that would
  erode the precision trust SAST earned).
- Sidesteps the active-attack authorization/blast-radius hole entirely for the
  per-PR path.

### Negative

- Defers true dynamic testing (accepted: it has no valid per-PR target; Track 2
  captures it at the right cadence).
- SCA + IaC/secret + repo-graph depth are each new engine integrations (effort),
  though each reuses the established pattern.

### Reconsideration triggers

- A repo (or the hosted app's own CI) gains per-PR ephemeral preview
  environments with reachable URLs → per-PR dynamic testing gets a real target;
  revisit.
- The operator wants dynamic coverage of the deployed app → open the Track 2
  ADR (post-merge/scheduled against the owned target).
- A future model makes whole-repo dynamic reasoning viable without a live
  target → revisit the frame.

## References

- [ADR-0006](0006-sast-detection-approach.md) — SAST (the engine+judge pattern
  Track 1 reuses)
- PRD #392 — Elder as an owned, free security reviewer (parent goal)
- #429 — the per-PR DAST idea this decision supersedes
- Decision-critic review (2026-06-17) — falsified per-PR DAST; produced this
  reframe
