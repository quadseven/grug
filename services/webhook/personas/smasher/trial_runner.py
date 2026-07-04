"""Trial Job launcher (#469, ADR-0013) — WEBHOOK-ONLY (not mirrored).

Submits the locked-down Trial Job to the in-cluster Kubernetes API, waits for it
to finish (bounded by the total budget), reads the survived-mutant summary back
from the pod termination message, and deletes the Job. Never raises: any
cluster / credential / timeout failure returns a degraded `TrialResult` so the
persona degrades to an advisory-neutral check.

Runs the Job in the DEDICATED `grug-trial` namespace (ADR-0013) — isolated from
the credential-bearing `grug` namespace, so the launcher's `create jobs` grant
there cannot borrow a privileged ServiceAccount. The scoped GitHub token is
handed to the Job via a per-Job Secret (created here, deleted with the Job),
never inlined into the Job spec.

Talks to `kubernetes.default.svc` over httpx with the launcher SA's mounted
token + CA bundle (no heavyweight `kubernetes` client dependency, consistent
with the repo's hand-rolled-over-deps ethos).
"""

from __future__ import annotations

import hashlib
import logging
import os
import ssl
import time
from typing import Any, Protocol

import httpx

from personas.smasher.sandbox import (
    TRIAL_NAMESPACE,
    TrialResult,
    build_prep_job,
    build_test_job,
    build_trial_pvc,
    parse_trial_result,
)

log = logging.getLogger("grug.smasher.trial_runner")

_API = "https://kubernetes.default.svc"
_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_TOKEN_PATH = f"{_SA_DIR}/token"
_CA_PATH = f"{_SA_DIR}/ca.crt"
_POLL_INTERVAL_SECONDS = 5
# Slack over the Job's own activeDeadlineSeconds so we outlive the kubelet kill
# and can still read the (partial) termination message before deleting.
_WAIT_SLACK_SECONDS = 60
_IMAGE_ENV = "GRUG_SMASHER_JOB_IMAGE"


class Cluster(Protocol):
    """The k8s operations the runner needs — injectable so the launch flow is
    testable without an API server."""

    def create_secret(self, name: str, token: str) -> None: ...
    def delete_secret(self, name: str) -> None: ...
    def create_pvc(self, manifest: dict[str, Any]) -> None: ...
    def delete_pvc(self, name: str) -> None: ...
    def create_job(self, manifest: dict[str, Any]) -> None: ...
    def wait_for_completion(self, job_name: str, timeout: int) -> str: ...
    def read_termination_message(self, job_name: str) -> str | None: ...
    def delete_job(self, job_name: str) -> None: ...


def _attempt_id() -> str:
    """Short unique suffix so concurrent Trial attempts for the same head SHA
    (e.g. a webhook run + an operator rerun) get DISTINCT resource names and
    never destructively collide. `os.urandom` (not a clock) so it works in the
    resume-safe runtime."""
    return os.urandom(4).hex()


def _job_name(owner: str, repo: str, head_sha: str) -> str:
    """Repo-qualified, collision-free Job name. Two PRs sharing a head SHA (a
    fork PR + base PR of the same commit) must NOT collide onto one Job name —
    the pre-create delete would otherwise kill a concurrently-running Trial for
    the other PR. Hash `owner/repo` into the name. Stays within the 63-char
    k8s name limit and the lowercase-alphanumeric-plus-dash charset."""
    repo_hash = hashlib.sha1(f"{owner}/{repo}".encode()).hexdigest()[:8]
    return f"grug-trial-{repo_hash}-{head_sha[:12].lower()}"


