# MIRRORED — sibling at services/api/personas/smasher/sandbox.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""The Smasher Trial sandbox — PURE manifest builder + result parser (#469, ADR-0013).

Two pure surfaces, both fully unit-lockable (no cluster, no IO):

  - `build_trial_job(...)` renders the locked-down k8s Job manifest that runs
    PR-author code. The security boundary lives ENTIRELY in this manifest's
    shape (see ADR-0013); the tests assert every boundary property.
  - `parse_trial_result(termination_message)` decodes the worker's JSON summary
    read back from the pod termination message. Malformed/absent -> degraded.

Plus `extract_target_lines(hunks)` — the diff's added Python lines, the mutation
targets. Non-Python files are skipped (the tracer is Python-only, #346 P3.1).

The Job's containers run from the SAME grug-webhook image (so `personas.smasher`
is importable inside the Job):
  - initContainer `fetch` — the ONLY token holder. Downloads the repo tarball
    at the head SHA (GitHub API, no git binary, no on-disk token) into the
    workspace. Token is an env var on THIS container only.
  - initContainer `deps` — NO token. Installs test deps (network) into the
    workspace. Author build code may run here, but there is no credential to
    steal.
  - container `test` — NO token, NO secrets, read-only rootfs, non-root, caps
    dropped, resource-limited. Runs the mutation worker offline-by-construction
    and writes the result to /dev/termination-log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from personas.code_reviewer.diff_parser import DiffHunk


@dataclass(frozen=True, slots=True)
class SurvivedMutant:
    """One mutant the repo's tests did NOT catch — an executable coverage gap."""

    file: str
    line: int
    operator: str
    original: str
    mutated: str


@dataclass(frozen=True, slots=True)
class TrialResult:
    """Decoded Trial outcome. `status` is `completed` (the worker ran to a
    verdict) or `degraded` (the Job produced no usable result — a fetch/parse
    failure, timeout, or crash; degraded is NEVER a pass, ADR-0003)."""

    status: str
    total: int
    killed: int
    survived: tuple[SurvivedMutant, ...]
    timed_out: int = 0
    errored: int = 0


def extract_target_lines(hunks: tuple[DiffHunk, ...]) -> dict[str, list[int]]:
    """Map each changed `*.py` file to the sorted new-side line numbers the diff
    ADDED. Only added lines are mutation targets — a PR review measures the
    coverage of what the PR introduces. Non-Python files are omitted."""
    by_file: dict[str, set[int]] = {}
    for hunk in hunks:
        if not hunk.file_path.endswith(".py"):
            continue
        added = _added_line_numbers(hunk)
        if added:
            by_file.setdefault(hunk.file_path, set()).update(added)
    return {path: sorted(lines) for path, lines in by_file.items()}


def _added_line_numbers(hunk: DiffHunk) -> list[int]:
    """New-side line numbers of the ADDED (`+`) lines in one hunk. Advances the
    counter on added/context lines, not removed lines (unified-diff semantics).
    The first body line is the `@@` header (skipped)."""
    out: list[int] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith("@@") or raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.append(lineno)
            lineno += 1
        elif raw.startswith("-"):
            continue
        else:
            lineno += 1
    return out


# The tarball fetch endpoint the `fetch` init container hits. Kept here (not in
# the runner) so the manifest and the fetcher can't drift on the path shape.
_TARBALL_PATH = "/repos/{owner}/{repo}/tarball/{ref}"


