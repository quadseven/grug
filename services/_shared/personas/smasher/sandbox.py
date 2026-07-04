"""The Smasher Trial sandbox — PURE manifest builder + result parser (#469, ADR-0013).

Two pure surfaces, both fully unit-lockable (no cluster, no IO):

  - `build_trial_job(...)` renders the locked-down Trial Job manifest that runs
    PR-author code. The security boundary lives ENTIRELY in this manifest's
    shape (see ADR-0013); the tests assert every boundary property.
  - `parse_trial_result(termination_message)` decodes the worker's JSON summary
    read back from the pod termination message. It is a TRUST BOUNDARY: the
    message is written by author-controlled code (same container), so every
    field is validated + bounded here. Malformed/absent -> degraded.

Plus `extract_target_lines(hunks)` — the diff's added Python lines, the mutation
targets. Non-Python files are skipped (the tracer is Python-only, #346 P3.1).

Everything runs in the DEDICATED `grug-trial` namespace (ADR-0013), which holds
no secrets and no privileged ServiceAccounts, so even the launcher's `create
jobs` grant there cannot be used to borrow a privileged identity.

TWO-POD SPLIT (peer-review PR #494) so the network-having phase and the
author-code phase are DIFFERENT pods with DIFFERENT egress policies (pod-level
NetworkPolicy can't distinguish containers in one pod):
  - **prep pod** (label `grug-trial-phase: prep`, egress DNS+443): initContainer
    `fetch` is the ONLY token holder (scoped `contents:read` token via a per-Job
    Secret `secretKeyRef`, never inlined) and tarball-fetches the repo at the
    head SHA into a shared PVC; main container `deps` (NO token) wheel-installs
    test deps into the PVC. No author test code runs here (wheel-only install).
  - **test pod** (label `grug-trial-phase: test`, DENY-ALL egress): the `test`
    container runs the mutation worker over the SAME PVC with NO token, NO
    secrets, and NO network at all - the deps are already vendored, so it needs
    none. This is the phase that runs author code; it is fully network-jailed
    (on a policy CNI) and holds nothing to steal.
All containers: read-only rootfs (+ writable `/tmp` + the PVC), non-root, caps
dropped, resource limits. The PVC (node-local `local-path`) is what carries the
checkout+deps between the two pods; `WaitForFirstConsumer` pins the test pod to
the prep pod's node. Result: the `test` pod writes the survived-mutant summary
to /dev/termination-log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from personas.code_reviewer.diff_parser import DiffHunk

# The namespace Trial Jobs run in — isolated from the credential-bearing `grug`
# namespace (ADR-0013). No secrets, no privileged SAs live here.
TRIAL_NAMESPACE = "grug-trial"

# The mutation operators the engine can emit (mutate.py). Used to VALIDATE the
# operator field of a survived-mutant row read back from the untrusted
# termination message — an unknown value means a forged/corrupt message.
TrialStatus = Literal["completed", "degraded"]
VALID_OPERATORS: frozenset[str] = frozenset(
    {"comparison-flip", "boundary", "boolean", "return-value"}
)

# Bound on the free-text fields of a survived-mutant row (they flow into a
# Grug-authored check-run markdown table; the message is author-controllable so
# the length is capped to prevent table/markdown flooding). The whole message is
# already kubelet-capped at 4 KiB, so these are belt-and-braces.
_MAX_FIELD_LEN = 200


@dataclass(frozen=True, slots=True)
class SurvivedMutant:
    """One mutant the repo's tests did NOT catch — an executable coverage gap.

    Fields originate from the (untrusted) termination message, so construction
    is validated: `operator` must be a known operator, string fields are length-
    bounded, `line >= 1`. `parse_trial_result` drops rows that fail these."""

    file: str
    line: int
    operator: str
    original: str
    mutated: str

    def __post_init__(self) -> None:
        if self.operator not in VALID_OPERATORS:
            raise ValueError(f"unknown mutation operator {self.operator!r}")
        if self.line < 1:
            raise ValueError(f"line must be >= 1, got {self.line}")
        for name in ("file", "original", "mutated"):
            if len(getattr(self, name)) > _MAX_FIELD_LEN:
                raise ValueError(f"{name} exceeds {_MAX_FIELD_LEN} chars")


@dataclass(frozen=True, slots=True)
class TrialResult:
    """Decoded Trial outcome. `status` is `completed` (the worker ran to a
    verdict) or `degraded` (the Job produced no usable result — a fetch/parse
    failure, timeout, or crash; degraded is NEVER a pass, ADR-0003). `reason`
    names the degrade cause (surfaced to the operator/PR author); `truncated`
    flags that survivor rows were dropped to fit the 4 KiB channel. Counts are
    non-negative (clamped at parse)."""

    status: TrialStatus
    total: int
    killed: int
    survived: tuple[SurvivedMutant, ...]
    timed_out: int = 0
    errored: int = 0
    reason: str | None = None
    truncated: bool = False


def extract_target_lines(hunks: tuple[DiffHunk, ...]) -> dict[str, list[int]]:
    """Map each changed `*.py` file to the sorted new-side line numbers the diff
    ADDED. Only added lines are mutation targets — a PR review measures the
    coverage of what the PR introduces. Non-Python files, and any path that
    isn't a safe relative path (absolute or containing `..`), are omitted."""
    by_file: dict[str, set[int]] = {}
    for hunk in hunks:
        if not hunk.file_path.endswith(".py") or not _is_safe_relpath(hunk.file_path):
            continue
        added = _added_line_numbers(hunk)
        if added:
            by_file.setdefault(hunk.file_path, set()).update(added)
    return {path: sorted(lines) for path, lines in by_file.items()}


