"""The Pulumi program manages nothing in Datadog (#1223, epic #1129).

Datadog is off and its keys are revoked. The operator's decision on #1129
was to `pulumi state delete` every Datadog resource and remove them from
the program, so the stack never needs a Datadog key again.

The first test runs the real composition root (`__main__.py`) under Pulumi
mocks and records every resource it registers and every SSM parameter it
reads. It sets the retired `datadog_enabled` flag to true on purpose: a
program that still carries Datadog code behind a flag fails here, not only
one that builds it by default.
"""

from __future__ import annotations

import ast
import runpy
import tomllib
from pathlib import Path

import pulumi
from pulumi.runtime import config as pulumi_config

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "__main__.py"

_RESOURCE_TYPES: list[str] = []
_SSM_READS: list[str] = []


class _ProgramMocks(pulumi.runtime.Mocks):
    def new_resource(self, args):  # type: ignore[override]
        _RESOURCE_TYPES.append(args.typ)
        return [args.name + "_id", args.inputs]

    def call(self, args):  # type: ignore[override]
        if args.token == "aws:ssm/getParameter:getParameter":
            name = args.args.get("name", "")
            _SSM_READS.append(name)
            return {"name": name, "value": "mock-value", "arn": f"arn:aws:ssm:us-east-1:000000000000:parameter{name}"}
        if args.token == "aws:iam/getOpenIdConnectProvider:getOpenIdConnectProvider":
            return {"arn": "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"}
        return {}


@pulumi.runtime.test
def _run_program():
    # `set_mocks` is one global slot shared with the other test modules, so
    # install this module's mocks (and config) right before the run.
    pulumi.runtime.set_mocks(_ProgramMocks(), project="grug", stack="prod", preview=False)
    pulumi_config.set_all_config(
        {
            "grug:env": "prod",
            "grug:domain": "example.com",
            "grug:datadog_enabled": "true",
        }
    )
    _RESOURCE_TYPES.clear()
    _SSM_READS.clear()
    runpy.run_path(str(MAIN), run_name="__main__")


def test_program_reads_no_datadog_key_and_registers_no_datadog_resource():
    # The decorator drains every outstanding registration before it returns.
    _run_program()

    # Sanity: the program really ran (the non-Datadog stack is non-trivial).
    assert "aws:sqs/queue:Queue" in _RESOURCE_TYPES, _RESOURCE_TYPES
    assert "/infra/llm/openrouter_api_key" in _SSM_READS, _SSM_READS

    datadog_reads = [n for n in _SSM_READS if n.startswith("/infra/datadog/")]
    assert datadog_reads == [], f"program reads Datadog keys: {datadog_reads}"
    datadog_types = sorted(
        {t for t in _RESOURCE_TYPES if t.startswith("datadog:") or t == "pulumi:providers:datadog"}
    )
    assert datadog_types == [], f"program registers Datadog resources: {datadog_types}"


def test_no_module_imports_pulumi_datadog():
    offenders = []
    for path in [MAIN, *sorted((ROOT / "components").glob("*.py"))]:
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] == "pulumi_datadog" for n in names):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"pulumi_datadog imported at {offenders}"


def test_pulumi_datadog_is_not_a_dependency():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert not [d for d in deps if d.lower().startswith("pulumi-datadog")], deps
