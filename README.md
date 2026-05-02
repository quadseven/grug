# Grug — automated TPM bot

Runs Definition-of-Ready checks on PRs + scheduled iteration pulse.
Cross-repo callable via two reusable workflows in this repo.

## Quick start (consumer repo)

### 1. PR-gate (every PR)

`.github/workflows/grug.pr-gate.yml`:

```yaml
name: Grug · PR DoR check
on:
  pull_request:
    types: [opened, edited, synchronize, ready_for_review]
permissions:
  contents: read
  pull-requests: write
  issues: write   # PR comments live under issues API
jobs:
  grug:
    uses: githumps/grug/.github/workflows/_reusable.grug-pr-gate.yml@main
    with:
      strict: true   # block merge on DoR fail; false = advisory only
    secrets:
      poolside_api_key: ${{ secrets.POOLSIDE_API_KEY }}
```

### 2. Scheduled pulse

`.github/workflows/grug.pulse.yml`:

```yaml
name: Grug · weekly pulse
on:
  schedule:
    - cron: '0 13 * * 1'   # Monday 13:00 UTC = ~05:00 PT
  workflow_dispatch:
permissions:
  contents: read
  issues: write
jobs:
  pulse:
    uses: githumps/grug/.github/workflows/_reusable.grug-pulse.yml@main
    with:
      issue_label: "grug-pulse"
      mode: "weekly"
    secrets:
      poolside_api_key: ${{ secrets.POOLSIDE_API_KEY }}
```

### 3. Required prereqs (per consumer repo)

1. **Create environment** `grug-bot` (Settings → Environments → New). No
   protection rules required — existence alone scopes the secret away
   from unrelated workflows on the same runner.
2. **Add secret** to that environment: `POOLSIDE_API_KEY` (Poolside free
   tier, OpenAI-compatible).
3. `GITHUB_TOKEN` auto-provided.
4. (Optional) Create label `grug-pulse` so the weekly issue is
   filterable; the workflow soft-warns + creates on first run if absent.
5. (Optional) **Project v2 auto-add**: pass your project's GraphQL node
   ID (`project_id`) and a PAT with `project` scope (`project_pat`).
   Pulse issues are added to that project on creation. If you also want
   them to land in a specific column (e.g. "Triage"), pass
   `project_status_field_id` + `project_status_option_id`.

   ```yaml
   with:
     issue_label: "grug-pulse"
     mode: "weekly"
     project_id: "PVT_..."                      # GraphQL node ID
     project_status_field_id: "PVTSSF_..."      # optional
     project_status_option_id: "<option-id>"    # optional, paired
   secrets:
     poolside_api_key: ${{ secrets.POOLSIDE_API_KEY }}
     project_pat: ${{ secrets.PROJECT_PAT }}
   ```

   **Find IDs:**
   - `project_id`: `gh api graphql -f query='query{user(login:"<owner>"){projectV2(number:N){id}}}'`
   - `project_status_field_id` + option IDs: `gh project field-list <N> --owner <owner> --format json` (works locally where gh CLI has interactive auth; for CI use direct GraphQL or copy the IDs)

   **Why GraphQL IDs and not owner+number?** Earlier revision used
   `gh project item-add --owner X --number N` which does an owner-type
   detection probe inside gh CLI. That probe fails with `unknown owner type`
   on classic PATs that have `project` scope but lack `read:user`/`read:org`.
   Direct GraphQL mutations (`addProjectV2ItemById`) skip the probe — same
   auth, fewer scope requirements.

   If any of `project_id` / `project_pat` is unset, the project step
   no-ops (silent + safe).

## What Grug checks (Definition of Ready)

Static checks on PR body — all blocking when `strict: true`:

| Check | Pass when |
|---|---|
| `why` | Has `## Why` (or `## Summary`) section ≥5 words |
| `acceptance` | Has `## Acceptance criteria` (or `## Test plan`) with ≥3 bullets |
| `estimate` | Body or label includes a Size: XS/S/M/L (XL must be split) |
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

## Stale issue labelling

Pulse also labels stale open issues by default (subsumes `actions/stale`
so all TPM mutation lives in one bot). Tunable via caller inputs:

```yaml
with:
  label_stale: true            # set false to disable
  stale_days: 90               # threshold
  stale_label: "stale"         # label name
  stale_exempt_labels: "epic,pinned,security,grug-pulse"
```

Behavior is idempotent — already-stale issues skip; exempt-labelled
issues skip; mutations cap at 30/run to stay under API limits. Never
auto-closes anything.

## Degradation behavior

Poolside outage / rate limit / missing key → LLM section shows
"degraded" message; static checks still run + still gate. The bot
fails-soft on the LLM, not on structure.

## Re-run

Every PR push re-triggers. Manual re-run: comment `/grug recheck`
(future) or push empty commit:

```bash
git commit --allow-empty -m "trigger grug recheck"
git push
```

## Sticky comment

Grug uses an HTML-comment marker (`<!-- grug-tpm-bot:sticky -->`) so
each run patches the same comment instead of stacking new ones.
