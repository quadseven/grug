<p align="center">
  <img src="assets/grug-portrait.png" width="200" alt="Grug. Grumpy caveman. Holding club.">
</p>

<h1 align="center">Grug</h1>

<p align="center">
  <strong>Grug see bug. Grug crush bug. Grug guard code.</strong><br>
  One grumpy caveman. Whole software lifecycle.
</p>

<p align="center">
  <a href="https://github.com/apps/grug-tribe/installations/new"><img src="https://img.shields.io/badge/Install_Grug-fbbf24?style=for-the-badge&labelColor=181613" alt="Install Grug"></a>
  <a href="https://grug.lol"><img src="https://img.shields.io/badge/grug.lol-181613?style=for-the-badge" alt="grug.lol"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/AGPL--3.0-181613?style=for-the-badge" alt="AGPL-3.0"></a>
</p>

---

## What Grug do

Grug is a modular GitHub bot. Different **personas** across the SDLC — TPM today, with code-reviewer, release-manager, and stuck-PR-pulse personas planned.

Grug live in GitHub. Grug post Check Runs. Grug never spam comments. **You ship. Grug guard.**

## Install

### Hosted SaaS — recommended

1. Sign in at [grug.lol](https://grug.lol) with GitHub
2. Install the **Grug Boss** GitHub App on the repos you want gated
3. Toggle personas per-repo in the dashboard

That's it. Webhook is wired, check-runs post on every PR.

> **Note:** the hosted instance is **allowlist-gated** — request access
> from `@evan` if you're new. Self-host (below) is the open path.

### Self-host (run Grug in your own cave)

Grug is AGPL-3.0. Deploy your own instance against your own AWS account + Cloudflare account. Step-by-step in [`docs/SELF_HOST.md`](docs/SELF_HOST.md).

```
# roughly
aws ssm put-parameter ...   # pre-load App secrets
pulumi up                   # deploy the cave
# point GitHub App webhook → webhook.<your-domain>/webhook/github
# done. Grug guard now.
```

## What Grug checks (Definition of Ready)

Static checks on PR body — **4 blocking, 1 advisory:**

| | Check | Pass when | Blocks? |
|---|---|---|---|
| ✅ | `why` | Has `## Why` (or `## Summary`) section ≥5 words | **yes** |
| ✅ | `acceptance` | Has `## Acceptance criteria` (or `## Test plan`) with ≥3 bullets | **yes** |
| ✅ | `estimate` | Body or label includes `Size: XS/S/M/L` (XL must be split) | **yes** |
| ✅ | `scope-fence` | Has `## Out of scope` section | **yes** |
| ⚠️ | `issue-link` | Body links an issue via `closes #N` | advisory |

LLM scope review (advisory) — Poolside `laguna-m.1`:
- Title ↔ body match
- AC testability
- Scope creep flag
- XL inflation check

## What Grug NOT check

Grug is the **process gate**, not the code review gate.

- Code correctness — Sentry / Seer / DD PR Gates own that
- Test coverage — pytest gate owns that
- Security findings — DD/Sentry security scanners own that

## Pulse (scheduled)

Weekly issue-grooming sweep:
- Re-prefixes Grugboard items with `[<repo>]`
- Labels stale issues (>90d, idempotent, capped at 30/run)
- Posts iteration-metrics summary to a configured Discord/Slack channel

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    grug.lol                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  web/    │  │ webhook/ │  │     api/         │  │
│  │ React+   │  │ FastAPI  │  │  FastAPI Lambda  │  │
│  │ Vite SPA │  │ Lambda   │  │  (OAuth, /me,    │  │
│  │ CF Pages │  │ (HMAC →  │  │  /installations) │  │
│  │          │  │ persona  │  │                  │  │
│  │          │  │ dispatch)│  │                  │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
│                                                     │
│  ┌──────────────────────────────────────────────┐   │
│  │ infra/pulumi/                                │   │
│  │ AWS Lambda + DDB + KMS + CF DNS/Workers + DD │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

PRD #21 + slice issues #22-#34 track v1.

## Contributing

Issues + PRs welcome. Use the DoR template — Grug will gate your own PR. Fair is fair.

PR body must have `## Why`, `## Acceptance criteria`, `## Out of scope`, `Size:`, and `closes #N`.

## Related docs

| Doc | What inside |
|---|---|
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operations (first deploy, secret rotation, tear-down + rebuild) |
| [`docs/SELF_HOST.md`](docs/SELF_HOST.md) | Step-by-step self-host setup |
| [`docs/HITL_PREREQUISITES.md`](docs/HITL_PREREQUISITES.md) | One-time GitHub App registration walkthrough |
| [`CONTEXT.md`](CONTEXT.md) | Domain glossary (every term you'll see in the code) |
| [`docs/adr/`](docs/adr/) | Architecture decision records |

## License

[AGPL-3.0](LICENSE) — see [`docs/SELF_HOST.md`](docs/SELF_HOST.md) for network-service compliance notes if you self-host.

<p align="center">
  <sub>Grug not lawyer. Grug just guard.</sub>
</p>
