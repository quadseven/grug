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

- **Chief** (`Grug - Chief`) - plan readiness before the hunt leaves the cave
- **Elder** (`Grug - Elder`) - durable Cave review with Markings, Lore, Omen
- **Guard** (`Grug - Guard`) - cave-mouth security (SAST, secrets, SCA, IaC + Seer)
- **Teller** (`Grug - Teller`) - tale of the hunt (walkthrough + mermaid map)

**Smasher**, **Warder**, and **Pulse** are wired as opt-in tracers. Omen runtime
signal already augments Elder where a repository-to-service mapping exists.
Check titles use caveman names (`Grug - <Persona>`); legacy aliases dual-post
during cutover so old required-status rulesets keep working.

Grug live in GitHub. Grug post Check Runs. Grug never spam comments. **You ship. Grug guard.**

## Install

### Hosted SaaS — recommended

1. Sign in at [grug.lol](https://grug.lol) with GitHub
2. Install the **Grug Boss** GitHub App on the repos you want gated
3. Toggle personas per-repo in the dashboard

That's it. Webhook is wired, check-runs post on every PR.

> **Note:** the hosted instance is **allowlist-gated**. Request access by
> opening a GitHub issue on this repo (no bracket prefix needed, `gh issue
> create` works fine) - a maintainer will pick it up.
> Self-host (below) is the open path and needs nobody's
> approval.

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

## What Chief checks (Hunt Plan)

Before the tribe leave the cave, Chief reads the **Hunt Plan** on the PR body —
**6 blocking plan checks, 3 advisory:**

| | Plan check | Pass when | Blocks? |
|---|---|---|---|
| yes | `why` (why hunt) | Has `## Why` (or `## Summary`) with at least 5 words | **yes** |
| yes | `acceptance` (what good look like) | Has `## Acceptance criteria` (or `## Test plan`) with at least 3 non-empty bullets | **yes** |
| yes | `estimate` (how big) | Body includes `Size: XS/S/M/L` (XL must be split) | **yes** |
| yes | `scope-fence` (where stop) | Has a non-empty `## Out of scope` section | **yes** |
| yes | `linked-issue-completeness` (ticket done) | Every ticket closed by a closing keyword has its checkboxes ticked | **yes** |
| yes | `linked-issue-epic` (which epic) | Every ticket the body names is an epic or belongs to one (grug#1034) | **yes** |
| soft | `issue-link` (which ticket) | Body links a ticket, e.g. `Refs #N` or `Part of #N` | advisory |
| soft | `loom-footer` (loom mark) | A PR naming a loom ticket carries the Loom audit footer | advisory |
| soft | `in-scope` (in scope) | All changed files trace to the linked issue's scope (grug#1195) | advisory |

Text inside HTML comments counts for nothing, and neither does a bare `-`
or empty checkbox: a PR body left as the template ships fails the plan.
Write `Refs #N` and `Part of #N`, not a closing keyword: a ticket closes
once its boxes are ticked, not when the PR merges.

The `in-scope` check names any rider files the linked issue never asked for
and is advisory by default; org/repo config can promote it to blocking.

The Hunt Plan is for PRs written by people and by operator sessions. A PR
body carrying an agent-authored marker (a `claude.ai/code/session_` link or
Claude Code's attribution footer) is still checked when its author is a
trusted operator (a human owner, member or collaborator on the repo). It gets
a **neutral** `Grug - Chief`, which still satisfies a required check, when it
is a fleet lane PR (agent marker plus the Loom audit footer), carries the
explicit `<!-- grug:agent-authored -->` opt-out, or comes from any other
author. The check-run title names the rule that applied. Elder and Guard
review every PR as usual.

### Check a Hunt Plan before you open the PR

`gh pr create --body` skips the repo's PR template, so a missing section or a
ticket with no epic usually shows up as a red `Grug - Chief` after the PR is
open. `scripts/chief_check.py` runs the same evaluator Chief runs, against
your `gh` login, and prints the same title and table:

```bash
make chief-check REPO=owner/name BODY=pr.md      # a body you are about to post
make chief-check REPO=owner/name PR=123          # an open PR's current body
gh pr create --body-file pr.md                   # once it exits 0
```

It exits 0 when Chief would pass (or skip), 1 when Chief would hold the PR,
and 2 on a usage error. When `which epic` fails it lists the repo's open
`Epic: ...` issues and the two fixes: add the ticket as a sub-issue of an
epic, or write `Part of #<epic>` in the ticket's body.

### Verify stated claims after a deploy

`scripts/shepherd_verify.py` reads the PR's `## Acceptance criteria` bullets
and checks explicitly named `Metric`, `Log event`, `Endpoint`, or `GET`
signals. Names may be in backticks, and metric names or URLs may be plain
text. It does not infer a signal from prose elsewhere in the PR. The caller
runs it after deploy completion and supplies source-specific query commands
and known-live baselines:

```bash
python3 scripts/shepherd_verify.py --pr 123 --repo example-org/example-repo \
  --since-epoch "$DEPLOY_FINISHED_UNIX" \
  --query-command /path/to/your-query-adapter \
  --baseline metric=your.known_live.metric
```

The adapter receives `source signal start_epoch end_epoch criterion` as
arguments and prints one nonnegative integer count. It must query the named
source over that window, including both the target and its baseline from the
same source. For an endpoint, it must count only responses matching the
criterion's expected status or body. A nonzero exit or invalid count makes
the result unverifiable.
Supply a baseline for each source used by the PR. The baseline must be known
to emit during the post-deploy window; a historical signal is not enough.
The default is two checks 30 seconds apart. An absent target is `missing`
only after both checks find its baseline live. An empty baseline, missing
baseline configuration, failed lookup, or PR with no explicit observable is
`unverifiable`. The output includes each criterion and the counts from each
attempt. Exit codes are 0 observed, 1 missing, and 2 unverifiable.

## What Elder and Guard check

### Elder: deep code review (Apex)

Every PR snapshot, after its base, head, and real author intent stay stable for
the adaptive quiet window:

- **Swift Hunt settle** - tiny PRs skip the quiet wait; medium PRs settle a few
  seconds; large multi-file storms keep the full window. Elder posts an
  `in_progress` `Grug - Elder` check the moment the durable job is
  queued, so required-status rulesets show pending instead of "check missing".
- **Tiered Cave review** (ADR-0019 / #645) - default is a single **coder** arm
  for ordinary PRs; the **reasoner** arm escalates only on large diffs,
  high-risk paths, explicit `deep-review`, or a calibration sample. Dual-arm
  `GRUG_REVIEW_DEPTH=deep` remains the max-recall rollback. Covers 25+ named
  correctness, error-handling, concurrency, and security-shape rules.
  Whole-file + 1-hop cross-file context + PR intent ride every pass. SaaS
  overload fallback stays last-resort (ADR-0005).
- **Markings Board** - structured inline findings: severity, effort, What Elder
  sees, Fix (one-click when safe), Lore citations from prior tribe history, and
  a copy-paste agent repair prompt. Check-run summary is a full findings table.
- **Lore + Omen** - accepted/rejected ledger history becomes measured confidence
  and few-shot practices; Datadog runtime signal (Omen) fuses when a service map
  exists - a moat that comes from owning the stack end-to-end.
- **Stable snapshot identity** - bot HTML footers no longer thrash mid-flight
  reviews; only base/head or real human intent changes re-queue Elder.
- **Living Hunt + auto-pause** - a push after Elder's first review gets a delta
  review of only what changed since the last reviewed head, with the commit
  range in the check title. Five pushes inside ten minutes pause Elder with one
  note on the PR; `/grug resume` lifts the pause and reviews the delta, and
  `/grug review` takes one look while the pause stays (grug#1188).
- **Deletion-only review** - a PR that only deletes files gets a verdict. A
  deleted guard (a CI workflow, CODEOWNERS, ruleset config, a security, policy
  or leak-scan file, or a test whose code stays) blocks, and only a person can
  clear it: `/grug accept-removal <path> <head-sha>` with the full SHA (the finding prints the
  exact command) from a User account with an OWNER, MEMBER or COLLABORATOR
  association and write access clears that path's finding when the SHA is the
  current head, then Elder re-reviews. Whoever wrote the change accepts only
  as OWNER or with admin. The check summary names who accepted it and on which
  head; a new push needs a new acceptance (grug#1401, grug#1405).
- **Feedback learning** - judge labels and write-authorized maintainer reactions
  retain per-model provenance. Confirmed findings refresh positive practices and
  few-shot examples; false positives become bounded AVOID guidance for later
  reviews.

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

Issues + PRs welcome. Use the Hunt Plan template — Grug will gate your own PR. Fair is fair.

PR body must have `## Why`, `## Acceptance criteria`, `## Out of scope`, `Size:`, and `Refs #N` (plus `Part of #N` naming the epic).

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
