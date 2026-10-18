"""#1238: a rollout-status timeout must leave evidence behind.

deploy.k8s run 39097624374 timed out waiting for an old grug-webhook pod to
terminate, the auto-rollback (#499) restarted every deployment, and the only
evidence of why the pod was stuck was gone: the rollout loop captured nothing.
These tests run scripts/rollout_gate.sh and the auto-rollback step against a
fake `kubectl`, the same way test_registry_login runs its action against a
fake `docker`.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_GATE = _ROOT / "scripts" / "rollout_gate.sh"
_WORKFLOWS = _ROOT / ".github" / "workflows"

# A fake kubectl: logs every call, fails `rollout status` for the
# deployments named in $FAIL_ROLLOUT, answers the selector and image reads,
# and echoes a recognisable marker for every other read. With $FAIL_READS
# set, every read other than the rollout itself fails too.
_FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
args="$*"
case "$args" in
  *"rollout status"*)
    for d in $FAIL_ROLLOUT; do
      case "$args" in *"deploy/$d "*) echo "error: timed out waiting for the condition" >&2; exit 1 ;; esac
    done
    echo "deployment successfully rolled out"; exit 0 ;;
esac
[ -n "${FAIL_READS:-}" ] && { echo "error: the server is unreachable" >&2; exit 1; }
case "$args" in
  *"go-template="*) printf 'app=grug-webhook,' ;;
  *"configmap grug-last-good"*"data.webhook"*) printf '%s' "$LAST_GOOD_W" ;;
  *"configmap grug-last-good"*"data.api"*) printf '%s' "$LAST_GOOD_A" ;;
  *"configmap grug-last-good"*"recorded_from_sha"*) printf 'abc1234' ;;
  *"get nodes"*) printf '' ;;
  *"get deploy grug-webhook -o jsonpath"*) printf '%s' "$LIVE_W" ;;
  *"get deploy grug-api -o jsonpath"*) printf '%s' "$LIVE_A" ;;
  *) echo "FAKE-OUT[$args]" ;;
esac
"""


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("kubectl", _FAKE_KUBECTL),
        ("git", "#!/usr/bin/env bash\nexit 0\n"),
    ):
        f = bin_dir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        PATH=f"{_fake_bin(tmp_path)}{os.pathsep}{env['PATH']}",
        CALLS=str(tmp_path / "calls"),
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"),
        FAIL_ROLLOUT="",
    )
    env.update(extra)
    (tmp_path / "calls").write_text("")
    (tmp_path / "summary.md").write_text("")
    return env


