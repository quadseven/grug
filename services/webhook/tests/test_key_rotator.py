"""Tests for the interim AWS key rotator (#386).

The safety property under test: a pod NEVER runs on a deleted key. The old
key is deleted only AFTER the new key is in the Secret and every Deployment
has rolled. A failure anywhere before that must leave the old key valid and
not strand the new one as a silent dangler.
"""

from __future__ import annotations

import base64

import pytest

import key_rotator
from key_rotator import RotationError, rotate


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


class FakeIam:
    """Records IAM key ops in call order; models the 2-key cap loosely."""

    def __init__(self, keys: list[str]):
        self._keys = list(keys)
        self.calls: list[tuple[str, str]] = []
        self._created = 0

    def list_access_keys(self, *, UserName):  # noqa: N803 (boto3 kwarg)
        return {"AccessKeyMetadata": [{"AccessKeyId": k} for k in self._keys]}

    def create_access_key(self, *, UserName):  # noqa: N803
        self._created += 1
        kid = f"AKIA-NEW-{self._created}"
        self._keys.append(kid)
        self.calls.append(("create", kid))
        return {"AccessKey": {"AccessKeyId": kid, "SecretAccessKey": f"secret-{kid}"}}

    def delete_access_key(self, *, UserName, AccessKeyId):  # noqa: N803
        self._keys.remove(AccessKeyId)
        self.calls.append(("delete", AccessKeyId))


class FakeKube:
    """In-memory Secret + Deployment rollout model; records call order."""

    def __init__(self, current_key_id: str | None, rolled_after: int = 0):
        data = {}
        if current_key_id is not None:
            data = {
                key_rotator._AK_ID_KEY: _b64(current_key_id),
                key_rotator._AK_SECRET_KEY: _b64("old-secret"),
            }
        self._secret = data
        self.calls: list[tuple[str, str]] = []
        # status catches up to the restart generation after this many polls
        # (models the stale-status window the H2 generation gate must survive)
        self._rolled_after = rolled_after
        self._gen: dict[str, int] = {}
        self._observed: dict[str, int] = {}
        self._polls: dict[str, int] = {}
        self.restarted: list[str] = []
        self.patch_fails = False

    def get_secret_data(self, name):
        return dict(self._secret)

    def patch_secret_data(self, name, data_b64):
        if self.patch_fails:
            raise RuntimeError("k8s patch boom")
        self._secret.update(data_b64)
        self.calls.append(("secret_update", data_b64[key_rotator._AK_ID_KEY]))

    def restart_deployment(self, name, *, stamp):
        # merge-patch bumps generation; return the NEW generation.
        self._gen[name] = self._gen.get(name, 1) + 1
        self.restarted.append(name)
        self.calls.append(("restart", name))
        return self._gen[name]

    def deployment_rolled(self, name, min_generation):
        n = self._polls.get(name, 0)
        self._polls[name] = n + 1
        # Stale until `rolled_after` polls elapse, THEN observed catches up to
        # the restart generation. Before that, replicas may look ready but the
        # generation gate (observed < min_generation) keeps it "not rolled".
        if n >= self._rolled_after:
            self._observed[name] = self._gen.get(name, 0)
        return self._observed.get(name, 0) >= min_generation


_DEPLOYMENTS = ["grug-api", "grug-webhook", "grug-consumer"]


def _rotate(iam, k8s, **kw):
    return rotate(
        iam, k8s, pod_user="grug-k8s-pod", secret_name="grug-secrets",
        deployments=_DEPLOYMENTS, stamp="2026-01-01T00:00:00Z",
        sleep=lambda _s: None, now=_fake_clock(), **kw,
    )


def _fake_clock():
    t = {"v": 0.0}
    def now():
        t["v"] += 1.0
        return t["v"]
    return now


def test_happy_path_create_update_roll_then_delete_old():
    iam = FakeIam(["AKIA-OLD"])
    k8s = FakeKube(current_key_id="AKIA-OLD")
    res = _rotate(iam, k8s)
    assert res.rotated
    assert res.new_key_id == "AKIA-NEW-1"
    assert res.deleted_key_id == "AKIA-OLD"
    # Secret now holds the new id.
    assert base64.b64decode(k8s._secret[key_rotator._AK_ID_KEY]).decode() == "AKIA-NEW-1"