def build_trial_job(
    *,
    job_name: str,
    image: str,
    owner: str,
    repo: str,
    head_sha: str,
    token: str,
    targets: dict[str, list[int]],
    total_budget_seconds: int,
    per_mutant_timeout_seconds: int,
    mutant_cap: int,
    service_account_name: str = "grug-smasher-launcher",
) -> dict[str, Any]:
    """Render the locked-down Trial Job manifest (ADR-0013). PURE.

    Boundary invariants asserted by test_smasher_sandbox.py: no pod SA token;
    the GitHub token reaches ONLY the `fetch` init container; the `test`
    container has no secrets, read-only rootfs, non-root, caps dropped, resource
    limits; `activeDeadlineSeconds`/`restartPolicy=Never`/`backoffLimit=0` bound
    runaways; result via the termination message; pod labelled for the
    egress-deny NetworkPolicy."""
    targets_json = json.dumps(targets, sort_keys=True)
    workspace = {"name": "workspace", "mountPath": "/workspace"}
    hardened_sc = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    limits = {"cpu": "1", "memory": "1Gi"}

    fetch_init = {
        "name": "fetch",
        "image": image,
        "command": ["python", "-m", "personas.smasher.trial_fetch"],
        "env": [
            # The token lives ONLY here. Used for the one authenticated tarball
            # request; never written to disk (no git remote to persist it).
            {"name": "GRUG_TRIAL_TOKEN", "value": token},
            {"name": "GRUG_TRIAL_REPO", "value": f"{owner}/{repo}"},
            {"name": "GRUG_TRIAL_REF", "value": head_sha},
            {"name": "GRUG_TRIAL_TARBALL_PATH",
             "value": _TARBALL_PATH.format(owner=owner, repo=repo, ref=head_sha)},
        ],
        "securityContext": hardened_sc,
        "resources": {"limits": limits, "requests": {"cpu": "100m", "memory": "128Mi"}},
        "volumeMounts": [workspace],
    }
    deps_init = {
        "name": "deps",
        "image": image,
        # NO token. Author build backends may run here; there is nothing to steal.
        "command": ["python", "-m", "personas.smasher.trial_deps"],
        "env": [{"name": "GRUG_TRIAL_WORKSPACE", "value": "/workspace"}],
        "securityContext": hardened_sc,
        "resources": {"limits": limits, "requests": {"cpu": "100m", "memory": "256Mi"}},
        "volumeMounts": [workspace],
    }
    test_container = {
        "name": "test",
        "image": image,
        # NO token, NO envFrom secret. This is the phase that runs author code.
        "command": ["python", "-m", "personas.smasher.trial_worker"],
        "env": [
            {"name": "GRUG_TRIAL_WORKSPACE", "value": "/workspace"},
            {"name": "GRUG_TRIAL_TARGETS", "value": targets_json},
            {"name": "GRUG_TRIAL_MUTANT_CAP", "value": str(mutant_cap)},
            {"name": "GRUG_TRIAL_PER_MUTANT_TIMEOUT", "value": str(per_mutant_timeout_seconds)},
        ],
        "securityContext": hardened_sc,
        "resources": {"limits": limits, "requests": {"cpu": "250m", "memory": "256Mi"}},
        "volumeMounts": [workspace],
        "terminationMessagePath": "/dev/termination-log",
        "terminationMessagePolicy": "File",
    }

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": "grug",
            "labels": {"app": "grug-trial", "grug-trial": "true"},
        },
        "spec": {
            # The kubelet-enforced kill switch: no matter what author code does,
            # the whole Job dies at the budget.
            "activeDeadlineSeconds": total_budget_seconds,
            "backoffLimit": 0,        # never re-run author code on failure
            "ttlSecondsAfterFinished": 300,  # self-clean finished Jobs
            "template": {
                "metadata": {
                    "labels": {"app": "grug-trial", "grug-trial": "true"},
                },
                "spec": {
                    # The load-bearing credential-denial: the code under test
                    # gets NO Kubernetes token.
                    "automountServiceAccountToken": False,
                    "serviceAccountName": service_account_name,
                    "restartPolicy": "Never",
                    "nodeSelector": {"kubernetes.io/arch": "arm64"},
                    "initContainers": [fetch_init, deps_init],
                    "containers": [test_container],
                    "volumes": [
                        {"name": "workspace", "emptyDir": {"sizeLimit": "512Mi"}},
                    ],
                },
            },
        },
    }


def parse_trial_result(termination_message: str | None) -> TrialResult:
    """Decode the worker's JSON summary from the pod termination message.

    Never raises: an absent / non-JSON / wrong-shape message degrades to a
    `TrialResult(status="degraded")` (ADR-0003 "no lies" — a Trial that
    produced no usable verdict is advisory-neutral, never a false pass)."""
    if not termination_message:
        return _degraded()
    try:
        data = json.loads(termination_message)
    except (json.JSONDecodeError, TypeError):
        return _degraded()
    if not isinstance(data, dict):
        return _degraded()

    survived: list[SurvivedMutant] = []
    for row in data.get("survived", []) or []:
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
            # Drop a malformed survivor row — never let one bad row sink the
            # whole result (the survivors are advisory findings).
            continue

    status = data.get("status")
    if status not in ("completed", "degraded"):
        status = "degraded"
    return TrialResult(
        status=status,
        total=_as_int(data.get("total")),
        killed=_as_int(data.get("killed")),
        survived=tuple(survived),
        timed_out=_as_int(data.get("timed_out")),
        errored=_as_int(data.get("errored")),
    )


def _degraded() -> TrialResult:
    return TrialResult(status="degraded", total=0, killed=0, survived=())


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
