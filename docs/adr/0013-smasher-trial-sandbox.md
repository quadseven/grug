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

### Execution vessel: a TWO-POD split per Trial run

The webhook (which already processes the untrusted webhook payload but holds no
code-execution surface) LAUNCHES the pods and never runs author code itself. The
work is split across TWO Jobs sharing one PVC so the network-having phase and
the author-code phase are DIFFERENT pods with DIFFERENT egress policies (a
pod-level NetworkPolicy cannot distinguish containers within one pod — the
original single-Job design could not deny the test phase network without also
denying the fetch phase; peer review PR #494 flagged this, and the split closes
it):

- **prep pod** (`grug-trial-phase: prep`, egress DNS+443):
  - init `fetch` — the ONLY holder of a short-lived credential (a
    `contents:read`-scoped, single-repo GitHub token via a per-Job Secret
    `secretKeyRef`, NOT inlined into the spec/etcd). Downloads the repo tarball
    at the head SHA into the shared PVC.
  - main `deps` — NO token. Wheel-only installs test deps into the PVC (wheel
    extraction runs no author build backends, so NO author code runs in prep).
- **test pod** (`grug-trial-phase: test`, DENY-ALL egress):
  - `test` — NO token, NO secrets, DENY-ALL egress (deps are vendored on the
    PVC, so it needs no network), `runAsNonRoot`, all caps dropped,
    `seccompProfile: RuntimeDefault`, CPU/memory limits. Runs the mutation
    worker; this is the ONLY phase author code executes, now fully
    network-jailed (on a policy CNI) and holding nothing to steal.
  - The PVC (pristine checkout + vendored deps) is mounted READ-ONLY here, so
    author pytest — same UID, same volume — is KERNEL-BLOCKED from writing back
    to `/workspace/repo` to poison the source of the per-mutant copies
    (peer-review PR #494; enforced, not by-convention). Each baseline/mutant is
    copied into a separate WRITABLE `scratch` emptyDir and run there; the copy is
    discarded after, so no run can pollute another.
- The **shared PVC** (node-local `local-path`, `WaitForFirstConsumer`) carries
  the checkout+deps between the pods; the binding pins the test pod to the prep
  pod's node. The launcher creates the PVC + Secret, runs prep, DROPS the Secret
  once prep succeeds (before the author-code pod starts), runs test, reads the
  result, and reaps the PVC/Secret/Jobs.
- `automountServiceAccountToken: false` on BOTH pods — no Kubernetes credential.
- `activeDeadlineSeconds` per Job = the wall-clock budget: the kubelet kills a
  runaway regardless of what the code under test does.

### Result channel: the pod termination message, NOT logs

BYON worker nodes' kubelet API (`:10250`) is unreachable from the control
plane, so `kubectl logs` / the logs API cannot read the Job's output (a live
constraint, see `feedback` memory on BYON). Instead the worker writes its
survived-mutant summary as JSON to `/dev/termination-log`; the launcher reads
it back via `pod.status.containerStatuses[].state.terminated.message` (the
Kubernetes API, no kubelet access, 4 KiB cap — survived-mutant summaries fit).

### Launcher RBAC + namespace isolation (the escalation fix)

The webhook + consumer pods mount a dedicated ServiceAccount
`grug-smasher-launcher`. Its permissions live ENTIRELY in a SEPARATE, dedicated
`grug-trial` namespace — NOT in `grug` — granting exactly `jobs`
create/get/list/delete, `pods` get/list (to read the termination message),
`secrets` create/get/delete (for the per-Job token Secret), and
`persistentvolumeclaims` create/get/delete (the shared workspace). It has ZERO
permissions in the `grug` namespace.

Why the separate namespace matters (this is load-bearing): `create jobs`
combined with the ability to set an arbitrary `serviceAccountName` on the
created pod is a known Kubernetes escalation primitive — the creator can launch
a Job whose pod mounts ANY ServiceAccount token in that namespace. In `grug`
that would include `grug-key-rotator` (which can patch the `grug-secrets` AWS
credential). By running Trial Jobs in `grug-trial` — which holds NO secrets and
NO privileged ServiceAccounts (only the permissionless `default` SA) — there is
nothing worth borrowing, so the primitive is inert. This isolation is
CNI-independent and does not rely on any admission controller. Trial pods run as
the `grug-trial` `default` SA with `automountServiceAccountToken:false`.

