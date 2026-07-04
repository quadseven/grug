"""Trial runner tests (#469) — the launcher that submits the Job + reads back
the result. The k8s I/O is behind an injectable `cluster` seam so these run with
a fake cluster (no real API server)."""

from __future__ import annotations

import json

from personas.smasher.sandbox import TRIAL_NAMESPACE, TrialResult
from personas.smasher.trial_runner import _job_name, launch_trial


class _FakeCluster:
    """Records the submitted manifest + secret and returns a canned message."""

    def __init__(self, termination_message, *, phase="Succeeded"):
        self.termination_message = termination_message
        self.phase = phase
        self.created = None
        self.secret = None
        self.deleted_job = False
        self.deleted_secret = False

    def create_secret(self, name, token):
        self.secret = (name, token)

    def delete_secret(self, name):
        self.deleted_secret = True

    def create_job(self, manifest):
        self.created = manifest

    def wait_for_completion(self, job_name, timeout):
        return self.phase

    def read_termination_message(self, job_name):
        return self.termination_message

    def delete_job(self, job_name):
        self.deleted_job = True


_TARGETS = {"pkg/mod.py": [2]}


def _launch(cluster, **over):
    kw = dict(
        owner="acme", repo="widget", head_sha="deadbeefcafe",
        token="ghs_x", targets=_TARGETS,  # noqa: S106
        mutant_cap=10, per_mutant_timeout_seconds=30, total_budget_seconds=300,
        image="registry.example/grug-webhook:sha",
        cluster=cluster,
    )
    kw.update(over)
    return launch_trial(**kw)


def test_happy_path_returns_survivors_and_cleans_up():
    msg = json.dumps({
        "status": "completed", "total": 3, "killed": 2,
        "survived": [{"file": "pkg/mod.py", "line": 2, "operator": "boundary",
                      "original": "0", "mutated": "1"}],
    })
    cluster = _FakeCluster(msg)
    res = _launch(cluster)
    assert isinstance(res, TrialResult)
    assert res.status == "completed"
    assert len(res.survived) == 1
    assert cluster.created["kind"] == "Job"
    assert cluster.created["metadata"]["namespace"] == TRIAL_NAMESPACE
    # Job + Secret both cleaned up.
    assert cluster.deleted_job is True and cluster.deleted_secret is True


def test_token_goes_into_a_secret_referenced_by_the_job():
    cluster = _FakeCluster(json.dumps({"status": "completed", "total": 1, "killed": 1}))
    _launch(cluster, token="ghs_secret")  # noqa: S106
    # The token is created as a Secret, never inlined into the manifest.
    assert cluster.secret is not None
    secret_name, tok = cluster.secret
    assert tok == "ghs_secret"
    manifest_json = json.dumps(cluster.created)
    assert "ghs_secret" not in manifest_json
    # The fetch container references that Secret by name.
    fetch = cluster.created["spec"]["template"]["spec"]["initContainers"][0]
    ref = next(e["valueFrom"]["secretKeyRef"] for e in fetch["env"] if e["name"] == "GRUG_TRIAL_TOKEN")
    assert ref["name"] == secret_name


def test_job_name_is_repo_qualified_no_collision_on_shared_sha():
    # Two different repos at the SAME head SHA must not collide onto one Job.
    a = _job_name("acme", "widget", "deadbeefcafe")
    b = _job_name("other", "widget", "deadbeefcafe")
    assert a != b
    assert a.startswith("grug-trial-") and len(a) <= 63


def test_no_image_degrades_with_reason():
    cluster = _FakeCluster(None)
    res = _launch(cluster, image="")
    assert res.status == "degraded" and res.reason == "no_job_image_configured"
    # No Job/Secret created when we bail early.
    assert cluster.created is None and cluster.secret is None


def test_no_termination_message_degrades():
    res = _launch(_FakeCluster(None))
    assert res.status == "degraded"


def test_cluster_error_degrades_not_raises_and_cleans_secret():
    class _Boom:
        def create_secret(self, name, token):
            raise RuntimeError("api down")

        def delete_secret(self, name):
            pass

        def create_job(self, manifest):
            raise RuntimeError("api down")

        def wait_for_completion(self, *a):
            raise RuntimeError("api down")

        def read_termination_message(self, *a):
            raise RuntimeError("api down")

        def delete_job(self, *a):
            pass

    res = _launch(_Boom())
    assert res.status == "degraded" and res.reason == "job_create_failed"


def test_delete_best_effort_even_on_read_failure():
    class _ReadBoom(_FakeCluster):
        def read_termination_message(self, job_name):
            raise RuntimeError("cannot read")

    cluster = _ReadBoom(None)
    res = _launch(cluster)
    assert res.status == "degraded"
    assert cluster.deleted_job is True and cluster.deleted_secret is True
