"""Sandbox-boundary tests (#469, ADR-0013).

`sandbox.py` is PURE: it builds the locked-down Trial Job manifest and parses
the termination-message result. These tests are the adversarial lock on the
security boundary — the properties that keep PR-author code from touching a
credential must be asserted, not assumed. The parse side is a TRUST BOUNDARY
(author code writes the message), so validation of forged input is tested too.
"""

from __future__ import annotations

import json

from personas.code_reviewer.diff_parser import parse_diff
from personas.smasher.sandbox import (
    TRIAL_NAMESPACE,
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
    token_secret_name="grug-trial-abc123-token",
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


def test_job_runs_in_isolated_trial_namespace():
    # The escalation fix: Trial Jobs live in the dedicated, secret-free
    # grug-trial namespace, NOT in grug.
    assert _job()["metadata"]["namespace"] == TRIAL_NAMESPACE == "grug-trial"


def test_pod_gets_no_service_account_token():
    job = _job()
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_token_only_reaches_fetch_via_secretref_never_inlined():
    job = _job(token_secret_name="the-secret")
    inits, mains = _containers(job)
    # The token is NEVER a plaintext env value anywhere (would persist in etcd).
    for c in inits + mains:
        for e in c.get("env", []):
            assert "value" not in e or "token" not in e.get("value", "").lower()
    # Exactly the `fetch` init container carries a GRUG_TRIAL_TOKEN sourced from
    # the per-Job Secret via secretKeyRef.
    holders = []
    for c in inits + mains:
        for e in c.get("env", []):
            if e["name"] == "GRUG_TRIAL_TOKEN":
                assert e["valueFrom"]["secretKeyRef"] == {"name": "the-secret", "key": "token"}
                holders.append(c["name"])
    assert holders == ["fetch"]


def test_test_container_has_no_secrets_and_no_token():
    job = _job()
    _inits, mains = _containers(job)
    test = next(c for c in mains if c["name"] == "test")
    assert "envFrom" not in test
    for e in test.get("env", []):
        assert "TOKEN" not in e["name"]
        assert "valueFrom" not in e  # no secretKeyRef sneaking in


def test_all_containers_are_hardened():
    # The security context must be locked on EVERY container that touches
    # author-controlled input — fetch (tarball), deps (requirements), test.
    job = _job()
    inits, mains = _containers(job)
    for c in inits + mains:
        sc = c["securityContext"]
        assert sc["readOnlyRootFilesystem"] is True, c["name"]
        assert sc["allowPrivilegeEscalation"] is False, c["name"]
        assert sc["runAsNonRoot"] is True, c["name"]
        assert sc["capabilities"]["drop"] == ["ALL"], c["name"]
        assert c["resources"]["limits"]["memory"] and c["resources"]["limits"]["cpu"]


def test_every_container_has_writable_tmp():
    # readOnlyRootFilesystem would break pip + pytest tmp_path without a
    # writable /tmp emptyDir mounted on each container.
    job = _job()
    spec = job["spec"]["template"]["spec"]
    vol_names = {v["name"] for v in spec["volumes"]}
    assert "tmp" in vol_names
    inits, mains = _containers(job)
    for c in inits + mains:
        mounts = {m["mountPath"] for m in c["volumeMounts"]}
        assert "/tmp" in mounts, c["name"]
        assert "/workspace" in mounts, c["name"]


def test_deadline_and_restart_policy_bound_runaways():
    job = _job()
    spec = job["spec"]["template"]["spec"]
    assert job["spec"]["activeDeadlineSeconds"] == _KW["total_budget_seconds"]
    assert spec["restartPolicy"] == "Never"
    assert job["spec"]["backoffLimit"] == 0


def test_result_channel_is_termination_message():
    job = _job()
    _inits, mains = _containers(job)
    test = next(c for c in mains if c["name"] == "test")
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
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


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
    assert 2 in targets["pkg/mod.py"]
    assert "README.md" not in targets


def test_extract_target_lines_empty_when_no_python():
    diff = (
        "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n"
        "@@ -0,0 +1 @@\n+hello\n"
    )
    assert extract_target_lines(parse_diff(diff)) == {}


def test_extract_target_lines_added_line_starting_with_plusplus_counts():
    # An added line whose CONTENT begins with `++`/`--` must still count and not
    # desync subsequent line numbers (single-char marker only).
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -1,1 +1,3 @@\n"
        " x = 0\n"
        "+y = --x\n"      # line 2, content starts with '--'
        "+z = y == 1\n"   # line 3
    )
    targets = extract_target_lines(parse_diff(diff))
    assert targets["m.py"] == [2, 3]


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
    assert res.reason == "unparseable_termination_message"


def test_parse_trial_result_empty_degrades():
    for msg in ("", None):
        res = parse_trial_result(msg)
        assert res.status == "degraded"
        assert res.reason == "no_termination_message"


def test_parse_trial_result_non_iterable_survived_degrades_not_crashes():
    # A forged {"survived": 5} must degrade, never raise (the never-raise
    # contract) — `for row in 5` would crash a naive parser.
    res = parse_trial_result(json.dumps({"status": "completed", "survived": 5}))
    assert res.status == "degraded"


def test_parse_trial_result_drops_forged_and_malformed_rows():
    msg = json.dumps({
        "status": "completed", "total": 4, "killed": 1,
        "survived": [
            {"file": "a.py", "line": 1, "operator": "boolean",
             "original": "True", "mutated": "False"},
            {"file": "b.py"},  # missing fields — dropped
            {"file": "c.py", "line": 1, "operator": "evil-injection",  # unknown op — dropped
             "original": "x", "mutated": "y"},
            {"file": "d.py", "line": 0, "operator": "boundary",  # line<1 — dropped
             "original": "0", "mutated": "1"},
        ],
    })
    res = parse_trial_result(msg)
    assert len(res.survived) == 1
    assert res.survived[0].file == "a.py"


def test_parse_trial_result_clamps_negative_counts():
    msg = json.dumps({"status": "completed", "total": -3, "killed": -1, "survived": []})
    res = parse_trial_result(msg)
    assert res.total == 0 and res.killed == 0


def test_parse_trial_result_surfaces_reason_and_truncated():
    msg = json.dumps({"status": "degraded", "reason": "baseline_failed", "survived": []})
    res = parse_trial_result(msg)
    assert res.status == "degraded" and res.reason == "baseline_failed"


def test_survived_mutant_rejects_oversized_field():
    import pytest
    with pytest.raises(ValueError):
        SurvivedMutant("f.py", 1, "boolean", "x" * 5000, "y")
