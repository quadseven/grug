# grug

A GitHub App that reviews every pull request: plan, code and security.

Grug checks that a PR says why it exists, what done looks like and how big
it is, reviews the code with an LLM, and scans it for security problems. It
answers with check runs and inline review comments, not a pile of chat
comments. It is for teams and agent fleets that want every PR held to the
same bar before merge. You ship. Grug guard.

## What it does

Each part of the review is a persona with its own check run, named
`Grug - <Persona>`. On a PR you see:

| Check | What it looks at | When it fails |
|---|---|---|
| `Grug - Chief` | The PR body, read as a Hunt Plan (see Contributing). Runs at once, no LLM. | A required section is missing or empty, the PR is `Size: XL`, or a linked ticket has no epic. |
| `Grug - Elder` | The code. An LLM review with whole-file and cross-file context, posted as inline findings plus a findings table in the check summary. | It finds a real problem. A degraded review (model outage, unreadable answer) ends `neutral`, never a block. |
| `Grug - Guard` | Security: SAST, secrets on added lines, dependency CVEs, and Terraform, Kubernetes and Dockerfile misconfigs. An LLM judge drops the noise first. | Advisory by default: findings post as `neutral` until the repo turns on Guard blocking. |

Also on by default:

- **Teller** (`Grug - Teller`) writes a walkthrough of the change into its
  check run and the PR's board comment.
- **Haunt** comments once when a PR closes over an unresolved critical
  finding.

Opt-in per repo: **Smasher** (mutation testing), **Totem** (release notes
and semver after merge) and **Drum** (scheduled project-health nudges).

Talk to Grug by commenting on the PR with a slash command. Use no at-sign:
the bot is `grug-tribe[bot]`, and the at-sign handle belongs to someone else.

| Command | What it does |
|---|---|
| `/grug improve` | Elder reviews the PR again and re-posts its findings. |
| `/grug recheck` | Chief re-runs the Hunt Plan checks. |
| `/grug ask <question>` | Answers a question about the diff as a reply. |
| `/grug fix` | Applies every unresolved suggested fix as one commit. |
| `/grug review` | One Elder review now, even while Elder is paused. |
| `/grug resume` | Lifts the auto-pause (five pushes in ten minutes) and reviews what changed. |

The full list, including `test-gaps`, `learnings` and `accept-removal`, is
at the top of
[`services/webhook/grug_commands.py`](services/webhook/grug_commands.py).

## Quick start

