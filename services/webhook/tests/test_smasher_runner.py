"""Trial runner tests (#469) — the launcher that submits the Job + reads back
the result. The k8s I/O is behind an injectable `cluster` seam so these run with
a fake cluster (no real API server)."""

from __future__ import annotations

import json

from personas.smasher.sandbox import TrialResult
from personas.smasher.trial_runner import launch_trial


class _FakeCluster:
    """Records the submitted manifest and returns a canned termination message."""

    def __init__(self, termination_message, *, phase="Succeeded"):
        self.termination_message = termination_message
        self.phase = phase
        self.created = None
        self.deleted = False

    def create_job(self, manifest):
        self.created = manifest

    def wait_for_completion(self, job_name, timeout):
        return self.phase

    def read_termination_message(self, job_name):
        return self.termination_message

    def delete_job(self, job_name):
        self.deleted = True


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


def test_happy_path_returns_survivors():
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
    # The Job was submitted and cleaned up.
    assert cluster.created["kind"] == "Job"
    assert cluster.deleted is True


def test_token_reaches_only_the_fetch_container_in_submitted_manifest():
    cluster = _FakeCluster(json.dumps({"status": "completed", "total": 0, "killed": 0}))
    _launch(cluster, token="ghs_secret")  # noqa: S106
    spec = cluster.created["spec"]["template"]["spec"]
    holders = [
        c["name"] for c in spec["initContainers"] + spec["containers"]
        if any("ghs_secret" == e.get("value") for e in c.get("env", []))
    ]
    assert holders == ["fetch"]


def test_no_termination_message_degrades():
    res = _launch(_FakeCluster(None))
    assert res.status == "degraded"


def test_job_failed_phase_still_reads_message():
    # A Job that hit its deadline may still have written a partial result;
    # if not, we degrade. Here it wrote nothing -> degraded.
    res = _launch(_FakeCluster(None, phase="Failed"))
    assert res.status == "degraded"


def test_cluster_error_degrades_not_raises():
    class _Boom:
        def create_job(self, manifest):
            raise RuntimeError("api down")

        def wait_for_completion(self, *a):
            raise RuntimeError("api down")

        def read_termination_message(self, *a):
            raise RuntimeError("api down")

        def delete_job(self, *a):
            pass

    res = _launch(_Boom())
    assert res.status == "degraded"


def test_delete_best_effort_even_on_read_failure():
    class _ReadBoom(_FakeCluster):
        def read_termination_message(self, job_name):
            raise RuntimeError("cannot read")

    cluster = _ReadBoom(None)
    res = _launch(cluster)
    assert res.status == "degraded"
    assert cluster.deleted is True  # cleanup still ran
