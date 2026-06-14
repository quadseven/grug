"""Interim automated rotation of the grug-k8s-pod AWS access key (#386).

Throwaway interim until Roles Anywhere lands (#388/#389). A CronJob runs
`main()` on a schedule; the rotation LOGIC lives in `rotate()` which takes
injected `iam` (boto3 IAM client) + `k8s` (KubeApi) interfaces so it is
fully unit-testable without real AWS/Kubernetes.

Safety invariant: a pod NEVER runs on a deleted key. Ordering is
create-new -> update-secret -> roll-and-wait -> delete-old. The old key is
deleted only AFTER the new key is live in the Secret and every consuming
Deployment has rolled onto it.

AWS caps an IAM user at 2 access keys; if a stale second key already exists
(a prior partial rotation), the non-current one is deleted first to make
room. The "current" key is the one currently in the Secret.

This module is webhook-image-only (not mirrored) - like rerun.py /
cave_fallback.py - because only the rotator CronJob runs it.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

log = logging.getLogger("grug.key_rotator")

# k8s secret keys holding the pod's AWS credential.
_AK_ID_KEY = "AWS_ACCESS_KEY_ID"
_AK_SECRET_KEY = "AWS_SECRET_ACCESS_KEY"


@dataclass(frozen=True)
class RotationResult:
    """Outcome of one rotation - what main() reports + metrics off."""

    rotated: bool
    new_key_id: str | None
    deleted_key_id: str | None
    reason: str


class RotationError(RuntimeError):
    """Rotation failed AFTER creating a new key but before it was safely
    live. Carries the new key id so the caller does NOT leave it dangling
    AND does NOT delete the still-current old key (fail safe-open: the old
    key stays valid)."""

    def __init__(self, message: str, *, new_key_id: str | None = None) -> None:
        super().__init__(message)
        self.new_key_id = new_key_id


class KubeApi:
    """Thin in-cluster Kubernetes REST wrapper over httpx.

    Reads the mounted ServiceAccount token + CA and the in-cluster API host
    from the standard env/mount locations. Only the verbs the rotator needs:
    read/patch a Secret, trigger a Deployment rollout-restart, read rollout
    status. Injected into `rotate()` so the rotation logic is testable
    against a fake.
    """

    _SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, namespace: str | None = None, timeout: float = 15.0) -> None:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self._base = f"https://{host}:{port}"
        with open(f"{self._SA_DIR}/token", encoding="utf-8") as fh:
            self._token = fh.read().strip()
        self._ca = f"{self._SA_DIR}/ca.crt"
        self._ns = namespace or self._read_namespace()
        self._client = httpx.Client(
            base_url=self._base,
            verify=self._ca,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=timeout,
        )

    def _read_namespace(self) -> str:
        with open(f"{self._SA_DIR}/namespace", encoding="utf-8") as fh:
            return fh.read().strip()

    @property
    def namespace(self) -> str:
        return self._ns

    def get_secret_data(self, name: str) -> dict[str, str]:
        r = self._client.get(f"/api/v1/namespaces/{self._ns}/secrets/{name}")
        r.raise_for_status()
        return r.json().get("data", {})

    def patch_secret_data(self, name: str, data_b64: dict[str, str]) -> None:
        """merge-patch the given base64 values into the Secret's `data`."""
        r = self._client.patch(
            f"/api/v1/namespaces/{self._ns}/secrets/{name}",
            headers={"Content-Type": "application/merge-patch+json"},
            json={"data": data_b64},
        )
        r.raise_for_status()

    def restart_deployment(self, name: str, *, stamp: str) -> int:
        """Rollout-restart via the standard restartedAt annotation. Returns
        the Deployment's NEW `.metadata.generation` (the merge-patch bumps it)
        so the rollout wait can require status to catch up to THIS generation
        rather than mistaking the prior rollout's completed status for ours."""
        r = self._client.patch(
            f"/apis/apps/v1/namespaces/{self._ns}/deployments/{name}",
            headers={"Content-Type": "application/merge-patch+json"},
            json={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": stamp
                            }
                        }
                    }
                }
            },
        )
        r.raise_for_status()
        return r.json().get("metadata", {}).get("generation", 0)

    def deployment_rolled(self, name: str, min_generation: int) -> bool:
        """True iff the Deployment has fully rolled out AT OR PAST
        `min_generation` (the generation returned by restart_deployment).
        Requiring `observedGeneration >= min_generation` closes the
        stale-status race where, right after the restart patch, `.status`
        still reflects the PRIOR completed rollout (all replicas ready on the
        OLD pods) and would otherwise read as 'rolled'. (audit H2)"""
        r = self._client.get(
            f"/apis/apps/v1/namespaces/{self._ns}/deployments/{name}"
        )
        r.raise_for_status()
        d = r.json()
        spec = d.get("spec", {})
        status = d.get("status", {})
        desired = spec.get("replicas", 1)
        return (
            status.get("observedGeneration", 0) >= min_generation
            and status.get("updatedReplicas", 0) == desired
            and status.get("availableReplicas", 0) == desired
            and status.get("unavailableReplicas", 0) == 0
        )


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _current_key_id(k8s: "KubeApi", secret_name: str) -> str | None:
    """The access-key id currently in the Secret (base64-decoded), or None."""
    data = k8s.get_secret_data(secret_name)
    enc = data.get(_AK_ID_KEY)
    if not enc:
        return None
    return base64.b64decode(enc).decode().strip()