def _is_safe_relpath(path: str) -> bool:
    """True for a workspace-relative path with no traversal — rejects absolute
    paths and any `..` segment (the path originates from an attacker-controlled
    diff and is used to open files inside the sandbox workspace)."""
    if not path or path.startswith("/") or path.startswith("\\"):
        return False
    parts = path.replace("\\", "/").split("/")
    return ".." not in parts


def _added_line_numbers(hunk: DiffHunk) -> list[int]:
    """New-side line numbers of the ADDED (`+`) lines in one hunk. Advances the
    counter on added/context lines, not removed lines (unified-diff semantics).
    The first body line is the `@@` header (skipped). Only a SINGLE leading
    `+`/`-` marks add/remove — an added line whose content starts with `++`
    (e.g. `+  x = ++y`) must still count, so we branch on the first char."""
    out: list[int] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if not raw:
            lineno += 1
            continue
        if raw.startswith("@@"):
            continue
        marker = raw[0]
        if marker == "+":
            out.append(lineno)
            lineno += 1
        elif marker == "-":
            continue  # removed line: no new-side advance
        else:
            lineno += 1  # context line
    return out


# The tarball fetch endpoint the `fetch` init container hits. Kept here (not in
# the runner) so the manifest and the fetcher can't drift on the path shape.
_TARBALL_PATH = "/repos/{owner}/{repo}/tarball/{ref}"

# The storage class carrying the checkout+deps between the prep and test pods.
# Node-local + WaitForFirstConsumer, so the PV is pinned to the prep pod's node
# and the test pod (mounting the same PVC) is forced onto that same node.
_TRIAL_STORAGE_CLASS = "local-path"

_HARDENED_SC = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
    "seccompProfile": {"type": "RuntimeDefault"},
}
_LIMITS = {"cpu": "1", "memory": "1Gi"}


def build_trial_pvc(name: str) -> dict[str, Any]:
    """The per-Trial shared workspace PVC (PURE). Carries the checkout + vendored
    deps from the prep pod to the network-jailed test pod."""
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": TRIAL_NAMESPACE, "labels": {"app": "grug-trial"}},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": _TRIAL_STORAGE_CLASS,
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }


def _pod_meta(labels: dict[str, str]) -> dict[str, Any]:
    return {"labels": {"app": "grug-trial", **labels}}


