"""Find and patch public Python functions changed by a PR."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from personas.code_reviewer.diff_parser import parse_diff


@dataclass(frozen=True)
class DocstringTarget:
    path: str
    name: str
    line: int
    end_line: int


def _added_lines(diff_text: str) -> dict[str, set[int]]:
    added: dict[str, set[int]] = {}
    for hunk in parse_diff(diff_text):
        line = hunk.new_start
        changed: set[int] = set()
        for row in hunk.body.splitlines()[1:]:
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


def find_docstring_targets(
    diff_text: str, file_contents: dict[str, str],
) -> tuple[list[DocstringTarget], list[tuple[str, str]]]:
    """Return changed public functions lacking docs and paths that could not parse."""
    changed = _added_lines(diff_text)
    targets: list[DocstringTarget] = []
    skipped: list[tuple[str, str]] = []
    for path, lines in changed.items():
        if not path.endswith(".py"):
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
        for name, node in _public_functions(tree):
            if not any(node.lineno <= line <= node.end_lineno for line in lines):
                continue
            if ast.get_docstring(node, clean=False):
                continue
            targets.append(DocstringTarget(path, name, node.lineno, node.end_lineno))
    return targets, skipped


def insert_docstring(source: str, *, line: int, docstring: str) -> str:
    """Insert a generated docstring after the function signature at `line`."""
    tree = ast.parse(source)
    node = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno == line),
        None,
    )
    if node is None:
        raise ValueError("function no longer exists at the expected line")
    if node.body[0].lineno == node.lineno:
        raise ValueError("function body shares its definition line")
    lines = source.splitlines(keepends=True)
    indent = " " * (node.col_offset + 4)
    cleaned = docstring.strip().replace('"""', "'''")
    if not cleaned:
        raise ValueError("generated docstring is empty")
    body = cleaned.splitlines()
    rendered = indent + '"""' + body[0] + "\n"
    rendered += "".join(indent + part + "\n" for part in body[1:])
    if len(body) > 1:
        rendered += indent + '"""\n'
    else:
        rendered = indent + '"""' + body[0] + '"""\n'
    lines.insert(node.body[0].lineno - 1, rendered)
    updated = "".join(lines)
    ast.parse(updated)
    return updated