Hosted, at [grug.lol](https://grug.lol):

1. Sign in with GitHub.
2. Install the `grug-tribe` GitHub App on the repos you want reviewed.
3. Turn personas on or off per repo in the dashboard.
4. Open a PR. Chief, Elder and Guard post their checks on it.

The hosted instance is allowlist-gated: open an issue on this repo to ask
for access.

To make Grug gate merges, add `Grug - Elder` (and `Grug - Chief` if you
want the Hunt Plan enforced) as required status checks on the branch.

To run your own Grug instead (it is AGPL-3.0), follow
[`docs/SELF_HOST.md`](docs/SELF_HOST.md); Configuration below lists what you
must bring.

## How it works

```mermaid
flowchart TB
    gh["GitHub"]
    browser["Browser"]

    subgraph cf["Cloudflare"]
        pages["web SPA<br/>Cloudflare Pages"]
        tunnel["Worker + tunnel"]
    end

    subgraph k8s["Kubernetes"]
        webhook["grug-webhook<br/>HMAC verify, then persona dispatch:<br/>Chief, Elder, Guard"]
        api["grug-api<br/>OAuth, /me, /installations"]
        consumer["grug-consumer<br/>Elder reviews from SQS"]
        poller["grug-poller<br/>CronJob, every 15 min"]
    end

    subgraph aws["AWS"]
        sqs[("SQS")]
        keys["KMS + SSM"]
    end

    pg[("Postgres<br/>CNPG")]
    llm["LLM backends"]

    gh -- "webhook" --> tunnel
    browser --> pages
    pages -- "API calls" --> tunnel
    tunnel --> webhook
    tunnel --> api
    webhook -- "Elder jobs" --> sqs
    sqs --> consumer
    consumer --> llm
    k8s -- "state" --> pg
    k8s -. "tokens, secrets" .-> keys
    k8s -- "check runs, comments" --> gh
```

| Part | What it does | Code |
|---|---|---|
| web | Landing page and dashboard (React + Vite) on Cloudflare Pages | [`web/`](web/) |
| grug-webhook | Checks GitHub's signature, runs Chief, starts Guard and Teller, and queues Elder | [`services/webhook/`](services/webhook/) |
| grug-consumer | Waits for a PR to settle, then runs Elder reviews, reruns and `/grug ask` | [`services/webhook/consumer.py`](services/webhook/consumer.py) |
| grug-poller | Reads reactions on Grug's comments to learn which findings were right | [`services/webhook/poller_handler.py`](services/webhook/poller_handler.py) |
| grug-api | GitHub sign-in, the dashboard's reads and writes, admin | [`services/api/`](services/api/) |
| Personas | Chief, Elder, Guard and the rest, shared by both services | [`services/_shared/personas/`](services/_shared/personas/) |
| Deploy | Kubernetes manifests, and Pulumi for the AWS side | [`k8s/`](k8s/), [`infra/pulumi/`](infra/pulumi/) |

Postgres holds installs, repo settings, review history and learnings. KMS
encrypts users' OAuth tokens, and pods read the App key and LLM keys from
SSM at runtime. [`docs/NETWORK-TOPOLOGY.md`](docs/NETWORK-TOPOLOGY.md) has
the request flows and trust boundaries.

## Run and test locally

You need Python 3.13 and [uv](https://docs.astral.sh/uv/); the web app also
needs Node 20. CI (`check.python.yml`) runs these four targets against a
Postgres service container:

```bash
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=grug_test postgres:18-alpine
export GRUG_TEST_DATABASE_URL="postgresql://postgres:test@localhost/grug_test"

make webhook-test
make scripts-test
make api-test
make pg-test
```

Without `GRUG_TEST_DATABASE_URL` the store tests skip, and without
`opengrep` or `semgrep` on your PATH the SAST rule tests skip; CI has both.
`make test` runs the first three targets in one go.

The web app, as `check.web.yml` runs it:

```bash
cd web
npm ci
npm test
npm run build
npm run dev        # local server on port 5173
```

## Configuration

**Per repo**, an optional `.grug.yaml` at the repo root tunes Elder. Grug
reads it from the PR's base commit, so a PR cannot loosen its own review.

| Key | Effect |
|---|---|
| `ignore` | Path globs Elder never reviews |
| `path_instructions` | Extra review instructions for files matching a path |
| `min_inline_severity` | Findings below this level stay in the summary, with no inline comment |
| `skip_prs` | Skip Elder and Teller for PRs matching `authors`, `labels` or `title_patterns` |

```yaml
ignore:
  - vendor/**
  - "*.lock"
path_instructions:
  - path: "k8s/**"
    instructions: Check every container sets resource requests and limits.
min_inline_severity: medium
```

[`docs/REPO_CONFIG.md`](docs/REPO_CONFIG.md) has the details. Which
personas run, and whether Elder and Guard block, is set per repo in the
dashboard.

**Self-hosting**, you supply:

| Setting | Where | What it does |
|---|---|---|
| GitHub App ID, private key, webhook secret, OAuth client ID and secret | SSM under `/grug/` | Your own App; [`docs/HITL_PREREQUISITES.md`](docs/HITL_PREREQUISITES.md) registers it |
| Database URL | SSM `/grug/database-url` | Your Postgres for the `grug_kv` table |
| Domain, AWS region, Cloudflare zone and token | Pulumi stack config, from [`Pulumi.dev.yaml.example`](infra/pulumi/Pulumi.dev.yaml.example) | Provisions SQS, KMS, S3 and DNS |
| LLM API keys | SSM, named by the `*_SSM` env vars in [`k8s/`](k8s/) | Elder's model backends |
| `GRUG_DATADOG_ENABLED` | Pod env | Off unless `true`. Datadog has been off on the hosted instance since 2026-10-05. |

Not in this repo: an AWS account, a Cloudflare account with a tunnel, a
Kubernetes cluster with arm64 nodes and an image registry, and a Postgres
server. [`docs/SELF_HOST.md`](docs/SELF_HOST.md) walks through each step.

## Status

Open work is tracked in the goal
[#1143, "the review bot makes lane PRs better and never slower"](https://github.com/quadseven/grug/issues/1143).

## Contributing

Changes land through a pull request; direct pushes to `main` are refused.
The branch rules require `grug python tests` and `Grug - Chief`, and a PR
merges only once `Grug - Elder` also passes on its head commit.
Read [`AGENTS.md`](AGENTS.md) first: this repository is public.

Grug reviews its own repo, so fill in the Hunt Plan from the
[PR template](.github/pull_request_template.md). Chief checks it:

| Check | Passes when | Blocks? |
|---|---|---|
| `why` | `## Why` (or `## Summary`) has at least 5 words | yes |
| `acceptance` | `## Acceptance criteria` (or `## Test plan`) has 3+ non-empty bullets | yes |
| `estimate` | The body says `Size: XS`, `S`, `M` or `L` (XL must be split) | yes |
| `scope-fence` | `## Out of scope` is not empty | yes |
| `linked-issue-completeness` | Every ticket closed by a closing keyword has its boxes ticked | yes |
| `linked-issue-epic` | Every ticket the body names is an epic or belongs to one | yes |
| `issue-link` | The body links a ticket, e.g. `Refs #N` or `Part of #N` | advisory |
| `loom-footer` | A PR naming a Loom ticket carries the Loom audit footer | advisory |
| `in-scope` | Every changed file traces to the linked ticket's scope | advisory |

Text inside HTML comments does not count, so an unfilled template fails.
Write `Refs #N` rather than a closing keyword: a ticket closes once its
boxes are ticked, not when the PR merges. A PR body with an agent's marker
gets a `neutral` Chief unless a trusted maintainer opened it.

Run the same checks before you open the PR. It exits 0 when Chief would
pass and 1 when it would hold the PR:

```bash
make chief-check REPO=owner/name BODY=pr.md   # a body you are about to post
make chief-check REPO=owner/name PR=123       # an open PR
```

## Tools

- `make pulumi-preview`: read-only diff of the AWS infra.
- `make smoke`: checks the public endpoints answer.
- `make elder-eval` and `make sast-benchmark`: score review quality on a
  committed corpus. These call real models.
- [`scripts/shepherd_verify.py`](scripts/shepherd_verify.py): after a
  deploy, checks a PR's acceptance bullets that name a `Metric`,
  `Log event`, `Endpoint` or `GET` signal.

```bash
python3 scripts/shepherd_verify.py --pr 123 --repo example-org/example-repo \
  --since-epoch "$DEPLOY_FINISHED_UNIX" \
  --query-command /path/to/your-query-adapter \
  --baseline metric=your.known_live.metric
```

The adapter gets `source signal start_epoch end_epoch criterion` and prints
one count. Each source needs a baseline known to emit in the window; a
target is `missing` only when two checks 30 seconds apart both see the
baseline live. Exit codes: 0 observed, 1 missing, 2 unverifiable.

## Docs

| Doc | What is in it |
|---|---|
| [`CONTEXT.md`](CONTEXT.md) | Glossary: every term in the code |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operations: first deploy, secret rotation, tear-down and rebuild |
| [`docs/SELF_HOST.md`](docs/SELF_HOST.md) | Running your own Grug |
| [`docs/HITL_PREREQUISITES.md`](docs/HITL_PREREQUISITES.md) | Registering the GitHub App |
| [`docs/NETWORK-TOPOLOGY.md`](docs/NETWORK-TOPOLOGY.md) | Request flows, placement, trust boundaries |
| [`docs/REPO_CONFIG.md`](docs/REPO_CONFIG.md) | `.grug.yaml` reference |
| [`docs/adr/`](docs/adr/) | Architecture decision records |

## License

[AGPL-3.0](LICENSE). If you run a modified Grug as a network service,
[`docs/SELF_HOST.md`](docs/SELF_HOST.md) explains what you owe its users.
Grug not lawyer. Grug just guard.