def _workspace_mounts(*, workspace_readonly: bool = False) -> list[dict[str, Any]]:
    ws: dict[str, Any] = {"name": "workspace", "mountPath": "/workspace"}
    if workspace_readonly:
        ws["readOnly"] = True
    return [ws, {"name": "tmp", "mountPath": "/tmp"}]


def _job_shell(job_name: str, *, phase_label: str, budget: int, pod_spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": TRIAL_NAMESPACE,
            "labels": {"app": "grug-trial", "grug-trial-phase": phase_label},
        },
        "spec": {
            "activeDeadlineSeconds": budget,   # kubelet-enforced kill switch
            "backoffLimit": 0,                 # never re-run on failure
            "ttlSecondsAfterFinished": 300,
            "template": {"metadata": _pod_meta({"grug-trial-phase": phase_label}), "spec": pod_spec},
        },
    }


def _pvc_volumes(pvc_name: str) -> list[dict[str, Any]]:
    return [
        {"name": "workspace", "persistentVolumeClaim": {"claimName": pvc_name}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
    ]


def build_prep_job(
    *,
    job_name: str,
    image: str,
    owner: str,
    repo: str,
    head_sha: str,
    token_secret_name: str,
    pvc_name: str,
    total_budget_seconds: int,
) -> dict[str, Any]:
    """Prep pod (PURE): fetch the repo tarball (init `fetch`, the ONLY token
    holder) + wheel-install deps (`deps`, no token) into the shared PVC. Labelled
    `grug-trial-phase: prep` so the egress-DNS+443 NetworkPolicy selects it. NO
    author test code runs here (wheel-only install runs no build backends)."""
    fetch_init = {
        "name": "fetch",
        "image": image,
        "command": ["python", "-m", "personas.smasher.trial_fetch"],
        "env": [
            {"name": "GRUG_TRIAL_TOKEN",
             "valueFrom": {"secretKeyRef": {"name": token_secret_name, "key": "token"}}},
            {"name": "GRUG_TRIAL_WORKSPACE", "value": "/workspace"},
            {"name": "GRUG_TRIAL_TARBALL_PATH",
             "value": _TARBALL_PATH.format(owner=owner, repo=repo, ref=head_sha)},
        ],
        "securityContext": _HARDENED_SC,
        "resources": {"limits": _LIMITS, "requests": {"cpu": "100m", "memory": "128Mi"}},
        "volumeMounts": _workspace_mounts(),
    }
    deps_main = {
        "name": "deps",
        "image": image,
        "command": ["python", "-m", "personas.smasher.trial_deps"],
        "env": [{"name": "GRUG_TRIAL_WORKSPACE", "value": "/workspace"}],
        "securityContext": _HARDENED_SC,
        "resources": {"limits": _LIMITS, "requests": {"cpu": "100m", "memory": "256Mi"}},
        "volumeMounts": _workspace_mounts(),
    }
    pod_spec = {
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "nodeSelector": {"kubernetes.io/arch": "arm64"},
        "imagePullSecrets": [{"name": "registry-pull"}],
        "initContainers": [fetch_init],
        "containers": [deps_main],
        "volumes": _pvc_volumes(pvc_name),
    }
    return _job_shell(job_name, phase_label="prep", budget=total_budget_seconds, pod_spec=pod_spec)


def build_test_job(
    *,
    job_name: str,
    image: str,
    pvc_name: str,
    targets: dict[str, list[int]],
    total_budget_seconds: int,
    per_mutant_timeout_seconds: int,
    mutant_cap: int,
) -> dict[str, Any]:
    """Test pod (PURE): run the mutation worker over the prepped PVC. Labelled
    `grug-trial-phase: test` so the DENY-ALL-egress NetworkPolicy selects it -
    this is the phase that runs author code, and it is fully network-jailed (on
    a policy CNI) with NO token/secrets. Result via the termination message."""
    targets_json = json.dumps(targets, sort_keys=True)
    # The pristine checkout + vendored deps are mounted READ-ONLY, so author
    # pytest (same UID, same PVC) CANNOT write to `/workspace/repo` or the deps
    # to poison the source of the per-mutant copies (codex peer-review PR #494 -
    # kernel-enforced, not by-convention). Copies + the mutant edits go to a
    # WRITABLE `scratch` emptyDir instead.
    mounts = _workspace_mounts(workspace_readonly=True) + [{"name": "scratch", "mountPath": "/scratch"}]
    test_container = {
        "name": "test",
        "image": image,
        "command": ["python", "-m", "personas.smasher.trial_worker"],
        "env": [
            {"name": "GRUG_TRIAL_WORKSPACE", "value": "/workspace"},
            {"name": "GRUG_TRIAL_SCRATCH", "value": "/scratch"},
            {"name": "GRUG_TRIAL_TARGETS", "value": targets_json},
            {"name": "GRUG_TRIAL_MUTANT_CAP", "value": str(mutant_cap)},
            {"name": "GRUG_TRIAL_PER_MUTANT_TIMEOUT", "value": str(per_mutant_timeout_seconds)},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
        ],
        "securityContext": _HARDENED_SC,
        "resources": {"limits": _LIMITS, "requests": {"cpu": "250m", "memory": "256Mi"}},
        "volumeMounts": mounts,
        "terminationMessagePath": "/dev/termination-log",
        "terminationMessagePolicy": "File",
    }
    pod_spec = {
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "nodeSelector": {"kubernetes.io/arch": "arm64"},
        "imagePullSecrets": [{"name": "registry-pull"}],
        "containers": [test_container],
        "volumes": _pvc_volumes(pvc_name) + [{"name": "scratch", "emptyDir": {"sizeLimit": "1Gi"}}],
    }
    return _job_shell(job_name, phase_label="test", budget=total_budget_seconds, pod_spec=pod_spec)


def parse_trial_result(termination_message: str | None) -> TrialResult:
    """Decode the worker's JSON summary from the pod termination message.

    TRUST BOUNDARY: the message is written by author-controlled code, so every
    field is validated + bounded. Never raises: an absent / non-JSON / wrong-
    shape / forged message degrades to `TrialResult(status="degraded")` (ADR-0003
    "no lies" — a Trial that produced no usable verdict is advisory-neutral,
    never a false pass)."""
    if not termination_message:
        return _degraded("no_termination_message")
    try:
        data = json.loads(termination_message)
    except (json.JSONDecodeError, TypeError):
        return _degraded("unparseable_termination_message")
    if not isinstance(data, dict):
        return _degraded("malformed_termination_message")

    raw_survived = data.get("survived")
    if raw_survived is None:
        raw_survived = []
    if not isinstance(raw_survived, list):
        # A non-list `survived` (forged `{"survived": 5}`) would crash a naive
        # iteration — degrade instead.
        return _degraded("malformed_termination_message")

    survived: list[SurvivedMutant] = []
    for row in raw_survived:
        if not isinstance(row, dict):
            continue
        try:
            survived.append(
                SurvivedMutant(
                    file=str(row["file"]),
                    line=int(row["line"]),
                    operator=str(row["operator"]),
                    original=str(row["original"]),
                    mutated=str(row["mutated"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            # Drop a malformed/forged survivor row — never let one bad row sink
            # the whole result (the survivors are advisory findings).
            continue

    status = data.get("status")
    if status not in ("completed", "degraded"):
        status = "degraded"
    return TrialResult(
        status=status,
        total=_as_nonneg_int(data.get("total")),
        killed=_as_nonneg_int(data.get("killed")),
        survived=tuple(survived),
        timed_out=_as_nonneg_int(data.get("timed_out")),
        errored=_as_nonneg_int(data.get("errored")),
        reason=(str(data["reason"])[:_MAX_FIELD_LEN] if data.get("reason") else None),
        truncated=bool(data.get("truncated", False)),
    )


def _degraded(reason: str) -> TrialResult:
    return TrialResult(status="degraded", total=0, killed=0, survived=(), reason=reason)


def _as_nonneg_int(value: Any) -> int:
    """Coerce to a non-negative int (a forged negative count is clamped to 0)."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
