# ADR-0008 — Roles Anywhere trust model for pod AWS credentials

## Status

Accepted (2026-07-03, ratified via PR #441; the shared PKI it depends on shipped in infrastructure#1318). Resolves audit #6. Supersedes the long-lived static
key + interim rotator (#386, shipped as a throwaway). Parent PRD: #385.
Implemented by #388 (end-to-end tracer) then #389 (rollout + static-key
retirement).

## Context

grug's in-cluster pods (api, webhook, poller, consumer) authenticate to AWS
with a **long-lived static IAM-user access key**. The key lives in SSM
(`/grug/k8s-pod-aws-access-key-id` + `-secret-access-key`) and is injected as
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env at deploy time
(`deploy.k8s.yml`). #386 shipped an interim CronJob rotator (a second IAM user,
`/grug/k8s-rotator-*`) that rotates the pod key on a schedule
(create-new -> update-secret -> roll-and-wait -> delete-old). The rotator is
explicitly a stopgap; a long-lived key in a Secret is the audit-#6 risk and the
rotator only shortens, not removes, its lifetime.

The standard fix on EKS is **IRSA** (pods assume a role via a projected SA
token validated against the cluster's public OIDC issuer). IRSA is
**platform-blocked on managed OKE**: the service-account token issuer is
internal and the JWKS is private, so AWS STS cannot validate the SA JWT. There
is no public OIDC endpoint to register as an identity provider. This is the
reason Roles Anywhere is on the table rather than IRSA.

**AWS IAM Roles Anywhere** lets a workload exchange an **X.509 client
certificate** for short-lived STS credentials. The pieces are a **Trust
Anchor** (references the CA that signs workload certs), a **Profile** (maps a
presented cert to assumable IAM roles), and the **`aws_signing_helper`** binary
wired as the AWS SDK's `credential_process`. The whole design reduces to two
questions: where the CA comes from, and how the cert + helper reach the pod.

A managed CA (AWS Private CA / ACM PCA) is the cleanest integration but costs
~$400/mo base, which contradicts the standing free-tier / no-paid-services
posture. The free path is a **self-signed CA** registered as an *external*
trust anchor.

The operator also requires the CA to be **reusable across projects** (grug,
digital-ledger, macchina, ...), not grug-specific.

## Decision

Adopt **AWS IAM Roles Anywhere** with a **self-signed cert-manager
ClusterIssuer** as the CA, provisioned as **shared, cluster-wide PKI** and
consumed per project.

### Trust model

1. **Shared CA + trust anchor live in `infrastructure` (Pulumi + cert-manager),
   not in grug.** A self-signed cert-manager `ClusterIssuer` is the CA; its CA
   certificate is registered as a single **external** Roles Anywhere Trust
   Anchor per AWS account. This is inherently multi-tenant: one CA + one trust
   anchor serve every project. (Honors the all-Pulumi / GitOps-for-cluster-infra
   rule; cluster-wide concerns do not belong in an app repo.)
2. **Each project owns its own Profile + IAM role + workload Certificate.** grug
   owns a `grug-k8s-pod` Profile + least-privilege role (the existing pod
   permissions: SSM read, the grug SQS queues, KMS for per-user encrypt, S3
   cave-diff write) mapped from a cert whose subject/SAN identifies the grug
   workload. A project's blast radius is its own role; the shared CA does not
   widen it.
3. **Pod certs are short-lived and auto-renewed.** A cert-manager `Certificate`
   issues the pod cert+key into a Secret with **duration 6h, renewBefore 4h**
   (aligned at #388 with the infrastructure tenant recipe proven in the #1318
   acceptance; this ADR originally said 2h - 4h renews earlier, never letting
   a pod hold a cert within 4h of expiry).
   STS sessions from `aws_signing_helper` are ~1h. A leaked cert is useless
   within hours; a leaked STS session within ~1h.
4. **grug creds delivery: `aws_signing_helper` baked into the image + SDK
   `credential_process`.** Fewest moving parts, no sidecar, fine for grug's
   Python SDK. This is a *per-project* implementation detail behind the shared
   PKI, revisable per project (a non-Python or polyglot workload may prefer a
   sidecar that writes a credentials file).

### Cutover and rollback (binding on #388/#389)

- Keep the static-key path live until a **real pod read of SSM, SQS, KMS, and
  S3 via the Roles Anywhere path is proven** on a deployed pod (the #388
  tracer). Only then (#389) retire the static key, the `/grug/k8s-pod-aws-*`
  SSM params, and the #386 rotator (CronJob + `key_rotator.py` +
  `k8s_rotator.py`).
- Rollback at any point is re-pointing the Deployment back at the static-key
  Secret, since both paths coexist until retirement.

### Forkability

A forker stands up **their own** ClusterIssuer CA, their own trust anchor, and
their own profiles/roles. Nothing in grug references the operator's specific
trust anchor by value; the anchor + profile ARNs are config (SSM), not code.

## Consequences

### Positive

- Removes the long-lived static AWS key entirely (the audit-#6 risk), replacing
  it with short-lived, auto-rotating, cert-derived STS credentials.
- Free: a self-signed cert-manager CA avoids ACM PCA's ~$400/mo.
- Reusable: one CA + one trust anchor serve every project; new tenants add only
  a Profile + role + Certificate. Directly satisfies the operator's
  reuse requirement.
- Retires the #386 interim rotator and its second IAM user.
- Forkable: no dependency on the operator's specific anchor.
- IRSA-shaped security posture without needing the public OIDC issuer managed
  OKE does not expose.

### Negative

- New cluster dependency: **cert-manager** must be provisioned (none today).
  This is cross-repo infra work in `infrastructure` that grug depends on (the
  shared CA + trust anchor must exist before grug's rollout). Needs its own
  infra issue/slice; grug #388 is blocked on it.
- Self-signed CA means the **operator owns CA-key custody and the CA's own
  rotation**; a compromised CA key forges any workload cert. Mitigated by
  keeping the CA key in-cluster (cert-manager) and short pod-cert TTLs, but it
  is a real trust root to protect.
- `aws_signing_helper` baked into the image adds a binary + a `~/.aws/config`
  `credential_process` line to maintain.

### Reconsideration triggers

- Managed OKE exposes a public OIDC issuer + JWKS -> **IRSA** becomes viable
  and is simpler (no CA, no certs); revisit and likely migrate.
- AWS ships a cheaper managed private CA, or the free-tier posture changes ->
  reconsider ACM PCA for a cleaner integration.
- cert-manager proves too heavy for the cluster -> reconsider the manual
  offline-CA-in-SSM variant (rejected here for the bespoke renewal code it
  requires).

## References

- #387 — this ADR (audit #6)
- #385 — PRD: migrate pods off the long-lived static key (parent)
- #386 — interim key rotator (shipped stopgap this supersedes)
- #388 — Roles Anywhere end-to-end tracer (next; proves the path on a live pod)
- #389 — roll Roles Anywhere to all pods + retire the static key
- infrastructure (TBD issue) — shared cert-manager ClusterIssuer CA + Roles
  Anywhere trust anchor (cluster-wide PKI this ADR depends on)