def rotate(
    iam,
    k8s: "KubeApi",
    *,
    pod_user: str,
    secret_name: str,
    deployments: list[str],
    stamp: str,
    wait_timeout_s: float = 180.0,
    poll_interval_s: float = 5.0,
    sleep=time.sleep,
    now=time.monotonic,
) -> RotationResult:
    """Rotate `pod_user`'s access key with the no-pod-on-a-deleted-key
    invariant. `iam` is a boto3 IAM client; `k8s` a KubeApi. `stamp`/`sleep`/
    `now` are injected for deterministic tests.
    """
    current_id = _current_key_id(k8s, secret_name)

    # AWS caps a user at 2 keys. If two already exist, drop the one that is
    # NOT current (stale from a prior partial rotation) to make room.
    existing = [
        k["AccessKeyId"]
        for k in iam.list_access_keys(UserName=pod_user).get("AccessKeyMetadata", [])
    ]
    if len(existing) >= 2:
        # Only safe to prune when we can POSITIVELY identify the in-use key
        # (the one in the Secret). If the Secret's key isn't among IAM's keys
        # (out-of-band deletion / stale Secret / empty Secret), we can't tell
        # which of the two is live - refuse rather than risk deleting an
        # in-use key. The rotation-failure monitor surfaces it for a human.
        if current_id not in existing:
            raise RotationError(
                f"{pod_user} has {len(existing)} access keys but the in-Secret "
                f"key {current_id!r} is not among them; refusing to guess which "
                f"to delete",
            )
        for kid in (k for k in existing if k != current_id):
            iam.delete_access_key(UserName=pod_user, AccessKeyId=kid)
            log.info("rotation_pruned_stale_key", extra={"key_id": kid})

    # 1) Create the new key.
    created = iam.create_access_key(UserName=pod_user)["AccessKey"]
    new_id = created["AccessKeyId"]
    new_secret = created["SecretAccessKey"]
    log.info("rotation_created_key", extra={"key_id": new_id})

    try:
        # 2) Publish it to the Secret.
        k8s.patch_secret_data(
            secret_name,
            {_AK_ID_KEY: _b64(new_id), _AK_SECRET_KEY: _b64(new_secret)},
        )
        # 3) Roll every consumer onto it and WAIT. Capture each Deployment's
        # post-restart generation so the wait requires status to catch up to
        # OUR rollout, not the prior one (audit H2).
        gens = {dep: k8s.restart_deployment(dep, stamp=stamp) for dep in deployments}
        deadline = now() + wait_timeout_s
        pending = set(deployments)
        while pending:
            pending = {d for d in pending if not k8s.deployment_rolled(d, gens[d])}
            if not pending:
                break
            if now() >= deadline:
                raise RotationError(
                    f"rollout did not complete within {wait_timeout_s}s: {sorted(pending)}",
                    new_key_id=new_id,
                )
            sleep(poll_interval_s)
    except RotationError:
        raise
    except Exception as e:  # noqa: BLE001 - re-wrap so caller never deletes old key
        raise RotationError(str(e), new_key_id=new_id) from e

    # 4) Only now is it safe to delete the OLD key.
    deleted_id = None
    if current_id and current_id != new_id:
        iam.delete_access_key(UserName=pod_user, AccessKeyId=current_id)
        deleted_id = current_id
        log.info("rotation_deleted_old_key", extra={"key_id": current_id})

    return RotationResult(
        rotated=True, new_key_id=new_id, deleted_key_id=deleted_id,
        reason="rotated",
    )


def main() -> int:
    """CronJob entrypoint. Emits a structured success/failure log line and
    exits non-zero on failure so the Kubernetes Job is marked failed (the
    DD kube-job-failure monitor keys off that). Never deletes the old key on
    failure (RotationError is caught here, the old key stays valid)."""
    # MUST configure structured logging first, or the success/failure events
    # below never reach Datadog as JSON with the service/env tags the
    # key_rotation_failed monitor queries (the default root logger drops INFO
    # and emits a bare stderr string). Mirrors main.py. (audit C1)
    from observability import configure_logging

    configure_logging()
    import boto3  # local import: only the rotator pod needs it at call time

    pod_user = os.environ.get("GRUG_ROTATE_USER", "grug-k8s-pod")
    secret_name = os.environ.get("GRUG_ROTATE_SECRET", "grug-secrets")
    deployments = [
        d.strip()
        for d in os.environ.get(
            "GRUG_ROTATE_DEPLOYMENTS", "grug-api,grug-webhook,grug-consumer"
        ).split(",")
        if d.strip()
    ]
    stamp = datetime.now(timezone.utc).isoformat()

    try:
        # Construct inside the try so a KubeApi/boto3 init failure (misconfig)
        # also surfaces as key_rotation_failed, not an un-logged crash (M1).
        iam = boto3.client("iam")
        k8s = KubeApi()
        res = rotate(
            iam, k8s, pod_user=pod_user, secret_name=secret_name,
            deployments=deployments, stamp=stamp,
        )
        log.info(
            "key_rotation_success",
            extra={"new_key_id": res.new_key_id, "deleted_key_id": res.deleted_key_id},
        )
        return 0
    except Exception as e:  # noqa: BLE001 - top-level guard; failure -> exit 1
        log.error(
            "key_rotation_failed",
            extra={"err": str(e), "dangling_new_key_id": getattr(e, "new_key_id", None)},
            exc_info=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