def launch_trial(
    *,
    owner: str,
    repo: str,
    head_sha: str,
    token: str,
    targets: dict[str, list[int]],
    mutant_cap: int,
    per_mutant_timeout_seconds: int,
    total_budget_seconds: int,
    image: str | None = None,
    cluster: Cluster | None = None,
) -> TrialResult:
    """Run one Trial in a sandbox Job and return its result. Never raises.

    `cluster`/`image` are injectable for tests; in production both default from
    the pod's mounted SA creds + `GRUG_SMASHER_JOB_IMAGE`."""
    if cluster is None:
        built = _default_cluster()
        if built is None:
            log.error("smasher_no_launcher_credentials")  # permanent operator fault
            return _degraded("no_launcher_credentials")
        cluster = built

    image = image or os.getenv(_IMAGE_ENV, "")
    if not image:
        log.error("smasher_no_job_image_configured")  # permanent operator fault
        return _degraded("no_job_image_configured")

    # Per-ATTEMPT unique names (codex peer-review PR #494): deterministic
    # head-SHA names + a delete-before-create would let a same-head rerun
    # (the rerun lane bypasses the head-SHA claim) destroy an in-flight Trial's
    # Jobs/PVC/Secret. A unique attempt suffix means concurrent attempts never
    # collide; each cleans up ONLY its own resources in the finally below. (A
    # webhook-pod crash mid-run leaks this attempt's PVC/Secret - bounded + all
    # carry the `app: grug-trial` label for a future age-based janitor; the Jobs
    # self-clean via ttlSecondsAfterFinished.)
    base = f"{_job_name(owner, repo, head_sha)}-{_attempt_id()}"
    prep_job, test_job = f"{base}-prep", f"{base}-test"
    secret_name, pvc_name = f"{base}-token", f"{base}-ws"
    budget = total_budget_seconds

    def _cleanup() -> None:
        # Order: Jobs (release the PVC mount) before the PVC, then the Secret.
        _swallow(lambda: cluster.delete_job(test_job))
        _swallow(lambda: cluster.delete_job(prep_job))
        _swallow(lambda: cluster.delete_pvc(pvc_name))
        _swallow(lambda: cluster.delete_secret(secret_name))

    try:
        cluster.create_secret(secret_name, token)
        cluster.create_pvc(build_trial_pvc(pvc_name))
        cluster.create_job(build_prep_job(
            job_name=prep_job, image=image, owner=owner, repo=repo, head_sha=head_sha,
            token_secret_name=secret_name, pvc_name=pvc_name, total_budget_seconds=budget,
        ))
    except Exception as e:  # noqa: BLE001 — submit failure degrades, never raises
        log.warning("smasher_prep_create_failed", extra={"kind": type(e).__name__})
        _cleanup()
        return _degraded("prep_create_failed")

    message: str | None = None
    try:
        # Phase 1: prep (fetch + wheel-install into the PVC). The token Secret is
        # only needed until fetch is done; drop it before the test pod runs so
        # the author-code phase can't even reach the Secret object.
        prep_phase = cluster.wait_for_completion(prep_job, budget + _WAIT_SLACK_SECONDS)
        _swallow(lambda: cluster.delete_secret(secret_name))
        if prep_phase != "Succeeded":
            log.warning("smasher_prep_failed", extra={"phase": prep_phase})
            return _degraded("prep_failed")

        # Phase 2: test (mutation worker, network-denied). Author code runs here.
        cluster.create_job(build_test_job(
            job_name=test_job, image=image, pvc_name=pvc_name, targets=targets,
            total_budget_seconds=budget, per_mutant_timeout_seconds=per_mutant_timeout_seconds,
            mutant_cap=mutant_cap,
        ))
        cluster.wait_for_completion(test_job, budget + _WAIT_SLACK_SECONDS)
        message = cluster.read_termination_message(test_job)
    except Exception as e:  # noqa: BLE001 — read/wait failure degrades
        log.warning("smasher_job_wait_or_read_failed", extra={"kind": type(e).__name__})
    finally:
        _cleanup()

    return parse_trial_result(message)


def _degraded(reason: str) -> TrialResult:
    return TrialResult(status="degraded", total=0, killed=0, survived=(), reason=reason)


