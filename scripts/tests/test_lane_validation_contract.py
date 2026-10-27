"""The lane validation contract runs the CI commands lanes kept failing on (#1464).

A lane pull request went red in CI on the `attest elder evaluation purity`
step of check.attesters.yml because .loom/validation.conf never ran that
command before the push. These tests pin the command into the contract
and prove it passes on this tree with no secrets or extra dependencies.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / ".loom" / "validation.conf"
WORKFLOW = REPO / ".github" / "workflows" / "check.attesters.yml"
ELDER_PURITY = "python3 infra/scripts/attest_elder_evaluation_purity.py"


def _contract_lines() -> dict[str, str]:
    """The contract's `key: value` lines (comments and blanks skipped)."""
    lines: dict[str, str] = {}
    for raw in CONTRACT.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        assert sep, f"contract line has no key: {raw!r}"
        assert key not in lines, f"contract repeats key {key!r}"
        lines[key.strip()] = value.strip()
    return lines


def test_ci_still_runs_the_elder_purity_attester():
    # If CI drops the step, this contract line is no longer a mirror of it.
    assert f"run: {ELDER_PURITY}" in WORKFLOW.read_text()


def test_contract_affected_runs_the_elder_purity_attester():
    lines = _contract_lines()
    assert ELDER_PURITY in lines.get("affected", ""), (
        "the lane contract's affected command must run the elder purity "
        "attester CI runs in check.attesters.yml"
    )


def test_contract_affected_command_passes_without_secrets():
    argv = shlex.split(_contract_lines()["affected"])
    if argv[0] == "python3":
        # Run under this interpreter so the test does not depend on a
        # python3 shim on PATH; the attester is pure stdlib either way.
        argv[0] = sys.executable
    env = {"PATH": os.environ.get("PATH", ""), "LOOM_CHANGED_FILES": ""}
    # The argv comes from this repo's own contract file, the same trusted
    # source the lane runner executes it from.
    result = subprocess.run(  # noqa: S603
        argv,
        cwd=REPO,
        env=env,
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
