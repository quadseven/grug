# ADR-0001 — Mirror service-internal modules; defer extraction until rule-of-three triggers

## Status

Accepted (2026-05-17)

## Context

`grug` ships as two AWS Lambda functions that share the same platform primitives:

- **`services/webhook/`** — receives GitHub App events (PR opened/synchronized/reopened, etc.), runs the TPM persona's Definition-of-Ready static checks, posts a green/red check-run back to the PR.
- **`services/api/`** — backs `grug.lol` (React SPA): GitHub OAuth sign-in, list/toggle installations, admin allowlist UI.

Both Lambdas need the same building blocks:

| File | Purpose |
|---|---|
| `observability.py` | DD-extension-aware logger setup (`DD_SERVICE`, `DD_ENV`, log level) |
| `secrets_loader.py` | SSM SecureString reads at cold start (App ID, webhook secret) |
| `github_checks_client.py` | Thin wrapper over `POST /repos/{owner}/{repo}/check-runs` |
| `adapters/install_store.py` | Single-table DynamoDB schema (PK/SK layout, allowlist gate, install/repo-config CRUD) |
| `ports/token_cache.py` | `InMemoryTokenCache` for App JWT + installation tokens |
| `personas/tpm/dor_checks.py` | Static DoR rule definitions (why ≥30 words, acceptance, scope-fence, etc.) |

These six modules currently exist as **byte-identical copies** under `services/api/` and `services/webhook/`. Total: ~245 LOC duplicated across the pair.

A naive architecture-review pass reads "duplicate code → extract a shared module" and proposes a `services/_shared/` Python package. We considered this, then rejected it (for now) based on the constraints below.

## Decision

**Mirror these modules across `services/api/` and `services/webhook/`. Do NOT extract a shared package until a third Lambda materializes** (rule-of-three).

The pre-existing comment in `services/api/Dockerfile.lambda:1-4` already captured this stance informally:

> *"API Lambda container image — mirrors services/webhook/Dockerfile.lambda. Rule-of-three for extracting a shared grug-base image triggers once a 3rd Lambda lands (web UI ships static — probably never)."*

This ADR promotes that comment to a first-class decision record so future architecture reviews (human or AI) don't re-derive it from grep.

### Discipline that pairs with the deferral

To keep the mirroring honest, three guardrails:

1. **Each mirrored file carries a header**: `# MIRRORED from services/<other>/<file>.py — keep in sync. See ADR-0001.`
2. **CI gate**: `scripts/check-mirrored-files.sh` extended to fail any PR that modifies one half of a mirror pair without also modifying the other (or explicitly justifies the divergence in the commit message).
3. **Legitimate per-service divergence is rare and named** (e.g. logger namespace, env-var names). When it exists, it's a parameter or env read, not a hardcoded literal in one copy — and the CI gate's allowlist explicitly tolerates that file.

### Rule-of-three trigger conditions

The deferral lifts when either:

- A **third Python consumer** lands. Most likely shape is a 3rd Lambda (scheduled job, analytics worker, persona-runner), but any Python deploy target that imports the same primitives counts (e.g. a CLI tool, a local dev REPL, a separate Step Functions task).
- A **third persona** lands (per PRD #21 v1.5+ roadmap: code-reviewer, release-manager, stuck-PR-pulse). At that point three instances of the same persona shape provide enough signal to extract the right abstraction.

The React SPA at `web/` does **not** count toward the rule-of-three — it ships as static assets through Cloudflare Pages and does not consume Python modules.

## Consequences

### Positive

- **No premature abstraction lock-in.** v1 has one persona; the shape of a shared CheckDefinition / ResultPublisher / TokenedGitHubClient abstraction is currently guesswork. Sandi Metz: *"Duplication is far cheaper than the wrong abstraction."*
- **No Dockerfile / Pulumi packaging churn.** Each Lambda's image-mode build (`COPY . .` from service dir) stays simple. Adding a sibling shared dir would require Dockerfile restructure + Pulumi packaging changes — non-trivial deploy-risk surface for a solo-maintainer project.
- **Independent blast-radius.** Two physical Lambdas with separate IAM roles. A bug in shared code can't accidentally cross the api ↔ webhook permission boundary because there IS no shared code yet.
- **Cold-start isolation.** Each Lambda imports only what it needs. A shared package would need careful import discipline to avoid bloating init time for the smaller of the two. (Not currently measured — listed as a defensive consideration, not a quantified benefit.)

### Negative

- **245 LOC duplicated.** Every cross-cutting change (e.g. new logger formatter, new SSM secret name convention) is two edits.
- **Drift risk if discipline lapses.** A contributor edits one copy and forgets the other → behavior diverges silently. Mitigated by the CI gate + MIRRORED header (above), but mitigation is only as strong as the script's MIRRORED_FILES allowlist — a file omitted from that list can drift silently by design.
- **Cross-attribution bugs from copy-paste.** `services/api/secrets_loader.py:20` currently hardcodes `logging.getLogger("grug.webhook.secrets")` — wrong namespace in the api copy. Tracked separately (see issue #140); fix it without re-architecting.
- **Security-surface duplication.** Four of the six mirrored modules (`secrets_loader`, `install_store`, `token_cache`, `github_checks_client`) handle credentials, OAuth tokens, or external-API calls. A CVE in a vendor library, an SDK upgrade, or a security patch must be applied to TWO files in lockstep. The CI gate catches the lockstep failure mode, but it does NOT prioritize security-sensitive mirrors — a future improvement.

### Reconsideration triggers

Open the deferred re-evaluation issue (filed at PR-merge time) when:

- A 3rd Python consumer (Lambda or otherwise) is committed to a milestone (not just brainstormed).
- A 2nd persona ships and proves the shared-shape hypothesis (or contradicts it).
- The CI mirror-gate fails ≥3 times in a quarter — signals the duplication tax exceeds the extraction cost.
- Lambda cold-start time per service crosses a budget that shared init could amortize.
- A **security-relevant change** to any mirrored module — CVE in a vendor SDK, auth-flow patch, secret-handling change, IAM-policy diff. Lockstep editing under time pressure is the most error-prone shape; one-off extraction may be cheaper than risking drift on a security fix.
- A vendor library bump in one Lambda's `requirements.txt` for a module that imports from a mirrored file — divergence inside a mirror is the hardest failure to detect.

Until then: mirror with discipline.

## References

- `services/api/Dockerfile.lambda:1-4` — pre-existing informal version of this decision
- `services/api/secrets_loader.py:20` — cross-attribution bug born of copy-paste (issue #140)
- `services/api/ports/token_cache.py` — comparable one-adapter Protocol; collapse decision (issue #141)
- PRD #21 — v1.5+ roadmap (code-reviewer + release-manager + stuck-PR-pulse personas)
- Sandi Metz, *Practical Object-Oriented Design* — "wrong abstraction" cost argument
- somatic-scripts `services/pasto-api/` ↔ `services/tempo-api/` — sibling project applying the same mirror-with-discipline pattern at larger scale
