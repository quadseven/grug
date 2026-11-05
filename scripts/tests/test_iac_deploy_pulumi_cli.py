"""The pulumi-up job installs a pinned Pulumi CLI before it first calls
`pulumi`, instead of relying on the runner image to ship one (#1655)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "iac.deploy.yml"
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_SHA_PIN = re.compile(r"^pulumi/actions@[0-9a-f]{40}$")


def _steps() -> list[dict]:
    data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["pulumi-up"]["steps"]


def _first_cli_call(steps: list[dict]) -> int:
    for i, step in enumerate(steps):
        if re.search(r"(^|\s)pulumi\s", step.get("run", "")):
            return i
    raise AssertionError("no run step calls the pulumi CLI")


def _pulumi_action_steps(steps: list[dict]) -> list[tuple[int, dict]]:
    return [
        (i, step)
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("pulumi/actions@")
    ]


def test_a_pinned_cli_install_runs_before_the_first_pulumi_call():
    steps = _steps()
    first_call = _first_cli_call(steps)
    installs = [
        (i, step)
        for i, step in _pulumi_action_steps(steps)
        if "command" not in step.get("with", {})
    ]
    assert installs, "no install-only pulumi/actions step in pulumi-up"
    index, step = installs[0]
    assert index < first_call, "the CLI install must run before pulumi login"
    assert _SHA_PIN.match(step["uses"]), step["uses"]
    version = str(step["with"].get("pulumi-version", ""))
    assert _EXACT_VERSION.match(version), f"pulumi-version must be exact, got {version!r}"


def test_every_pulumi_action_step_uses_the_same_exact_version():
    versions = {
        str(step.get("with", {}).get("pulumi-version", ""))
        for _i, step in _pulumi_action_steps(_steps())
    }
    assert len(versions) == 1, versions
    assert _EXACT_VERSION.match(versions.pop())
