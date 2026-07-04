"""Trial Job launcher (#469, ADR-0013) — WEBHOOK-ONLY (not mirrored).

Submits the locked-down Trial Job to the in-cluster Kubernetes API, waits for it
to finish (bounded by the total budget), reads the survived-mutant summary back
from the pod termination message, and deletes the Job. Never raises: any
cluster / credential / timeout failure returns a degraded `TrialResult` so the
persona degrades to an advisory-neutral check.

Talks to `kubernetes.default.svc` over httpx with the launcher SA's mounted
token + CA bundle (no heavyweight `kubernetes` client dependency, consistent
with the repo's hand-rolled-over-deps ethos). The launcher SA
(`grug-smasher-launcher`) is scoped to `jobs` + `pods` verbs only - it cannot
read secrets or escalate (ADR-0013).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Protocol

import httpx

from personas.smasher.sandbox import (
    TrialResult,
    build_trial_job,
    parse_trial_result,
)

log = logging.getLogger("grug.smasher.trial_runner")

_NAMESPACE = "grug"
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

    def create_job(self, manifest: dict[str, Any]) -> None: ...
    def wait_for_completion(self, job_name: str, timeout: int) -> str: ...
    def read_termination_message(self, job_name: str) -> str | None: ...
    def delete_job(self, job_name: str) -> None: ...


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
            log.warning("smasher_no_launcher_credentials")
            return _degraded()
        cluster = built

    image = image or os.getenv(_IMAGE_ENV, "")
    if not image:
        log.warning("smasher_no_job_image_configured")
        return _degraded()

    job_name = f"grug-trial-{head_sha[:12]}"
    manifest = build_trial_job(
        job_name=job_name,
        image=image,
        owner=owner,
        repo=repo,
        head_sha=head_sha,
        token=token,
        targets=targets,
        total_budget_seconds=total_budget_seconds,
        per_mutant_timeout_seconds=per_mutant_timeout_seconds,
        mutant_cap=mutant_cap,
    )

    try:
        # A prior run at the same head SHA may have left a Job (name collision);
        # delete first so create doesn't 409. Best-effort.
        try:
            cluster.delete_job(job_name)
        except Exception:  # noqa: BLE001 — no prior Job is the common case
            pass
        cluster.create_job(manifest)
    except Exception as e:  # noqa: BLE001 — submit failure degrades, never raises
        log.warning("smasher_job_create_failed", extra={"kind": type(e).__name__})
        return _degraded()

    message: str | None = None
    try:
        cluster.wait_for_completion(job_name, total_budget_seconds + _WAIT_SLACK_SECONDS)
        message = cluster.read_termination_message(job_name)
    except Exception as e:  # noqa: BLE001 — read/wait failure degrades
        log.warning("smasher_job_wait_or_read_failed", extra={"kind": type(e).__name__})
    finally:
        # Delete is best-effort; ttlSecondsAfterFinished is the backstop.
        try:
            cluster.delete_job(job_name)
        except Exception as e:  # noqa: BLE001
            log.info("smasher_job_delete_failed", extra={"kind": type(e).__name__})

    return parse_trial_result(message)


def _degraded() -> TrialResult:
    return TrialResult(status="degraded", total=0, killed=0, survived=())


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
        self._headers = {"Authorization": f"Bearer {token}"}
        self._verify = ca_path
        self._jobs = f"{_API}/apis/batch/v1/namespaces/{_NAMESPACE}/jobs"
        self._pods = f"{_API}/api/v1/namespaces/{_NAMESPACE}/pods"

    def create_job(self, manifest: dict[str, Any]) -> None:
        resp = httpx.post(
            self._jobs, headers=self._headers, verify=self._verify,
            json=manifest, timeout=15,
        )
        resp.raise_for_status()

    def wait_for_completion(self, job_name: str, timeout: int) -> str:
        """Poll the Job until it Completes or Fails, or the timeout elapses.
        Returns the terminal phase ("Succeeded"/"Failed"/"Unknown")."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = httpx.get(
                f"{self._jobs}/{job_name}", headers=self._headers,
                verify=self._verify, timeout=10,
            )
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
        resp = httpx.get(
            self._pods, headers=self._headers, verify=self._verify,
            params={"labelSelector": f"job-name={job_name}"}, timeout=10,
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
        resp = httpx.request(
            "DELETE", f"{self._jobs}/{job_name}", headers=self._headers,
            verify=self._verify, params={"propagationPolicy": "Background"},
            timeout=10,
        )
        if resp.status_code not in (200, 202, 404):
            resp.raise_for_status()