def _gate(tmp_path: Path, **extra: str) -> tuple[subprocess.CompletedProcess, list[str], str]:
    env = _env(tmp_path, **extra)
    proc = subprocess.run(
        ["bash", str(_GATE), "grug", "180s", "grug-webhook", "grug-api", "grug-consumer"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    calls = (tmp_path / "calls").read_text().splitlines()
    return proc, calls, (tmp_path / "summary.md").read_text()


def test_gate_passes_quietly_when_every_rollout_finishes(tmp_path):
    proc, calls, summary = _gate(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [c for c in calls if "rollout status" in c] == [
        "-n grug rollout status deploy/grug-webhook --timeout=180s",
        "-n grug rollout status deploy/grug-api --timeout=180s",
        "-n grug rollout status deploy/grug-consumer --timeout=180s",
    ]
    assert summary == ""


def test_gate_captures_the_stuck_state_into_the_summary_before_failing(tmp_path):
    proc, calls, summary = _gate(tmp_path, FAIL_ROLLOUT="grug-webhook")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    # Stops at the first stuck deployment: the rollback must not wait
    # behind two more 180s timeouts.
    assert not any("deploy/grug-api" in c and "rollout status" in c for c in calls)
    # The capture the issue asks for, scoped to the failing deployment's
    # pods by its own selector.
    for read in (
        "-n grug get pods -o wide",
        "-n grug describe pods -l app=grug-webhook",
        "-n grug get events --sort-by=.lastTimestamp",
        "-n grug describe deploy/grug-webhook",
        "-n grug get rs -l app=grug-webhook -o wide",
    ):
        assert read in calls, (read, calls)
    term = [c for c in calls if "custom-columns" in c]
    assert term and "deletionTimestamp" in term[0] and "finalizers" in term[0]
    # The output lands in the step summary, which survives the rollback.
    assert "rollout of deploy/grug-webhook did not finish within 180s" in summary
    assert "FAKE-OUT[-n grug describe pods -l app=grug-webhook]" in summary
    assert "FAKE-OUT[-n grug get pods -o wide]" in summary
    assert "FAKE-OUT[-n grug get events --sort-by=.lastTimestamp]" in summary
    assert "::error::rollout of deploy/grug-webhook" in proc.stdout


def test_a_failing_diagnostic_never_masks_the_rollout_failure(tmp_path):
    proc, _calls, summary = _gate(tmp_path, FAIL_ROLLOUT="grug-api", FAIL_READS="1")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "rollout of deploy/grug-api did not finish" in summary
    assert "the server is unreachable" in summary


def test_gate_rejects_a_call_without_deployments(tmp_path):
    proc = subprocess.run(
        ["bash", str(_GATE), "grug", "180s"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2


def _run_blocks(workflow: str) -> list[str]:
    spec = yaml.safe_load((_WORKFLOWS / workflow).read_text())
    return [
        step["run"]
        for job in spec["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]


def test_production_rollouts_go_through_the_gate():
    """Every rollout wait on the production namespace, in the deploy and
    both rollback paths, goes through the gate - a bare `rollout status`
    is the path that captured nothing in run 39097624374."""
    for wf in ("deploy.k8s.yml", "deploy.rollback.yml"):
        runs = "\n".join(_run_blocks(wf))
        bare = [
            line.strip()
            for line in runs.splitlines()
            if re.search(r"^\s*kubectl\b.*\brollout status\b", line)
        ]
        assert bare == [], f"{wf}: bare rollout status (use scripts/rollout_gate.sh): {bare}"
        assert "bash scripts/rollout_gate.sh grug " in runs, wf
    deploy = yaml.safe_load((_WORKFLOWS / "deploy.k8s.yml").read_text())
    # yaml parses the bare `on:` key as True.
    paths = deploy[True]["push"]["paths"]
    assert "scripts/rollout_gate.sh" in paths


def _rollback_step() -> str:
    spec = yaml.safe_load((_WORKFLOWS / "deploy.k8s.yml").read_text())
    (step,) = [
        s for s in spec["jobs"]["deploy"]["steps"]
        if s.get("name", "").startswith("Auto-rollback to last-good")
    ]
    return step["run"]


def _run_rollback(tmp_path: Path, live_w: str, live_a: str) -> tuple[int, str]:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    for stem in ("grug-secrets", "registry-pull-grug", "registry-pull-trial"):
        (runner_temp / f"{stem}.absent").write_text("")
    script = tmp_path / "rollback.sh"
    script.write_text(_rollback_step())
    env = _env(
        tmp_path,
        RUNNER_TEMP=str(runner_temp),
        GITHUB_SHA="f" * 40,
        LAST_GOOD_W="registry.invalid/grug-webhook@sha256:aaaa",
        LAST_GOOD_A="registry.invalid/grug-api@sha256:bbbb",
        LIVE_W=live_w,
        LIVE_A=live_a,
    )
    proc = subprocess.run(
        ["bash", str(script)], cwd=_ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, (tmp_path / "summary.md").read_text()


def test_rollback_summary_names_the_digests_it_restored(tmp_path):
    code, summary = _run_rollback(
        tmp_path,
        live_w="registry.invalid/grug-webhook@sha256:cccc",
        live_a="registry.invalid/grug-api@sha256:dddd",
    )
    assert code == 1  # a rollback always fails the run, loudly
    assert "restored webhook image (webhook, consumer, cronjobs): sha256:aaaa (was sha256:cccc)" in summary
    assert "restored api image: sha256:bbbb (was sha256:dddd)" in summary
    assert "NO-OP" not in summary
    assert "ROLLED BACK to last-good (from abc1234)" in summary
    # Public repo: the registry host in the image ref is never printed.
    assert "registry.invalid" not in summary


def test_rollback_summary_flags_a_no_op_restore(tmp_path):
    code, summary = _run_rollback(
        tmp_path,
        live_w="registry.invalid/grug-webhook@sha256:aaaa",
        live_a="registry.invalid/grug-api@sha256:bbbb",
    )
    assert code == 1
    assert "image restore was a NO-OP" in summary


def test_webhook_termination_grace_is_an_explicit_decision():
    docs = list(yaml.safe_load_all((_ROOT / "k8s" / "webhook-deployment.yaml").read_text()))
    (dep,) = [d for d in docs if d and d.get("kind") == "Deployment"]
    assert dep["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 30
