import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shepherd

REPO = "example-org/example-repo"
IMAGE = "registry.example.com/example-service"
MERGE = "a" * 40
DIGEST = "sha256:" + "b" * 64
OLD_DIGEST = "sha256:" + "c" * 64
ARGS = (
    "pr-deploy", "--repo", REPO, "--pr", "123", "--image", IMAGE,
    "--namespace", "production", "--deployment", "example", "--container", "app",
)
PR = ["gh", "pr", "view", "123", "-R", REPO, "--json", "state,mergeCommit,mergedAt,body"]
MANIFEST = [
    "docker", "buildx", "imagetools", "inspect", "--format", "{{.Manifest.Digest}}",
    f"{IMAGE}:{MERGE}",
]
DEPLOYMENT = [
    "kubectl", "--namespace", "production", "get", "deployment", "example", "-o", "json",
]
CLAIM = "Metric `service.recovered` appears."
PR_DATA = {
    "state": "MERGED",
    "mergeCommit": {"oid": MERGE},
    "mergedAt": "2026-01-01T00:00:00Z",
    "body": f"## Acceptance criteria\n- {CLAIM}",
}
DEPLOY_EPOCH = "1767225660"
NOW = "1767225720"


def stub_commands(monkeypatch, responses):
    def run(argv, **kwargs):
        assert not kwargs.get("shell")
        result = responses[tuple(argv)]
        if callable(result):
            result = result()
        if isinstance(result, subprocess.CompletedProcess):
            return result
        if not isinstance(result, str):
            result = json.dumps(result)
        return subprocess.CompletedProcess(argv, 0, stdout=result, stderr="")

    monkeypatch.setattr(subprocess, "run", run)


def test_report_merged_pr_with_published_artifact_but_old_live_pin(monkeypatch, capsys):
    stub_commands(
        monkeypatch,
        {
            tuple(PR): PR_DATA,
            tuple(MANIFEST): DIGEST,
            tuple(DEPLOYMENT): {
                "spec": {"template": {"spec": {"containers": [
                    {"name": "sidecar", "image": "registry.example.com/helper:1"},
                    {"name": "app", "image": f"{IMAGE}@{OLD_DIGEST}"},
                ]}}},
            },
        },
    )

    assert shepherd.main(*ARGS, "--query-command", "query-adapter") == 1
    report = capsys.readouterr().out
    assert f"https://github.com/{REPO}/pull/123" in report
    assert f"{IMAGE}:{MERGE}" in report
    assert DIGEST in report
    assert OLD_DIGEST in report
    assert "merged-but-not-deployed" in report


def deployment(generation=2, observed=2):
    return {
        "metadata": {"uid": "example-uid", "generation": generation},
        "spec": {"replicas": 2, "template": {"spec": {"containers": [
            {"name": "app", "image": f"{IMAGE}@{DIGEST}"},
        ]}}},
        "status": {
            "observedGeneration": observed, "updatedReplicas": 2,
            "availableReplicas": 2, "replicas": 2,
            "conditions": [{
                "type": "Progressing", "status": "True",
                "reason": "NewReplicaSetAvailable", "lastUpdateTime": "2026-01-01T00:01:00Z",
            }],
        },
    }


@pytest.mark.parametrize(
    ("target_counts", "baseline_counts", "status", "code"),
    [
        ((0, 1), (0,), "acceptance observed", 0),
        ((0, 0), (1, 2), "acceptance missing", 1),
        ((0, 0), (0, 0), "possible ingestion lag", 2),
    ],
)
def test_merge_to_acceptance_uses_post_rollout_window(
    monkeypatch, capsys, target_counts, baseline_counts, status, code,
):
    target = iter(map(str, target_counts))
    baseline = iter(map(str, baseline_counts))
    monkeypatch.setattr("shepherd_verify.time.time", lambda: int(NOW))
    stub_commands(monkeypatch, {
        tuple(PR): PR_DATA,
        tuple(MANIFEST): DIGEST,
        tuple(DEPLOYMENT): deployment(),
        ("query-adapter", "metric", "service.recovered", DEPLOY_EPOCH, NOW, CLAIM): target.__next__,
        ("query-adapter", "metric", "service.heartbeat", DEPLOY_EPOCH, NOW, ""): baseline.__next__,
    })

    assert shepherd.main(
        *ARGS, "--query-command", "query-adapter", "--retry-seconds", "0",
        "--baseline", "metric=service.heartbeat",
    ) == code
    report = capsys.readouterr().out
    assert status in report
    assert "rollout completed at 2026-01-01T00:01:00Z" in report
    assert "baseline 'service.heartbeat'" in report


def test_acceptance_is_unverified_if_deployment_changes_during_query(monkeypatch, capsys):
    monkeypatch.setattr("shepherd_verify.time.time", lambda: int(NOW))
    rollouts = iter((deployment(), deployment(generation=3, observed=3)))
    stub_commands(monkeypatch, {
        tuple(PR): PR_DATA,
        tuple(MANIFEST): DIGEST,
        tuple(DEPLOYMENT): rollouts.__next__,
        ("query-adapter", "metric", "service.recovered", DEPLOY_EPOCH, NOW, CLAIM): "1",
    })

    assert shepherd.main(*ARGS, "--query-command", "query-adapter") == 2
    assert "deployment changed during acceptance checks" in capsys.readouterr().out


def test_matching_pin_with_incomplete_rollout_never_runs_acceptance(monkeypatch, capsys):
    stub_commands(monkeypatch, {
        tuple(PR): PR_DATA,
        tuple(MANIFEST): DIGEST,
        tuple(DEPLOYMENT): deployment(observed=1),
    })

    assert shepherd.main(*ARGS, "--query-command", "query-adapter") == 2
    report = capsys.readouterr().out
    assert "generation 1/2" in report
    assert "post-merge rollout unverified" in report


def test_failed_manifest_lookup_keeps_build_unverified(monkeypatch, capsys):
    stub_commands(monkeypatch, {
        tuple(PR): PR_DATA,
        tuple(MANIFEST): subprocess.CompletedProcess(MANIFEST, 1, stdout="", stderr="denied"),
    })

    assert shepherd.main(*ARGS) == 2
    report = capsys.readouterr().out
    assert f"registry manifest {IMAGE}:{MERGE}" in report
    assert "artifact evidence unavailable" in report
