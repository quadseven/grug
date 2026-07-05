# ADR-0016 - Release promotion: deploy the PR-tested image digest, not a rebuild

## Status

Accepted (2026-07-05). Implements #498 (entry slice of the release chain:
#499 synthetic self-test + auto-rollback, #500 preview namespaces).

## Context

check.image-build.yml (the #77/ADR-0014 gate) builds BOTH service images on
every PR and smoke-proves their import graphs, boot-refusal tripwire, and
signing-helper ABI - then throws the images away. Merging to main triggers a
full REBUILD in deploy.k8s.yml which applies straight to prod. Two costs:
double build time, and the digest that reaches prod is NOT byte-identical to
the one the gate tested (different build timestamps at minimum; different
code whenever main moved under the PR).

Worse, the gate built on ubuntu-latest (x86) while the cluster and the
deploy build are arm64 - the "PR-tested image" was not even the artifact
architecture that ships.

## Decision

1. **The PR gate builds the shippable artifact.** check.image-build.yml
   moves to the ubuntu-24.04-arm hosted runner (free for public repos, same
   as deploy) and, for SAME-REPO PRs, pushes the smoked images to the
   private registry tagged `pr-<n>`. Fork PRs get no credentials by GitHub's
   own rules and skip the push - the gate's smokes still run.
2. **Merge PROMOTES when it is safe to do so.** deploy.k8s.yml resolves the
   merged PR for the pushed sha and promotes the `pr-<n>` digest via a
   server-side retag (`docker buildx imagetools create`) - no rebuild, no
   layer transfer - IFF the promotion decision passes. Otherwise it falls
   back to the existing build path (direct pushes, workflow_dispatch,
   fork-sourced merges, stale images).
3. **The promotion decision is a pure, unit-tested function**
   (`scripts/promotion.py`): promote only when (a) the merge commit resolves
   to exactly one PR, (b) the PR head's GIT TREE equals the merge commit's
   tree - if main moved under the PR, the tested image is NOT the merged
   code and MUST be rebuilt - and (c) both services' `pr-<n>` manifests
   exist in the registry.
4. **Provenance is recorded on the workloads.** The deploy annotates each
   Deployment/CronJob with the image source (`promoted-pr-<n>` or
   `rebuilt`), the digest, and the merge sha.
5. **Credential surface** (the security fork this ADR owns): a dedicated
   `registry-ci` GitHub environment (Pulumi-managed in the infrastructure
   repo, branch policy `*`) holds a PUSH-scoped registry credential and an
   ephemeral tag:ci tailnet key. Trust boundary, stated honestly: any
   SAME-REPO PR author can push `pr-*` images (and could modify the gate
   workflow to misuse the credentials) - acceptable because same-repo
   branch authors are already write-gated humans in a single-operator org,
   and the blast radius is bounded by (3): a poisoned pr tag only ships if
   the corresponding PR also merges. Fork PRs cannot reach any of it.
   k8s-prod (cluster credential, SSM read role) remains main-only and is
   NOT exposed to PRs.

## Consequences

- Deploys get faster (retag instead of rebuild) and prod runs the exact
  bytes the gate proved.
- The registry accretes `pr-*` tags; the infrastructure repo's
  tag-retention CronJob learns to expire them (they previously matched its
  "named tag = keep forever" rule).
- A rebuild fallback stays load-bearing forever: promotion is an
  optimization, never a requirement - anything ambiguous rebuilds.
- DD_GIT_COMMIT_SHA baked in a promoted image is the PR head sha, which is
  tree-identical to the merge by rule (3b); DD_VERSION (manifest env) still
  carries the merge sha.

## References

- #498, #77/ADR-0014 (the gate this promotes from), infrastructure
  retention CronJob (#1555), somatic-scripts aws-verify environment (the
  Pulumi-managed pre-merge-environment precedent).