def _swallow(fn) -> None:
    """Run a best-effort cleanup/pre-delete; a failure (e.g. 404 no prior
    object) is logged at debug and never propagates."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        log.debug("smasher_cleanup_noop", extra={"kind": type(e).__name__})


def _default_cluster() -> "_HttpxCluster | None":
    """Build the real in-cluster client from the mounted SA creds, or None when
    not running in a pod with the launcher token (local/test/misconfigured)."""
    try:
        with open(_TOKEN_PATH, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        return None
    if not token or not os.path.exists(_CA_PATH):
        return None
    return _HttpxCluster(token=token, ca_path=_CA_PATH)


class _HttpxCluster:
    """Real Kubernetes API client over httpx (the launcher SA token + CA)."""

    def __init__(self, *, token: str, ca_path: str) -> None:
        # ssl context (not verify=<path>, deprecated in httpx 0.28+); one reused
        # client instead of a fresh TLS handshake per poll.
        ctx = ssl.create_default_context(cafile=ca_path)
        self._client = httpx.Client(
            verify=ctx,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        self._jobs = f"{_API}/apis/batch/v1/namespaces/{TRIAL_NAMESPACE}/jobs"
        self._pods = f"{_API}/api/v1/namespaces/{TRIAL_NAMESPACE}/pods"
        self._secrets = f"{_API}/api/v1/namespaces/{TRIAL_NAMESPACE}/secrets"
        self._pvcs = f"{_API}/api/v1/namespaces/{TRIAL_NAMESPACE}/persistentvolumeclaims"

    def create_secret(self, name: str, token: str) -> None:
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": TRIAL_NAMESPACE,
                "labels": {"app": "grug-trial"},
            },
            "type": "Opaque",
            # stringData: k8s base64-encodes it; the raw token never appears in
            # our code path as base64 and is deleted with the Job.
            "stringData": {"token": token},
        }
        self._client.post(self._secrets, json=manifest).raise_for_status()

    def delete_secret(self, name: str) -> None:
        resp = self._client.request("DELETE", f"{self._secrets}/{name}")
        if resp.status_code not in (200, 202, 404):
            resp.raise_for_status()

    def create_pvc(self, manifest: dict[str, Any]) -> None:
        self._client.post(self._pvcs, json=manifest).raise_for_status()

    def delete_pvc(self, name: str) -> None:
        resp = self._client.request("DELETE", f"{self._pvcs}/{name}")
        if resp.status_code not in (200, 202, 404):
            resp.raise_for_status()

    def create_job(self, manifest: dict[str, Any]) -> None:
        self._client.post(self._jobs, json=manifest).raise_for_status()

    def wait_for_completion(self, job_name: str, timeout: int) -> str:
        """Poll the Job until it Completes or Fails, or the timeout elapses.
        Returns the terminal phase ("Succeeded"/"Failed"/"Unknown")."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._client.get(f"{self._jobs}/{job_name}", timeout=10)
            resp.raise_for_status()
            status = resp.json().get("status", {})
            if status.get("succeeded"):
                return "Succeeded"
            if status.get("failed"):
                return "Failed"
            time.sleep(_POLL_INTERVAL_SECONDS)
        return "Unknown"

    def read_termination_message(self, job_name: str) -> str | None:
        """Read the test container's termination message from the Job's pod."""
        resp = self._client.get(
            self._pods, params={"labelSelector": f"job-name={job_name}"}, timeout=10,
        )
        resp.raise_for_status()
        for pod in resp.json().get("items", []):
            for cs in pod.get("status", {}).get("containerStatuses", []):
                if cs.get("name") != "test":
                    continue
                terminated = (cs.get("state", {}) or {}).get("terminated", {}) or {}
                msg = terminated.get("message")
                if msg:
                    return msg
        return None

    def delete_job(self, job_name: str) -> None:
        resp = self._client.request(
            "DELETE", f"{self._jobs}/{job_name}",
            params={"propagationPolicy": "Background"}, timeout=10,
        )
        if resp.status_code not in (200, 202, 404):
            resp.raise_for_status()
