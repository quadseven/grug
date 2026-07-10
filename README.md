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

Grug is a modular GitHub bot with focused **personas** across the SDLC. Four
are enabled by default:

- **Chief** (`Grug - Definition of Ready`) - static PR-readiness checks and
  branch-ruleset self-heal.
- **Elder** (`Grug - Code Review`) - durable, deep LLM diff review with
  whole-file, cross-file, PR-intent, and runtime context.
- **Guard** (`Grug - Guard`) - Semgrep, dependency-CVE, secret, and IaC
  detection filtered through an exploitability judge.
- **Teller** (`Grug - Teller`) - an updatable PR walkthrough with file and
  effort summaries.

**Smasher**, **Warder**, and **Pulse** are wired as opt-in tracers. Omen runtime
signal already augments Elder where a repository-to-service mapping exists.

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
pulumi up                   # provision AWS infra (SSM refs, SQS, KMS, DD monitors)
# provision Postgres (grug_kv on CNPG) + stand up a Cloudflare tunnel
# push to main → deploy.k8s.yml builds the images + applies k8s/ to your cluster
# point GitHub App webhook → webhook.<your-domain>/webhook/github
# done. Grug guard now.
```

## What Chief checks (Definition of Ready)

Static checks on PR body — **4 blocking, 1 advisory:**

| | Check | Pass when | Blocks? |
|---|---|---|---|
| ✅ | `why` | Has `## Why` (or `## Summary`) section ≥5 words | **yes** |
| ✅ | `acceptance` | Has `## Acceptance criteria` (or `## Test plan`) with ≥3 bullets | **yes** |
| ✅ | `estimate` | Body or label includes `Size: XS/S/M/L` (XL must be split) | **yes** |
| ✅ | `scope-fence` | Has `## Out of scope` section | **yes** |
| ⚠️ | `issue-link` | Body links an issue via `closes #N` | advisory |

## What Elder and Guard check

### Elder: deep code review

Every PR snapshot, after its base, head, title, and body remain stable for the
quiet window:

- **LLM review** - after a durable 90-second quiet-snapshot window, the default
  deep pass sends whole-file context plus untrusted PR intent to both Poolside
  and OpenRouter (review-only Opus 4.7 with high-effort reasoning), merges
  deduplicated findings, and preserves backend/model provenance. It covers 25+
  named correctness, error-handling, concurrency, and security-shape rules;
  `GRUG_REVIEW_DEPTH=fast` is the one-primary-plus-fallback rollback. A
  self-hosted fallback remains behind an SQS airlock when both clouds fail
  (ADR-0005). If only one deep backend succeeds, its findings are published as
  provisional advice and the durable job retries instead of recording a clean
  completed review.
- **Feedback learning** - judge labels and write-authorized maintainer reactions
  retain per-model provenance. Confirmed findings refresh positive practices and
  few-shot examples; false positives become bounded AVOID guidance for later
  reviews. Reactions from users without write access cannot steer the prompt.

### Guard: deterministic security review

- **SAST** — Semgrep OSS over vendored offline rules + a zero-dep builtin
  detector (sql/command/template injection, path traversal, SSRF, unsafe
  deserialization, weak crypto, cleartext secret logging).
- **SCA** — dependency-CVE scan of manifest/lockfile changes via the OSV API.
- **Secret scanning** — provider-token patterns + entropy-gated generic rule
  on added lines of ANY file type; values are masked, never echoed.
- **IaC scanning** — Terraform / Kubernetes YAML / Dockerfile misconfigs
  (open 0.0.0.0/0, privileged pods, public ACLs, root containers).
- **Exploitability judge** — an LLM precision layer that grades every
  security candidate real-vs-noise before it reaches your PR; recall +
  precision are tracked against a committed benchmark corpus.

Advisory-first: Elder and Guard findings post as `neutral` until their separate
per-repo blocking flags are enabled.

## Roadmap (epic #464)

- expand Smasher beyond its mutation-testing tracer
- add deploy gating to Warder's changelog and semver analysis
- deepen Pulse's scheduled project-health sweep
- add title/body scope review to Chief

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    grug.lol                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐      │
│  │  web/    │  │ webhook/ │  │     api/         │      │
│  │ React+   │  │ FastAPI  │  │  FastAPI         │      │
│  │ Vite SPA │  │ pod      │  │  (OAuth, /me,    │      │
│  │ CF Pages │  │ (HMAC →  │  │  /installations) │      │
│  │          │  │ persona  │  │   pod            │      │
│  │          │  │ dispatch)│  │                  │      │
│  └──────────┘  └──────────┘  └──────────────────┘      │
│       webhook + api on Kubernetes (OKE),               │
│       behind a Cloudflare tunnel; + grug-consumer       │
│       (SQS) and grug-poller job    │
│                                                        │
│  ┌──────────────────────────────────────────────┐     │
│  │ infra/pulumi/  +  k8s/                        │     │
│  │ Postgres (CNPG) + SQS + KMS + SSM + CF + DD   │     │
│  └──────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────┘
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
| [`docs/NETWORK-TOPOLOGY.md`](docs/NETWORK-TOPOLOGY.md) | k8s/tunnel/CNPG/SQS topology, request flows, trust boundaries |
| [`CONTEXT.md`](CONTEXT.md) | Domain glossary (every term you'll see in the code) |
| [`docs/adr/`](docs/adr/) | Architecture decision records |

## License

[AGPL-3.0](LICENSE) — see [`docs/SELF_HOST.md`](docs/SELF_HOST.md) for network-service compliance notes if you self-host.

<p align="center">
  <sub>Grug not lawyer. Grug just guard.</sub>
</p>
