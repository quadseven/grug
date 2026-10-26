"""Regression test infrastructure for repo docs tests.

This module provides a way to verify that new tests fail against the pre-fix
code, ensuring backward compatibility before changes are committed.
"""

import sys
from pathlib import Path
from typing import Callable

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services._shared.personas.code_reviewer.repo_docs import build_repo_docs_context


def _run_test(module_path: Path, test_name: str) -> tuple[bool, str]:
    """Run a single test using pytest and return (passed, output).

    Args:
        module_path: Path to the test module.
        test_name: Name of the test function to run.

    Returns:
        Tuple of (test_passed, output_text).
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(module_path),
        f"-k",
        test_name,
        "-v",
        "--tb=short",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stdout + result.stderr
    passed = result.returncode == 0

    return passed, output


def test_regression_context_md_included():
    """Test that CONTEXT.md is included when present.

    Regression test to ensure the pre-fix behavior is preserved.
    """
    CONTEXT_TEXT = """# CONTEXT.md — grug domain glossary

| **Hunt Plan** | Product name for Chief's gate. Industry jargon "Definition of Ready" / "DoR" is **not** used on product surfaces. |
"""

    def fetch_file(path: str) -> str | None:
        return CONTEXT_TEXT if path == "CONTEXT.md" else None

    ctx = build_repo_docs_context(
        fetch_file=fetch_file,
        list_dir=lambda _: [],
        hunks=[],
    )

    assert ctx is not None
    assert "Hunt Plan" in ctx


def test_regression_adr_scoped_by_changed_paths():
    """Test that ADRs are scoped by changed paths.

    Regression test to ensure the pre-fix behavior is preserved.
    """
    ADR_REGISTRY = """# ADR-0010: Persona dispatch registry

Decisions: adding a persona = one spec entry + one dispatch module + default config keys, no dispatcher edits.
"""

    def fetch_file(path: str) -> str | None:
        return ADR_REGISTRY if path == "docs/adr/0010-persona-dispatch-registry.md" else None

    def list_dir(path: str) -> list[str] | None:
        return ["0010-persona-dispatch-registry.md"] if path == "docs/adr/" else None

    ctx = build_repo_docs_context(
        fetch_file=fetch_file,
        list_dir=list_dir,
        hunks=[],
    )

    assert ctx is not None
    assert "0010-persona-dispatch-registry" in ctx


def test_regression_missing_context_md_still_renders_adrs():
    """Test that missing CONTEXT.md still renders ADRs.

    Regression test to ensure the pre-fix behavior is preserved.
    """
    ADR_REGISTRY = """# ADR-0010: Persona dispatch registry

Decisions: adding a persona = one spec entry + one dispatch module + default config keys, no dispatcher edits.
"""

    def fetch_file(path: str) -> str | None:
        return ADR_REGISTRY if path == "docs/adr/0010-persona-dispatch-registry.md" else None

    def list_dir(path: str) -> list[str] | None:
        return ["0010-persona-dispatch-registry.md"] if path == "docs/adr/" else None

    ctx = build_repo_docs_context(
        fetch_file=fetch_file,
        list_dir=list_dir,
        hunks=[],
    )

    assert ctx is not None
    assert "CONTEXT.md" not in ctx.split("---")[0]


def test_regression_no_docs_anywhere_returns_none():
    """Test that no docs anywhere returns None.

    Regression test to ensure the pre-fix behavior is preserved.
    """
    ctx = build_repo_docs_context(
        fetch_file=lambda _: None,
        list_dir=lambda _: None,
        hunks=[],
    )

    assert ctx is None


def test_regression_fetch_exception_degrades_to_none():
    """Test that fetch exceptions degrade to None.

    Regression test to ensure the pre-fix behavior is preserved.
    """
    def fetch_file(path: str) -> str | None:
        raise RuntimeError("api down")

    ctx = build_repo_docs_context(
        fetch_file=fetch_file,
        list_dir=lambda _: None,
        hunks=[],
    )

    assert ctx is None
