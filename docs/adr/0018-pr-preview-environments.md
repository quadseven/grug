# ADR-0018 - PR preview environments (namespace-per-PR)

## Status

Accepted (2026-07-05). Implements #500, the final slice of the release
chain (builds on #498 pr-<n> images + #499 deploy/synthetic machinery).

## Context

With #498 pushing a `pr-<n>` image per PR, a preview deploy is nearly
free: a reviewer (or a future synthetic) can exercise a REAL running PR
before merge - the Vercel preview model on our own 4-node cluster. The
constraints: it must not touch prod secrets, must not starve prod, and
must clean itself up.

## Decision

1. **Opt-in via the `preview` label.** `preview.yml` (pull_request:
   labeled/unlabeled/synchronize/closed) deploys `grug-pr-<n>` only while
   the PR is open, labeled, and same-repo (fork PRs get no cluster
   creds). It requires the #498 `pr-<n>` images to exist.
2. **Namespace + schema isolation.** Namespace `grug-pr-<n>` holds both
   services on the PR digest; data is a throwaway Postgres SCHEMA
   `grug_pr_<n>` in the shared CNPG DB (drop+create each refresh - the app
   bootstraps `grug_kv` into it via `search_path`).
3. **No prod secrets - a namespace-gated preview mode.** A preview pod
   gets a `grug-preview-secret` with only the schema-scoped DB URL and a
   FAKE random webhook secret. `services/_shared/preview_mode.py`
   activates ONLY when `GRUG_PREVIEW=1` AND the pod's own namespace
   (downward API) starts with `grug-pr-`; the namespace check is the
   load-bearing guard, so the RA-identity/SSM/KMS hardening (#388/#389)
   can NEVER be disabled in the prod `grug` namespace even if the flag
   leaks. In preview mode: the RA proof auto-skips (no `AWS_CONFIG_FILE`),
   `/readyz` gates on Postgres only, and `get_webhook_secret` reads the
   fake env secret.
4. **Resource caps.** A `ResourceQuota` + `LimitRange` per preview
   namespace bound CPU/memory/pods; a `NetworkPolicy` denies cross-
   namespace traffic (DNS + Postgres + HTTPS egress only).
5. **Reaping - two paths.** The workflow reaps on PR close/merge/unlabel
   (delete namespace + drop schema). The `grug-preview-janitor` hourly
   CronJob is the TTL backstop: it deletes `grug-pr-*` namespaces older
   than 48h (`GRUG_PREVIEW_TTL_HOURS`) and drops their schemas. Both use
   `preview_names.pr_of_namespace`, which refuses any name not matching
   `^grug-pr-<digits>$` - a fat-fingered selector cannot reach prod/system
   namespaces.

## Consequences

- Reviewers can hit a running PR before merge; a future #499-style
  synthetic could target the preview namespace pre-merge.
- The shared CNPG credential is reused (schema isolation, not credential
  isolation). A per-schema DB role is a hardening follow-up; the data
  partition is the schema.
- Previews are same-repo-only and opt-in (cluster is small) - auto-preview
  on every PR is explicitly out of scope.
- The janitor SA has cluster namespace delete (unavoidable for namespace
  ops); the name-regex guard bounds its blast radius.

## References

- #500, #498/ADR-0016, #499/ADR-0017, ADR-0013 (the grug-trial
  namespace-isolation precedent this mirrors).
