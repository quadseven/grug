"""Sandbox-boundary tests (#469, ADR-0013).

`sandbox.py` is PURE: it builds the locked-down Trial Job manifest and parses
the termination-message result. These tests are the adversarial lock on the
security boundary — the properties that keep PR-author code from touching a
credential must be asserted, not assumed.
"""

from __future__ import annotations

import json

from personas.code_reviewer.diff_parser import parse_diff
from personas.smasher.sandbox import (
    SurvivedMutant,
    TrialResult,
    build_trial_job,
    extract_target_lines,
    parse_trial_result,
)

_KW = dict(
    job_name="grug-trial-abc123",
    image="registry.example/grug-webhook:sha",
    owner="acme",
    repo="widget",
    head_sha="deadbeef",
    token="ghs_secrettoken",  # noqa: S106 — test fixture, not a real cred
    targets={"pkg/mod.py": [2, 3]},
    total_budget_seconds=300,
    per_mutant_timeout_seconds=30,
    mutant_cap=10,
)


def _job(**over):
    kw = {**_KW, **over}
    return build_trial_job(**kw)


def _containers(job):
    spec = job["spec"]["template"]["spec"]
    return spec.get("initContainers", []), spec["containers"]


def test_pod_gets_no_service_account_token():
    job = _job()
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_token_only_reaches_the_fetch_init_container():
    job = _job()
    inits, mains = _containers(job)
    # Exactly ONE container may see the token — the fetch/clone init phase.
    holders = []
    for c in inits + mains:
        for e in c.get("env", []):
            if e.get("value") == _KW["token"] or "TOKEN" in e.get("name", ""):
                holders.append(c["name"])
    assert holders == ["fetch"], f"token leaked beyond fetch init: {holders}"


def test_test_container_has_no_secrets_and_no_token():
    job = _job()
    _inits, mains = _containers(job)
    test = next(c for c in mains if c["name"] == "test")
    # No secret injection at all into the phase that runs author code.
    assert "envFrom" not in test
    for e in test.get("env", []):
        assert "TOKEN" not in e["name"]
        assert e.get("value") != _KW["token"]
        assert "valueFrom" not in e  # no secretKeyRef sneaking in


def test_test_container_is_hardened():
    job = _job()
    _inits, mains = _containers(job)
    test = next(c for c in mains if c["name"] == "test")
    sc = test["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["runAsNonRoot"] is True
    assert sc["capabilities"]["drop"] == ["ALL"]
    # Resource limits so a runaway mutant can't starve the node.
    assert test["resources"]["limits"]["memory"]
    assert test["resources"]["limits"]["cpu"]


def test_deadline_and_restart_policy_bound_runaways():
    job = _job()
    spec = job["spec"]["template"]["spec"]
    # activeDeadlineSeconds = the kubelet kill switch regardless of author code.
    assert job["spec"]["activeDeadlineSeconds"] == _KW["total_budget_seconds"]
    assert spec["restartPolicy"] == "Never"
    assert job["spec"]["backoffLimit"] == 0  # never re-run author code on failure


def test_result_channel_is_termination_message():
    job = _job()
    _inits, mains = _containers(job)
    test = next(c for c in mains if c["name"] == "test")
    # The worker writes JSON to /dev/termination-log; we read it via the API.
    assert test["terminationMessagePath"] == "/dev/termination-log"
    assert test["terminationMessagePolicy"] == "File"


def test_pod_labelled_for_networkpolicy_selection():
    job = _job()
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert labels.get("grug-trial") == "true"


def test_budget_env_passed_to_worker_not_as_secret():
    job = _job()
    _inits, mains = _containers(job)
    test = next(c for c in mains if c["name"] == "test")
    env = {e["name"]: e["value"] for e in test.get("env", []) if "value" in e}
    assert env["GRUG_TRIAL_MUTANT_CAP"] == "10"
    assert env["GRUG_TRIAL_PER_MUTANT_TIMEOUT"] == "30"


# ---- extract_target_lines ----

def test_extract_target_lines_added_python_only():
    diff = (
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def f(x):\n"
        "+    y = x > 0\n"
        "     return x\n"
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " hi\n"
        "+more\n"
    )
    hunks = parse_diff(diff)
    targets = extract_target_lines(hunks)
    assert "pkg/mod.py" in targets
    assert 2 in targets["pkg/mod.py"]  # the added `y = x > 0` line
    assert "README.md" not in targets  # non-Python skipped


def test_extract_target_lines_empty_when_no_python():
    diff = (
        "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n"
        "@@ -0,0 +1 @@\n+hello\n"
    )
    assert extract_target_lines(parse_diff(diff)) == {}


# ---- parse_trial_result ----

def test_parse_trial_result_survived():
    msg = json.dumps({
        "status": "completed",
        "total": 8, "killed": 6, "timed_out": 1, "errored": 0,
        "survived": [
            {"file": "pkg/mod.py", "line": 2, "operator": "comparison-flip",
             "original": ">", "mutated": ">="},
        ],
    })
    res = parse_trial_result(msg)
    assert isinstance(res, TrialResult)
    assert res.status == "completed"
    assert res.total == 8 and res.killed == 6
    assert res.survived == (
        SurvivedMutant("pkg/mod.py", 2, "comparison-flip", ">", ">="),
    )


def test_parse_trial_result_malformed_degrades():
    res = parse_trial_result("not json{")
    assert res.status == "degraded"
    assert res.survived == ()


def test_parse_trial_result_empty_degrades():
    for msg in ("", None):
        res = parse_trial_result(msg)
        assert res.status == "degraded"


def test_parse_trial_result_drops_malformed_survivor_rows():
    msg = json.dumps({
        "status": "completed", "total": 2, "killed": 1,
        "survived": [
            {"file": "a.py", "line": 1, "operator": "boolean",
             "original": "True", "mutated": "False"},
            {"file": "b.py"},  # missing fields — must be dropped, not crash
        ],
    })
    res = parse_trial_result(msg)
    assert len(res.survived) == 1
    assert res.survived[0].file == "a.py"
