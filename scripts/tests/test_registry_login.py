"""Registry login goes through one retried composite action.

deploy.k8s run 37360195280 (2026-10-05) failed its `Registry login` step
with `context deadline exceeded` seconds after joining the tailnet: one
transient miss on a single unretried `docker login` failed a main deploy and
paged the CI-on-main monitor. The fix is `.github/actions/registry-login`,
which retries with backoff; these tests pin that every workflow uses it and
that the retry loop actually retries, by running the action's script against
a fake `docker`.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_ACTION = _ROOT / ".github" / "actions" / "registry-login" / "action.yml"


def _action_step() -> dict:
    spec = yaml.safe_load(_ACTION.read_text())
    steps = spec["runs"]["steps"]
    assert len(steps) == 1, steps
    return steps[0]


def test_no_workflow_runs_docker_login_inline():
    offenders = [
        f.name
        for f in sorted((_ROOT / ".github" / "workflows").glob("*.yml"))
        if "docker login" in f.read_text()
    ]
    assert offenders == [], (
        "use ./.github/actions/registry-login instead of an inline docker login: "
        f"{offenders}"
    )


def _run_action(tmp_path: Path, fail_times: int, attempts: int) -> tuple[int, int, str]:
    """Run the action's script with a fake docker that fails `fail_times`
    logins before succeeding. Returns (exit code, docker calls, output)."""
    calls = tmp_path / "calls"
    calls.write_text("")
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'cat >/dev/null; echo x >> "{calls}"\n'
        f'n=$(wc -l < "{calls}"); [ "$n" -gt {fail_times} ]\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "REGISTRY_HOST": "registry.example",
        "REGISTRY_USERNAME": "u",
        "REGISTRY_PASSWORD": "p",
        "ATTEMPTS": str(attempts),
        "RETRY_DELAY_S": "0",
    }
    proc = subprocess.run(
        ["bash", "-c", _action_step()["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    n = len(calls.read_text().splitlines())
    return proc.returncode, n, proc.stdout + proc.stderr


def test_transient_failures_are_retried(tmp_path):
    code, n, out = _run_action(tmp_path, fail_times=2, attempts=4)
    assert code == 0, out
    assert n == 3


def test_gives_up_after_attempts_and_fails_loudly(tmp_path):
    code, n, out = _run_action(tmp_path, fail_times=99, attempts=3)
    assert code != 0
    assert n == 3
    assert "::error::" in out


def test_default_retries_more_than_once():
    inputs = yaml.safe_load(_ACTION.read_text())["inputs"]
    assert int(inputs["attempts"]["default"]) > 1
