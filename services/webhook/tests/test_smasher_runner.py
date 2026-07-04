"""Trial runner tests (#469) — the launcher that orchestrates the two-pod flow
(PVC -> prep pod -> test pod) and reads back the result. The k8s I/O is behind
an injectable `cluster` seam so these run with a fake cluster (no API server)."""

from __future__ import annotations

import json

from personas.smasher.sandbox import TRIAL_NAMESPACE
from personas.smasher.trial_runner import _job_name, launch_trial


class _FakeCluster:
    """Records the full lifecycle and returns a canned test-pod message."""

    def __init__(self, termination_message, *, prep_phase="Succeeded", test_phase="Succeeded"):
        self.termination_message = termination_message
        self.prep_phase = prep_phase
        self.test_phase = test_phase
        self.events: list = []
        self.jobs: dict = {}
        self.secret = None
        self.pvc = None

    def create_secret(self, name, token):
        self.secret = (name, token); self.events.append(("secret+", name))

    def delete_secret(self, name):
        self.events.append(("secret-", name))

    def create_pvc(self, manifest):
        self.pvc = manifest; self.events.append(("pvc+", manifest["metadata"]["name"]))

    def delete_pvc(self, name):
        self.events.append(("pvc-", name))

    def create_job(self, manifest):
        n = manifest["metadata"]["name"]; self.jobs[n] = manifest
        self.events.append(("job+", n))

    def wait_for_completion(self, job_name, timeout):
        return self.prep_phase if job_name.endswith("-prep") else self.test_phase

    def read_termination_message(self, job_name):
        assert job_name.endswith("-test"), "result read from the TEST pod only"
        return self.termination_message

    def delete_job(self, job_name):
        self.events.append(("job-", job_name))


_TARGETS = {"pkg/mod.py": [2]}


def _launch(cluster, **over):
    kw = dict(
        owner="acme", repo="widget", head_sha="deadbeefcafe",
        token="ghs_x", targets=_TARGETS,  # noqa: S106
        mutant_cap=10, per_mutant_timeout_seconds=30, total_budget_seconds=300,
        image="registry.example/grug-webhook:sha", cluster=cluster,
    )
    kw.update(over)
    return launch_trial(**kw)


def test_happy_path_two_pod_flow_and_cleanup():
    msg = json.dumps({
        "status": "completed", "total": 3, "killed": 2,
        "survived": [{"file": "pkg/mod.py", "line": 2, "operator": "boundary",
                      "original": "0", "mutated": "1"}],
    })
    cluster = _FakeCluster(msg)
    res = _launch(cluster)
    assert res.status == "completed" and len(res.survived) == 1
    created_jobs = [n for kind, n in cluster.events if kind == "job+"]
    # prep pod created BEFORE the test pod.
    assert created_jobs == [n for n in created_jobs if n.endswith(("-prep", "-test"))]
    assert any(n.endswith("-prep") for n in created_jobs)
    assert any(n.endswith("-test") for n in created_jobs)
    prep_i = next(i for i, (k, n) in enumerate(cluster.events) if k == "job+" and n.endswith("-prep"))
    test_i = next(i for i, (k, n) in enumerate(cluster.events) if k == "job+" and n.endswith("-test"))
    assert prep_i < test_i
    # PVC + Secret + both Jobs cleaned up.
    assert cluster.pvc is not None
    assert ("pvc-", cluster.pvc["metadata"]["name"]) in cluster.events
    assert any(k == "secret-" for k, _ in cluster.events)
    deleted = {n for k, n in cluster.events if k == "job-"}
    assert all(n in deleted for n in created_jobs)


def test_test_pod_runs_in_grug_trial_namespace():
    cluster = _FakeCluster(json.dumps({"status": "completed", "total": 1, "killed": 1}))
    _launch(cluster)
    for m in cluster.jobs.values():
        assert m["metadata"]["namespace"] == TRIAL_NAMESPACE


def test_token_secret_dropped_before_test_pod_runs():
    # The token Secret must be deleted after prep succeeds, BEFORE the test pod
    # (author code) runs - so the author phase can't even reach the Secret.
    cluster = _FakeCluster(json.dumps({"status": "completed", "total": 1, "killed": 1}))
    _launch(cluster)
    test_create_i = next(i for i, (k, n) in enumerate(cluster.events) if k == "job+" and n.endswith("-test"))
    # A secret delete happens before the test job is created.
    secret_deletes = [i for i, (k, _) in enumerate(cluster.events) if k == "secret-"]
    assert any(i < test_create_i for i in secret_deletes)


def test_prep_failure_degrades_and_skips_test_pod():
    cluster = _FakeCluster(None, prep_phase="Failed")
    res = _launch(cluster)
    assert res.status == "degraded" and res.reason == "prep_failed"
    # The test pod (author code) is NEVER created if prep failed.
    assert not any(k == "job+" and n.endswith("-test") for k, n in cluster.events)


def test_failed_test_job_with_forged_clean_message_degrades():
    # CRITICAL: a malicious test can pre-write a forged clean termination
    # message and kill the worker (-> Job FAILED). The runner must NOT trust the
    # message from a non-Succeeded Job - it degrades WITHOUT parsing it.
    forged = json.dumps({"status": "completed", "total": 1, "killed": 1, "survived": []})
    cluster = _FakeCluster(forged, test_phase="Failed")
    res = _launch(cluster)
    assert res.status == "degraded" and res.reason == "test_job_failed"
    assert res.survived == ()  # the forged clean result never surfaced


def test_token_goes_into_a_secret_never_inlined():
    cluster = _FakeCluster(json.dumps({"status": "completed", "total": 1, "killed": 1}))
    _launch(cluster, token="ghs_secret")  # noqa: S106
    assert cluster.secret[1] == "ghs_secret"
    for m in cluster.jobs.values():
        assert "ghs_secret" not in json.dumps(m)


def test_job_name_repo_qualified_no_collision_on_shared_sha():
    a = _job_name("acme", "widget", "deadbeefcafe")
    b = _job_name("other", "widget", "deadbeefcafe")
    assert a != b and a.startswith("grug-trial-") and len(a) <= 55  # +suffix stays <=63


def test_no_image_degrades_with_reason():
    cluster = _FakeCluster(None)
    res = _launch(cluster, image="")
    assert res.status == "degraded" and res.reason == "no_job_image_configured"
    assert cluster.pvc is None and cluster.secret is None


def test_no_termination_message_degrades():
    assert _launch(_FakeCluster(None)).status == "degraded"


def test_cluster_error_degrades_not_raises():
    class _Boom(_FakeCluster):
        def create_pvc(self, manifest):
            raise RuntimeError("api down")

    res = _launch(_Boom(None))
    assert res.status == "degraded" and res.reason == "prep_create_failed"
