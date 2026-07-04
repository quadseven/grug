# ADR-0013: Smasher Trial — diff-scoped mutation testing in a locked-down k8s Job

- Status: Accepted
- Date: 2026-07-04
- Refs: #469 (epic #464 slice 5, PRD #346 Pillar 3.1), ADR-0010 (registry
  dispatch), ADR-0012 (Guard extraction — the async-persona precedent)

## Context

Grug reviews code; it does not EXECUTE it. The execution-class capability
("Smasher") starts with the cheapest ownable slice: **diff-scoped mutation
testing**. Mutate only the lines a PR added/changed, run the repo's own test
suite per mutant; a mutant the tests still pass on ("survived") is an
EXECUTABLE proof of a coverage gap, with a concrete reproducer — replacing the
Elder's LLM *guess* at missing coverage with a fact.

The hard part is not the mutation engine; it is that running mutation testing
means EXECUTING PR-author-controlled code (the repo's tests + the code under
test). That must never happen inside the `grug-webhook` pod, which holds the
GitHub-App key, SSM/KMS access, the Postgres URL, and the SQS grants. The
whole slice is a sandbox-boundary problem.

## Decision

### Execution vessel: one locked-down Kubernetes Job per Trial run

The webhook (which already processes the untrusted webhook payload but holds no
code-execution surface) LAUNCHES a Job and never runs author code itself. The
Job is the vessel; its pod spec is the security boundary:

- `automountServiceAccountToken: false` on the Job pod — the code under test
  gets NO Kubernetes credential.
- Two containers sharing a `workspace` emptyDir, run as separate phases:
  - **init `clone`** — the ONLY holder of a short-lived credential. Receives a
    GitHub installation access token scoped `contents:read` on the SINGLE repo
    (the token-create API accepts a `repositories` + `permissions` subset),
    clones at the PR head SHA, installs test dependencies (this phase has
    egress). Writes the checkout + the non-secret target map to `workspace`.
    The token is an env var on THIS CONTAINER ONLY.
  - **main `test`** — NO token, NO secrets, `readOnlyRootFilesystem: true`
    except the workspace, `runAsNonRoot`, all caps dropped,
    `seccompProfile: RuntimeDefault`, CPU/memory limits. Runs the mutation
    worker over the checkout. This is where author code executes; it holds
    nothing worth stealing and (see below) is the network-jailed phase.
- `activeDeadlineSeconds` = the total wall-clock budget: the kubelet kills a
  runaway Job regardless of what the code under test does.

### Result channel: the pod termination message, NOT logs

BYON worker nodes' kubelet API (`:10250`) is unreachable from the control
plane, so `kubectl logs` / the logs API cannot read the Job's output (a live
constraint, see `feedback` memory on BYON). Instead the worker writes its
survived-mutant summary as JSON to `/dev/termination-log`; the launcher reads
it back via `pod.status.containerStatuses[].state.terminated.message` (the
Kubernetes API, no kubelet access, 4 KiB cap — survived-mutant summaries fit).

### Launcher RBAC (minimal)

The webhook pod gets a dedicated ServiceAccount `grug-smasher-launcher` bound
to a namespace-scoped Role granting exactly: `jobs` create/get/delete/list and
`pods` get/list (to read the termination message). NO secret read, NO exec, NO
deployment access — it cannot escalate. The launcher talks to the in-cluster
API over HTTPS (`kubernetes.default.svc`, port 443, already permitted by the
egress NetworkPolicy) using the mounted SA token + CA bundle via `httpx` — no
new heavyweight `kubernetes` client dependency, consistent with the repo's
hand-rolled-over-deps ethos (`diff_parser`, OSV-over-httpx).

### Network isolation and the flannel constraint (load-bearing)

The test phase MUST NOT reach the network (no exfiltration, no cluster-internal
SSRF to metadata/other-namespace services). We ship a `NetworkPolicy` selecting
the Trial Job pods that denies ALL egress in the test phase (DNS + 443 open only
in the init phase, keyed by a pod label the init/test split cannot itself
forge — the policy is per-pod so both phases share it; init's egress need is
satisfied by the same allow-DNS+443 rule, and the test phase simply makes no
allowed calls... **this is not sufficient alone** — see below).

**CRITICAL PRECONDITION.** `NetworkPolicy` is only enforced by a policy-capable
CNI. The current OKE cluster runs **flannel**, which does NOT enforce it (the
existing `k8s/networkpolicy.yaml` documents the same caveat). On flannel the
egress-deny is inert and the test phase CAN reach the network. Therefore:

- The **load-bearing** isolation is CREDENTIAL DENIAL, which is
  CNI-independent: the test phase has no SA token and no secrets, so even with
  network it has nothing to exfiltrate beyond the repo contents the PR author
  already controls, and it cannot authenticate to any cluster service.
- Cluster-internal SSRF (metadata endpoint, other pods) is the residual risk on
  a non-policy CNI. Enabling Smasher therefore REQUIRES a policy-enforcing CNI
  (Calico/Cilium) as an operator precondition, documented in `docs/SELF_HOST.md`
  and the per-repo enable note. The design does not silently claim network
  isolation it cannot enforce on flannel.
- Trust framing (from the pre-implementation design): enable Smasher only where
  PR authors are trusted at the level of "may run code in the sandbox." It is
  `smasher_enabled` default OFF, per-repo opt-in, plus a global SSM kill switch.

### Persona packaging

Smasher joins the registry (ADR-0010): `key=smasher`, canonical `smasher`,
check-run `Grug — Smasher`, `smasher_enabled` default OFF, no blocking mode
(mutation findings are inherently advisory), `dispatch_style=async` (like Elder
and Guard — the Job round-trip is far over the ACK budget). Survived mutants
become ordinary `Finding`s published through the SHARED Guard/Elder publish
path (check-run + inline review, advisory). This is the THIRD async persona,
re-confirming the rule-of-three trigger for the async-machinery extraction
(#77); this slice follows the established per-persona pattern (Elder/Guard
enqueue+run functions) rather than doing that risky refactor inside a
security-critical change — the extraction is its own slice.

### Kill switches (all fail-open to no-Trial)

`smasher_enabled` per-repo (default OFF), a global SSM flag
`/grug/smasher-enabled` (fallback-safe -> off), the mutant cap, per-mutant
timeout, and the total wall-clock budget (`activeDeadlineSeconds`). Any error
anywhere degrades to a neutral advisory check ("no lies", ADR-0003) — a Trial
that cannot run never blocks a PR.

## Consequences

- The webhook pod now mounts a ServiceAccount token (for the launcher SA). The
  blast radius is bounded to `jobs`/`pods` verbs in the one namespace with no
  secret read — a compromised webhook could create locked-down Jobs, not
  escalate. This is the accepted cost of in-cluster Job launching.
- Smasher cannot be safely enabled on a flannel-only cluster; a policy CNI is a
  hard precondition. Until then Smasher stays OFF (its default) and the built
  code is inert.
- Mutation is AST-based (`ast.parse` -> mutate one node -> `ast.unparse`) so a
  mutant is always syntactically valid — a mutant that failed to parse would be
  a false "killed" and inflate confidence. Python-only (matches the issue's
  scope; other ecosystems are out of scope).
- Out of scope (deepening follow-ups, #346 P3.2/P3.3): property-test
  scaffolding, fuzzing, distributed-systems simulation.