The launcher talks to the in-cluster API over HTTPS (`kubernetes.default.svc`,
port 443, already permitted by the `grug` egress NetworkPolicy) using the
mounted SA token + CA bundle via `httpx` — no new heavyweight `kubernetes`
client dependency, consistent with the repo's hand-rolled-over-deps ethos
(`diff_parser`, OSV-over-httpx).

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

### Accepted residuals (peer-review PR #494)

Cross-model peer review (codex, poolside, spark) surfaced three residuals that
are INHERENT to the pre-agreed single-Job mutation-testing design, not
defects. They are accepted under Smasher's trust model (default OFF, per-repo
opt-in only where PR authors may "run code in the sandbox", advisory-only,
policy-CNI precondition, no credential in the test phase):

- **Test-phase egress — FIXED (was a residual).** The two-pod split (above) puts
  the author-code phase in its own pod with a DENY-ALL egress NetworkPolicy;
  deps are already vendored on the PVC so it needs no network. On a policy CNI
  the test phase is genuinely offline. (On flannel the policy is inert like all
  the others, but credential-denial + no-network-need still hold.)
- **Oracle trust.** Mutation testing measures the repo's OWN test suite as the
  oracle, so the test suite's exit code IS author-controlled by definition — an
  author could add a source-hash-guard test to make every mutant "killed". This
  is intrinsic to mutation testing, not a Smasher bug; it is bounded by
  advisory-only + trusted-authors, and gaming it is self-defeating (the author
  is the party who wanted the coverage signal).
- **Persistent launcher token.** The launcher SA token is mounted on
  webhook/consumer whenever they run, not only when Smasher is enabled. Its
  permissions are confined to the secret-free `grug-trial` namespace, so the
  added surface over an already-compromised webhook is marginal (create
  locked-down Jobs in a namespace with nothing to steal). The launch PATH is
  still flag-gated (`dispatch_smasher_review` checks the global master switch
  before minting a token or creating a Job).

The one NON-inherent integrity hole peer review found — an author test
daemonizing a process to OVERWRITE the termination message after the worker's
authoritative write — IS fixed: the worker reaps every other process in its PID
namespace before the authoritative write (`trial_worker._reap_other_processes`).

### Kill switches (all fail-open to no-Trial)

`smasher_enabled` per-repo (default OFF), a global SSM flag
`/grug/smasher-enabled` (fallback-safe -> off), the mutant cap, per-mutant
timeout, and the total wall-clock budget (`activeDeadlineSeconds`). Any error
anywhere degrades to a neutral advisory check ("no lies", ADR-0003) — a Trial
that cannot run never blocks a PR.

## Consequences

- The webhook + consumer pods now mount a ServiceAccount token (the launcher).
  The blast radius is bounded by the `grug-trial` namespace isolation: the token
  grants `jobs`/`pods`/`secrets` verbs ONLY in a secret-free, privileged-SA-free
  namespace and nothing in `grug`, so a compromised webhook cannot use it to
  reach `grug-secrets` or any real credential. This is the accepted cost of
  in-cluster Job launching. (An earlier draft granted the launcher `create jobs`
  in the `grug` namespace, which WAS a privilege-escalation path via
  serviceAccountName borrowing — caught in review and fixed by the dedicated
  namespace.)
- Smasher cannot be safely enabled on a flannel-only cluster; a policy CNI is a
  hard precondition. Until then Smasher stays OFF (its default) and the built
  code is inert.
- Mutation is AST-based (`ast.parse` -> mutate one node -> `ast.unparse`) so a
  mutant is always syntactically valid — a mutant that failed to parse would be
  a false "killed" and inflate confidence. Python-only (matches the issue's
  scope; other ecosystems are out of scope).
- Out of scope (deepening follow-ups, #346 P3.2/P3.3): property-test
  scaffolding, fuzzing, distributed-systems simulation.
