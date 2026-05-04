# Grug

Modular GitHub bot. Different personas across the SDLC — TPM today
(Definition-of-Ready check + scheduled iteration pulse), with
code-reviewer, release-manager, and stuck-PR-pulse personas planned.

## Install

### Hosted SaaS — recommended

1. Sign in at https://grug.lol with GitHub
2. Install the **Grug Boss** GitHub App on the repos you want gated
3. Toggle personas per-repo in the dashboard

That's it. Webhook is wired, check-runs post on every PR.

> Note: the hosted instance is **allowlist-gated** — request access
> from `@evan` if you're new. Self-host (below) is the open path.

### Self-host

Grug is AGPL-3.0. Deploy your own instance against your own AWS account
+ Cloudflare account. Step-by-step in [`docs/SELF_HOST.md`](docs/SELF_HOST.md).

Roughly: pre-load App secrets into AWS SSM → `pulumi up` → point your
GitHub App webhook URL at `webhook.<your-domain>/webhook/github`.

## What Grug checks (Definition of Ready)

Static checks on PR body — all blocking when `strict: true`:

| Check | Pass when |
|---|---|
| `why` | Has `## Why` (or `## Summary`) section ≥5 words |
| `acceptance` | Has `## Acceptance criteria` (or `## Test plan`) with ≥3 bullets |
| `estimate` | Body or label includes `Size: XS/S/M/L` (XL must be split) |
| `scope-fence` | Has `## Out of scope` (advisory; warning only) |
| `issue-link` | Body links an issue via `closes #N` (advisory) |

LLM scope review (advisory) — Poolside `laguna-m.1`:
- Title ↔ body match
- AC testability
- Scope creep flag
- XL inflation check

## What Grug does NOT check

- Code correctness — Sentry / Seer / DD PR Gates own that
- Test coverage — pytest gate owns that
- Security findings — DD/Sentry security scanners own that

Grug is the **process gate**, not the **code review gate**.

## Pulse (scheduled)

Weekly issue-grooming sweep:
- Re-prefixes Grugboard items with `[<repo>]`
- Labels stale issues (>90d, idempotent, capped at 30/run)
- Posts iteration-metrics summary to a configured Discord/Slack channel

## Architecture (SaaS)

| Component | What |
|---|---|
| `services/webhook/` | FastAPI Lambda receiving GitHub webhooks → HMAC verify → persona dispatch |
| `services/api/` | FastAPI Lambda backing the dashboard (OAuth, /me, /installations, /admin) |
| `web/` | React + Vite SPA on Cloudflare Pages |
| `infra/pulumi/` | Account-agnostic IaC (AWS Lambda + DDB + KMS + Cloudflare DNS/Workers + Datadog monitors) |
| `services/api/personas/tpm/` | TPM persona (DoR check + Poolside LLM scope review + Checks API post) |

PRD #21 + slice issues #22-#34 track v1.

## License

[AGPL-3.0](LICENSE) — see [`docs/SELF_HOST.md`](docs/SELF_HOST.md) for
network-service compliance notes if you self-host.

## Contributing

Issues + PRs welcome. Use the DoR template (PR body must have
`## Why`, `## Acceptance criteria`, `Size:`, `closes #N`) — Grug will
gate your own PR.

## Related docs

- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — operations (first deploy, secret rotation, tear-down + rebuild)
- [`docs/SELF_HOST.md`](docs/SELF_HOST.md) — step-by-step self-host setup
- [`docs/HITL_PREREQUISITES.md`](docs/HITL_PREREQUISITES.md) — one-time GitHub App registration walkthrough
