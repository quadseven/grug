# Cutover playbook: reusable workflow → Grug App

Migrating each consumer repo off `_reusable.grug-pr-gate.yml` to the
hosted Grug App. Tracks slices #32 (canary on infrastructure) and
#33 (bulk on remaining 9).

> **Note:** this playbook moves *consumer repos* onto the hosted App — it
> is unrelated to the App's own runtime. The App now runs on Kubernetes
> (Postgres store, SQS, Cloudflare tunnel; see [`RUNBOOK.md`](RUNBOOK.md)
> + [`NETWORK-TOPOLOGY.md`](NETWORK-TOPOLOGY.md)). The `curl /livez`
> checks below hit the k8s pods, not the retired Lambdas.

## Pre-flight (one-time)

Verify on grug.lol dashboard:
- `https://grug.lol/admin` shows your admin USER# row
- Webhook alive: `curl -sI https://webhook.grug.lol/livez | head -1` returns `HTTP/2 200`
- API alive: `curl -sI https://api.grug.lol/livez | head -1` returns `HTTP/2 200`

If any fail, see [`RUNBOOK.md`](RUNBOOK.md) "First-time deploy" / "Common
failure modes" before continuing.

## Per-repo cutover (5 steps, ~5 min each)

### 1. Install the App

UI: https://github.com/apps/grug-boss/installations/new → pick the
target repo → install.

Webhook fires `installation` event → grug-webhook records the install
in Postgres (`grug_kv`). Verify on `/admin` that the new INST# row
appears (refresh).

### 2. Test the App posts a check-run

Open a fresh PR on the target repo. PR body should INTENTIONALLY miss
DoR sections (e.g. drop the `## Why`) to confirm the FAILURE path
works. Then update body with valid DoR sections to confirm SUCCESS.

Look for `Grug — Definition of Ready` (no `Grug · DoR check / `
prefix — that prefix is the legacy reusable-workflow check name).

### 3. Update branch protection

```bash
REPO=githumps/<target-repo>

# Snapshot current required checks (so we can re-add them):
gh api "repos/${REPO}/branches/main/protection/required_status_checks" \
  --jq '.contexts[]'

# Replace required-checks list — drop the old workflow-prefixed name,
# add the App-posted name:
gh api -X PATCH "repos/${REPO}/branches/main/protection/required_status_checks" \
  -f strict=true \
  -f 'contexts[]=Grug — Definition of Ready' \
  -f 'contexts[]=<other context>' -f 'contexts[]=...'
  # ... keep DD/Snyk/etc. contexts from the snapshot above
```

Note: `contexts[]` rebuilds the entire list. Snapshot first, then write.

### 4. Delete the legacy workflow file

Open a PR ON THE TARGET REPO that deletes
`.github/workflows/grug.pr-gate.yml`:

```bash
cd /path/to/<target-repo>
git checkout -b chore/cutover-to-grug-app
git rm .github/workflows/grug.pr-gate.yml
git commit -m "chore: cut over to grug.lol GitHub App

Closes the migration off the legacy githumps/grug reusable workflow.
The hosted App at grug.lol now posts the 'Grug — Definition of Ready'
check on every PR.

Per githumps/grug Slice $SLICE_NUM cutover."
git push origin chore/cutover-to-grug-app
gh pr create --title "chore: cut over to grug.lol GitHub App" --body "..."
```

This PR's own check-run is gated by the new App. Self-test loop:
if the App is broken, the PR can't pass DoR. If the PR passes DoR,
the cutover is verified.

### 5. Verify in production

After merge:
- Open another PR on the target → check-run should still post
- `gh api repos/${REPO}/commits/<head_sha>/check-runs --jq '.check_runs[] | .name'`
  should list `Grug — Definition of Ready` (no workflow-prefix)

If anything broke, re-add `.github/workflows/grug.pr-gate.yml` from
git history and revert step 3's branch protection change. Cutover is
fully reversible until step 4 lands on default branch.

## Slice 11 — canary on a private repo you own

Use a private repo you own first. Smallest blast radius, you control it, can iterate.
Steps 1-5 above. Confirm everything green for ~24h before bulk-cutover.

## Slice 12 — bulk-cutover remaining 9 repos

Order (smallest → largest, or by criticality):

1. `aws-solutions-architect-study` (low-traffic study repo)
2. `gemini-plugin-cc` (small)
3. `meow-now`
4. `holdfast`
5. `vroom-vroom`
6. `claude-stuff`
7. `conducted`
8. `grugthink` (production-traffic AWS service)
9. `somatic-scripts` (largest, most active)

For each: re-run steps 1-5. Total wall-clock ~45 min if you batch,
plus a few PRs per repo to merge.

## Post-cutover (Slice 13)

After all 10 consumer repos migrated:

- Delete `.github/workflows/_reusable.grug-pr-gate.yml` from
  `githumps/grug` (no consumers left)
- Delete `.github/workflows/grug.pr-gate.yml` from `githumps/grug`
  (the self-check; switches to App-posted check on grug repo too —
  install Grug App on `githumps/grug` itself first)
- Update branch protection on `githumps/grug` main accordingly
- Close PRD #21
