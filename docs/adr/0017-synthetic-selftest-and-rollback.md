# ADR-0017 - Post-deploy synthetic self-test + last-good digest rollback

## Status

Accepted (2026-07-05). Implements #499 (release chain: builds on
ADR-0016's promotion + provenance trail; #500 previews remain).

## Context

A deploy that passes rollout checks can still be broken (bad config, a
runtime-only bug); recovery was revert-PR + full rebuild (minutes) or a
hand-run kubectl. For a solo operator, merged code should prove itself
and the escape hatch should be seconds, not minutes.

## Decision

1. **Rollback anchor = the release running BEFORE the latest deploy.**
   The deploy snapshots the currently-running digest pair (webhook+api)
   into the `grug-last-good` ConfigMap BEFORE any mutation (only when
   every workload is currently Available, so a broken state is never
   anchored). The anchor deliberately does NOT advance to the new
   release on success: manual rollback's one job is "get me OFF the
   current release" - including a bad release that fooled the synthetic
   (codex r11; the earlier no-op-drill framing from r5 inverted that
   priority and was removed). A drill therefore genuinely reverts one
   release; roll forward afterwards by re-running deploy.k8s via
   workflow_dispatch. The anchor's digest
   manifests are protected from registry retention by the live-cluster
   reference rule (and by their merge-sha tags).
2. **Synthetic self-test after apply**: /livez + /readyz on both
   services (port-forward from the runner - no public round-trip
   dependency), a SIGNED ping delivery through the full auth stack (CF
   shared-secret middleware + HMAC verify + the SSM secret fetch that
   proves Roles Anywhere creds work in the new pods; ping dispatches
   nothing = side-effect-free, the #368 proof mechanics automated), then
   a 60s soak asserting zero container restarts and re-probed health.
   The soak is the OWNED stand-in for the "DD error-rate window" idea:
   the deploy role deliberately has no DD keys (least privilege), and
   restarts+readiness catch the same crash classes.
3. **Auto-rollback on ANY post-anchor failure** (a failed apply/rollout/
   smoke - captured via continue-on-error - or a failed synthetic):
   re-apply the anchored digests
   (set image, no rebuild, ~30s), annotate
   `grug.dev/image-source=rollback-last-good`, emit the owned
   `grug.deploy.rollback` count via a node's DogStatsD hostPort, and
   fail the run LOUDLY. The '[grug] Deploy auto-rollback fired' monitor
   (Pulumi, pinned by synth test) pages Discord on any occurrence. The
   bad merge stays on main by design - rollback restores service, the
   operator decides revert-vs-fix-forward.
4. **One-click manual path**: `deploy.rollback.yml` workflow_dispatch
   re-applies the anchor the same way (safe to drill: after a good
   deploy the anchor equals the running images, so a drill is a no-op
   rollout).
5. **Telemetry (#498 handoff)**: successful deploys emit
   `grug.deploy.completed` tagged `source:promoted-pr-N|rebuilt`, and the
   promotion decision now takes a `gate_ran` fact (from the head's
   check-runs) so workflow-only merges classify as expected-rebuild
   instead of firing the artifact-missing warning.

### What rollback does NOT cover (image-only contract)

The anchor records DIGESTS plus the mutated durable objects: the
auto-rollback restores the pre-seed snapshots of grug-secrets AND both
registry-pull secrets (a rotated-wrong pull credential would strand
rollback pods under imagePullPolicy Always) and force-restarts the pods
so they mount and pull with the restored values. Everything else applied from k8s/ (manifests, the RA ConfigMap)
is NOT undone - the rollback step warns when the failing merge touched
k8s/, and the recovery for manifest-shaped failures is revert +
redeploy. The MANUAL deploy.rollback.yml path is image-only (a later
run has no snapshot). Full
release-state rollback (rendered-manifest snapshots) is deliberately
rejected at this scale - it is the Argo/GitOps threshold the issue
excluded.

## Consequences

- A bad merge self-heals to the previous release in ~30s; the failure
  mode "deploy green, prod broken, operator asleep" now pages with prod
  already restored.
- The synthetic adds ~90s to every deploy (probes + soak).
- The anchor is one generation deep - sufficient for the
  revert-or-fix-forward loop this repo actually runs; deeper history
  lives in the registry's sha tags (KEEP_NEWEST window).
- consumer/poller roll back to the webhook image by construction (they
  share it); a future third image joins the anchor by adding one literal.

## References

- #499, #498/ADR-0016, #368 (the proof mechanics), infrastructure
  retention CronJob (live-reference protection).
