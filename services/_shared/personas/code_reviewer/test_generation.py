"""Find changed public Python functions no test in the PR covers, and
place generated tests for them.

Coverage here is deliberately simple and deterministic: a changed public
function is covered when some test file in the PR's changed-file contents
references its name. That name match is a heuristic: it can over-count
(a same-named helper mentioned in passing) and under-count (a test that
reaches the function through a fixture or a re-export). The mapped test
file for the function's own module is the primary evidence; anything
richer (a coverage database, line data) belongs to the review that
produced the contents, not to this seam.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.test_paths import is_test_file


@dataclass(frozen=True)
class TestTarget:
    path: str       # the changed source file
    name: str       # public function (or Class.method) changed by the PR
    line: int
    end_line: int
    test_path: str  # where generated tests for `path` live


def test_path_for(path: str) -> str:
    """The test file for a source path: `tests/test_<stem>.py` beside the
    source's package directory (`pkg/mod.py` -> `pkg/tests/test_mod.py`)."""
    parts = path.split("/")
    stem = parts[-1].removesuffix(".py")
    return "/".join([*parts[:-1], "tests", f"test_{stem}.py"])


def _added_lines(diff_text: str) -> dict[str, set[int]]:
    added: dict[str, set[int]] = {}
    for hunk in parse_diff(diff_text):
        line = hunk.new_start
        changed: set[int] = set()
        for row in hunk.body.splitlines():
            if row.startswith("@@"):
                continue
            if row.startswith("+"):
                changed.add(line)
                line += 1
            elif row.startswith("-"):
                continue
            else:
                line += 1
        added.setdefault(hunk.file_path, set()).update(changed)
    return added


def _public_functions(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                yield node.name, node
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for child in node.body:
                if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and not child.name.startswith("_")):
                    yield f"{node.name}.{child.name}", child


def find_test_targets(
    diff_text: str, file_contents: dict[str, str],
) -> tuple[list[TestTarget], list[tuple[str, str]]]:
    """Changed public functions no test file in `file_contents` references,
    plus every changed Python path that could not be read or parsed."""
    changed = _added_lines(diff_text)
    test_texts = [
        text for path, text in file_contents.items() if is_test_file(path)
    ]
    targets: list[TestTarget] = []
    skipped: list[tuple[str, str]] = []
    for path in sorted(changed):
        if not path.endswith(".py") or is_test_file(path):
            continue
        source = file_contents.get(path)
        if source is None:
            skipped.append((path, "could not read changed Python file"))
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            skipped.append((path, "Python file does not parse"))
            continue
        lines = changed[path]
        for name, node in _public_functions(tree):
            if not any(node.lineno <= line <= node.end_lineno for line in lines):
                continue
            bare = name.rsplit(".", 1)[-1]
            if any(re.search(rf"\b{re.escape(bare)}\b", text) for text in test_texts):
                continue
            targets.append(TestTarget(
                path, name, node.lineno, node.end_lineno, test_path_for(path),
            ))
    return targets, skipped


def merge_generated_tests(existing: str | None, generated: str) -> str:
    """Append generated test code to a test file's current text (or start
    the file with it). Raises ValueError when the generated code is empty
    or the merged file would not parse."""
    snippet = (generated or "").strip()
    if not snippet:
        raise ValueError("generated test code is empty")
    try:
        ast.parse(snippet)
    except SyntaxError as exc:
        raise ValueError("generated test code does not parse") from exc
    if existing and existing.strip():
        merged = existing.rstrip("\n") + "\n\n\n" + snippet + "\n"
    else:
        merged = snippet + "\n"
    try:
        ast.parse(merged)
    except SyntaxError as exc:
        raise ValueError("merged test file does not parse") from exc
    return merged
