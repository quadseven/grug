"""Every Datadog call in the composition root sits behind `_datadog_enabled`.

Datadog is off and its keys are revoked, so a Datadog provider built at all
fails the whole stack at configure time. Reading the program's syntax tree
(rather than running it) keeps this check free of Pulumi mocks."""

from __future__ import annotations

import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "__main__.py"
DATADOG_ROOTS = {"_datadog", "dd_monitors", "dd_rum", "dd_dashboard"}


def _root(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _gated_calls(tree: ast.Module) -> tuple[list[int], list[int]]:
    gated: list[int] = []
    ungated: list[int] = []

    def walk(node: ast.AST, under_gate: bool) -> None:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
                and node.test.id == "_datadog_enabled":
            for child in node.body:
                walk(child, True)
            for child in node.orelse:
                walk(child, under_gate)
            return
        if isinstance(node, ast.Call) and _root(node.func) in DATADOG_ROOTS:
            (gated if under_gate else ungated).append(node.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, under_gate)

    walk(tree, False)
    return gated, ungated


def test_every_datadog_call_is_behind_the_gate():
    gated, ungated = _gated_calls(ast.parse(MAIN.read_text()))
    assert gated, "expected the Datadog components to still exist behind the gate"
    assert ungated == [], f"Datadog calls outside `if _datadog_enabled:` at lines {ungated}"


def test_the_gate_defaults_off():
    src = MAIN.read_text()
    assert '_datadog_enabled = config.get_bool("datadog_enabled") or False' in src
    prod = (MAIN.parent / "Pulumi.prod.yaml").read_text()
    assert "datadog_enabled" not in prod