def test_invariant_old_key_deleted_only_after_secret_update_and_rollout():
    iam = FakeIam(["AKIA-OLD"])
    k8s = FakeKube(current_key_id="AKIA-OLD", rolled_after=1)
    _rotate(iam, k8s)
    # Reconstruct the global order: create (iam) -> secret_update (k8s) ->
    # restarts (k8s) -> delete (iam). Assert delete-old is LAST and create
    # is FIRST, and the secret update + restarts precede the delete.
    order = [c[0] for c in iam.calls]
    assert order[0] == "create"
    assert order[-1] == "delete"
    assert iam.calls[-1] == ("delete", "AKIA-OLD")
    # k8s side updated the secret and restarted all three before the delete.
    assert ("secret_update", _b64("AKIA-NEW-1")) in k8s.calls
    assert k8s.restarted == _DEPLOYMENTS


def test_stale_status_does_not_prematurely_delete_old_key():
    """H2: just after the restart patch, a Deployment's status can still show
    the PRIOR rollout as complete (replicas ready on OLD pods). The
    generation gate must keep waiting - not delete the old key - until status
    observes OUR restart generation."""
    iam = FakeIam(["AKIA-OLD"])
    k8s = FakeKube(current_key_id="AKIA-OLD", rolled_after=2)  # 2 stale polls
    res = _rotate(iam, k8s)
    assert res.deleted_key_id == "AKIA-OLD"
    # Each deployment was polled PAST the stale window before being accepted.
    assert all(p > 2 for p in k8s._polls.values())
    # Old key deleted only after the secret update + restarts (still last).
    assert iam.calls[-1] == ("delete", "AKIA-OLD")


def test_two_key_cap_prunes_stale_noncurrent_key_first():
    # User already has the current key + a stale leftover -> prune stale,
    # then create. (delete-stale, create, ..., delete-old)
    iam = FakeIam(["AKIA-OLD", "AKIA-STALE"])
    k8s = FakeKube(current_key_id="AKIA-OLD")
    res = _rotate(iam, k8s)
    assert ("delete", "AKIA-STALE") in iam.calls
    # stale pruned BEFORE the new key is created
    assert iam.calls.index(("delete", "AKIA-STALE")) < iam.calls.index(("create", "AKIA-NEW-1"))
    assert res.deleted_key_id == "AKIA-OLD"


def test_two_keys_none_current_refuses_to_guess():
    iam = FakeIam(["AKIA-X", "AKIA-Y"])
    k8s = FakeKube(current_key_id="AKIA-OLD")  # neither X nor Y is current
    with pytest.raises(RotationError):
        _rotate(iam, k8s)
    # Never created or deleted anything - bailed before touching keys.
    assert iam.calls == []


def test_rollout_timeout_raises_and_keeps_old_key():
    iam = FakeIam(["AKIA-OLD"])
    # never rolls -> timeout
    k8s = FakeKube(current_key_id="AKIA-OLD", rolled_after=10_000)
    with pytest.raises(RotationError) as ei:
        _rotate(iam, k8s, wait_timeout_s=3.0)
    assert ei.value.new_key_id == "AKIA-NEW-1"
    # Old key NOT deleted (fail safe-open): no delete of AKIA-OLD.
    assert ("delete", "AKIA-OLD") not in iam.calls


def test_secret_patch_failure_rewrapped_and_keeps_old_key():
    iam = FakeIam(["AKIA-OLD"])
    k8s = FakeKube(current_key_id="AKIA-OLD")
    k8s.patch_fails = True
    with pytest.raises(RotationError) as ei:
        _rotate(iam, k8s)
    assert ei.value.new_key_id == "AKIA-NEW-1"
    assert ("delete", "AKIA-OLD") not in iam.calls


def test_first_run_no_current_key_creates_and_does_not_delete():
    iam = FakeIam([])
    k8s = FakeKube(current_key_id=None)
    res = _rotate(iam, k8s)
    assert res.rotated
    assert res.new_key_id == "AKIA-NEW-1"
    assert res.deleted_key_id is None
    assert all(c[0] != "delete" for c in iam.calls)
