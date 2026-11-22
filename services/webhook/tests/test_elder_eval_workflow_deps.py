"""The Elder eval workflow installs every package the eval imports (grug#989).

`benchmark.elder-eval.yml` builds its environment from an explicit
`uv run --with ...` list rather than a requirements file. On 2026-11-16 the
record run died with `ModuleNotFoundError: No module named 'yaml'`, because
`repo_config.py` (imported at module load through quarantine.py) needs pyyaml
and the list did not name it. This test walks the module-level imports reachable
from `elder_eval` and checks each third-party one against that list.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "benchmark.elder-eval.yml"
SEARCH = [REPO / "services" / "webhook", REPO / "services" / "_shared"]
ENTRY = REPO / "services" / "webhook" / "elder_eval"

# Import name -> the distribution that provides it, where they differ.
DISTRIBUTION = {"yaml": "pyyaml", "jwt": "pyjwt", "psycopg_pool": "psycopg"}


def _resolve(module: str) -> Path | None:
    parts = module.split(".")
    for base in SEARCH:
        path = base.joinpath(*parts)
        for candidate in (path.with_suffix(".py"), path / "__init__.py"):
            if candidate.exists():
                return candidate
    return None


def _module_level(tree: ast.Module):
    """Statements that run at import, including inside top-level try/if."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.Try, ast.If)):
            stack += node.body + node.orelse + list(getattr(node, "finalbody", []))
            for handler in getattr(node, "handlers", []):
                stack += handler.body


def third_party_imports() -> dict[str, str]:
    todo = sorted(ENTRY.glob("*.py"))
    seen: set[Path] = set()
    found: dict[str, str] = {}
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in _module_level(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.ImportFrom) and node.level:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                target = base.joinpath(*(node.module or "").split(".")) if node.module else base
                todo += [c for c in (target.with_suffix(".py"), target / "__init__.py") if c.exists()]
                continue
            for name in names:
                local = _resolve(name) or _resolve(name.split(".")[0])
                if local:
                    todo.append(local)
                elif name.split(".")[0] not in sys.stdlib_module_names:
                    found.setdefault(name.split(".")[0], path.relative_to(REPO).as_posix())
    return found


def workflow_packages() -> set[str]:
    text = WORKFLOW.read_text()
    start = text.index("uv run --with")
    run = text[start:text.index("python -m elder_eval", start)]
    specs = re.findall(r"--with\s+'?([^'\s]+)'?", run)
    return {re.split(r"[\[<>=!~ ]", spec, maxsplit=1)[0].lower() for spec in specs}


class ElderEvalWorkflowInstallsWhatTheEvalImports(unittest.TestCase):
    def test_the_walk_reaches_the_shared_reviewer_code(self):
        self.assertIn("yaml", third_party_imports())

    def test_every_module_level_third_party_import_is_installed(self):
        installed = workflow_packages()
        missing = {
            name: where
            for name, where in third_party_imports().items()
            if DISTRIBUTION.get(name, name).lower() not in installed
        }
        self.assertEqual(missing, {}, "add these to the `uv run --with` list in benchmark.elder-eval.yml")


if __name__ == "__main__":
    unittest.main()
