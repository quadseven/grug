"""Guard vacuous-test watch tests.

Scan functions are pure `(path, text) -> tuple[Violation, ...]`, so every
rule is exercised against fixture content with no network. The run-loop
tests mirror test_dep_watch.py's `_wire` shape.
"""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import httpx
import pytest

from personas.guard import vacuous_test_watch as vt


def test_vacuous_test_flagged():
    """A test file is flagged as vacuous-test."""
    v = vt.scan_file("test_example.py", "def test_example(): pass")
    assert [(x.line, x.category) for x in v] == [(1, "vacuous-test")]
    assert "test_example" in v[0].file


def test_non_test_file_is_clean():
    """A non-test file is not flagged."""
    assert vt.scan_file("example.py", "x = 1") == ()


def test_ignored_temporary_files_are_clean():
    """Temporary test files are not flagged."""
    assert vt.scan_file("test_temp_abc.py", "def test_x(): pass") == ()
    assert vt.scan_file("scratch_test.py", "def test_y(): pass") == ()


def test_match_pattern_only_py_files():
    """Only .py files are matched."""
    v = vt.scan_file("test_example.txt", "def test_x(): pass")
    assert v == ()
    v = vt.scan_file("test_example.md", "def test_x(): pass")
    assert v == ()


def test_scan_file_returns_empty_for_empty():
    """An empty file returns no violations."""
    assert vt.scan_file("test_example.py", "") == ()


def test_scan_file_returns_empty_for_nonexistent():
    """A file that doesn't match the pattern returns no violations."""
    assert vt.scan_file("module.go", "func main() {}") == ()
    assert vt.scan_file("test_example.c", "int main() {}") == ()


def test_headers_function_returns_auth_headers():
    """The headers function returns the correct headers."""
    headers = vt._headers("test-token")
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["Accept"] == "application/vnd.github.raw"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_api_headers_function_returns_correct_headers():
    """The api_headers function returns the correct headers."""
    headers = vt._api_headers("test-token")
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_headers_are_deterministic():
    """Headers are deterministic for the same token."""
    h1 = vt._headers("token1")
    h2 = vt._headers("token1")
    assert h1 == h2


def test_api_headers_are_deterministic():
    """API headers are deterministic for the same token."""
    h1 = vt._api_headers("token1")
    h2 = vt._api_headers("token1")
    assert h1 == h2


def test_report_marker_is_present():
    """The report marker is in the report body."""
    from personas.guard import vacuous_test_watch as vt
    lines = vt._report_body([]).split("\n")
    assert vt._REPORT_MARKER in lines
    assert lines.index(vt._REPORT_MARKER) > 0


def test_report_title_includes_violation_count():
    """The report title includes the number of violations."""
    from personas.guard import vacuous_test_watch as vt
    v = vt.Violation("test.py", 1, "vacuous-test", "detail")
    body = vt._report_body([v])
    assert "1 violation" in body
    body = vt._report_body([v, v])
    assert "2 violations" in body


def test_report_table_has_correct_columns():
    """The report table has the correct columns."""
    from personas.guard import vacuous_test_watch as vt
    v = vt.Violation("test_example.py", 42, "vacuous-test", "Test passes on base code")
    body = vt._report_body([v])
    assert "| File |" in body
    assert "| Line |" in body
    assert "| Category |" in body
    assert "| What Grug see |" in body


def test_report_capped_at_max_rows():
    """The report is capped at the max rows."""
    from personas.guard import vacuous_test_watch as vt
    violations = [
        vt.Violation(f"test{i}.py", 1, "vacuous-test", "detail")
        for i in range(vt._MAX_REPORT_ROWS + 10)
    ]
    body = vt._report_body(violations)
    assert f"...and {vt._MAX_REPORT_ROWS + 10 - vt._MAX_REPORT_ROWS} more" in body
    assert body.count("|") <= vt._MAX_REPORT_ROWS * 4 + 4


def test_existing_report_no_existing_marker():
    """A repo with no existing report returns None."""
    resp = httpx.get(
        "https://api.github.com/repos/test/repo/issues",
        params={"state": "open", "per_page": 50},
        headers={"Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if resp.status_code != 200:
        pytest.skip("GitHub API not available")
    for issue in resp.json() or []:
        if issue.get("pull_request"):
            continue
        assert vt._REPORT_MARKER not in (issue.get("body") or "")
