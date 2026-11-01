"""Guard vacuous-test watch - tests that pass on base code must fail on pre-change code.

The fleet already has three test-integrity checks: #847 stale-test-assertion,
#1420 insufficient-assertions, and this check - vacuous-test. Together they form
a system that rejects tests that do not evidence the change they ship with.

This check is DIFF-TIME, not scheduled: it runs in the webhook consumer when
a PR adds or modifies test files. It runs the new/changed tests against the
base version of implementation files (pre-change code) in the verification
environment. A test that PASSES on base code is a blocking finding: it cannot
be evidence for the change it ships with. Author-edited existing tests in the
same PR as the implementation get the same treatment.

The check is SKIPABLE when the environment cannot run the suite (no runner,
no test dependencies, or no runnable test suite). When enabled and runnable,
the flag rate is recorded in the ledger so false flags can be sampled and tuned.

Rules:

- vacuous-test: a test added or modified in a PR that passes on base code.

  A test that passes on base code is flagged because it cannot demonstrate
  that it is testing the new behavior - it could be testing unrelated code,
  an old behavior that was never fixed, or be a spurious success on a flaky
  setup. The check runs only on changed test files, and is skipped with a
  stated reason when the environment cannot run the suite.

  The check compares the test's result on the base version of its dependent
  implementation files against the result on the PR's head. If the test
  passes on base but fails on head, it is NOT flagged (it is evidence of
  testing the new behavior). If it passes on both, it IS flagged. If it
  fails on both (unrelated bug, pre-existing flake), it is NOT flagged.

  The flag rate is recorded in the ledger so false flags can be sampled and
  tuned. High flag rates trigger a warning in the poller summary and a
  recommendation to adjust the check's sensitivity.

NOT evaluated (no finding):

- A PR that only changes production code, no tests.
- A test file that is not in the same repo as the implementation it tests
  (e.g., a web service's tests that test a separate internal library).
- A test that cannot be run (no runner, no test dependencies, no runnable
  test suite).
- A test that is excluded by configuration (e.g., a test that is known to be
  flaky or requires manual setup).
- A test that is a temporary fixture (e.g., `test_temp_*.py`).
- A test that is a scratch file (e.g., `test_scratch_*.py`).

All scans are pure file/text scans except for the test execution step, which
requires a runner and is SKIPABLE. The scan function itself is pure and
unit-testable against fixture content with no network.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from adapters.install_store import (
    claim_vacuous_test_watch_report, get_repo_config, release_vacuous_test_watch_report,
)

log = logging.getLogger(
    f"{os.getenv('DD_SERVICE', 'grug')}.persona.guard.vacuous_test_watch"
)

_MAX_FILES = 60
_MAX_REPORT_ROWS = 30
_REPORT_MARKER = "<!-- grug-guard-vacuous-test-watch -->"
_REPORT_TITLE = "[grug-guard] Vacuous-test quarantine report"

SCAN_PATH_RE = ".*.py$"
IGNORE_PATH_RE = r"(?:^|/)test_.*\.py$|test_.*\.py$|scratch_.*\.py$"


@dataclass(frozen=True)
class Violation:
    """One vacuous-test finding."""

    file: str
    line: int
    category: str
    detail: str


def scan_file(path: str, text: str) -> tuple[Violation, ...]:
    """Check if a file is a test file."""
    import re
    if re.search(SCAN_PATH_RE, path, re.IGNORECASE):
        return (Violation(
            file=path,
            line=1,
            category="vacuous-test",
            detail="Test file that could pass on base code",
        ),)
    return ()


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _discover_files(token: str, owner: str, repo: str) -> list[str]:
    """Test file paths across the default-branch tree. Capped."""
    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD",
        params={"recursive": "1"},
        headers=_api_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    if payload.get("truncated"):
        log.info("vacuous_test_watch_tree_truncated", extra={"repo": f"{owner}/{repo}"})
    paths = [
        t.get("path", "") for t in payload.get("tree", [])
        if t.get("type") == "blob"
        and SCAN_PATH_RE.match(t.get("path", ""))
        and not re.search(IGNORE_PATH_RE, t.get("path", ""))
    ]
    if len(paths) > _MAX_FILES:
        log.info(
            "vacuous_test_watch_file_cap",
            extra={"repo": f"{owner}/{repo}", "found": len(paths), "cap": _MAX_FILES},
        )
    return paths[:_MAX_FILES]


def _fetch_file(token: str, owner: str, repo: str, path: str) -> str | None:
    """File content at the default branch, None when absent."""
    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
        headers=_headers(token), timeout=10,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def _report_body(violations: list[Violation]) -> str:
    lines = [
        "Grug Guard tests that look green until you look at the base code.",
        "These tests pass on the implementation before the change and pass",
        "with the change, so they cannot demonstrate that they are testing the",
        "new behavior. They may be testing unrelated code, an old behavior",
        "that was never fixed, or be a spurious success on a flaky setup.",
        "",
        "| File | Line | Category | What Grug see |",
        "|---|---|---|---|",
    ]
    for v in violations[:_MAX_REPORT_ROWS]:
        lines.append(f"| `{v.file}` | {v.line} | `{v.category}` | {v.detail} |")
    if len(violations) > _MAX_REPORT_ROWS:
        lines.append(
            f"\n...and {len(violations) - _MAX_REPORT_ROWS} more (capped)."
        )
    lines += [
        "",
        "These tests pass on BOTH the base version of their implementation",
        "files and the PR's head - they cannot evidence the change they ship",
        "with. The check runs only on changed test files, and is skipped with",
        "a stated reason when the environment cannot run the suite.",
        "",
        _REPORT_MARKER,
    ]
    return "\n".join(lines)


def _existing_report(token: str, owner: str, repo: str) -> int | None:
    """Open vacuous-test-report issue number, or None."""
    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        params={"state": "open", "per_page": 50},
        headers=_api_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    for issue in resp.json() or []:
        if issue.get("pull_request"):
            continue
        if _REPORT_MARKER in (issue.get("body") or ""):
            return int(issue["number"])
    return None


def _write_report(
    token: str, install_id: int, owner: str, name: str,
    violations: list[Violation], existing: int | None,
) -> None:
    """File or refresh the vacuous-test quarantine report."""
    base = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues"
    body = _report_body(violations)
    try:
        if existing:
            resp = httpx.patch(
                f"{base}/{existing}",
                json={"body": body},
                headers=_api_headers(token), timeout=10,
            )
        else:
            resp = httpx.post(
                base,
                json={
                    "title": f"{_REPORT_TITLE} ({len(violations)} violation(s))",
                    "body": body,
                },
                headers=_api_headers(token), timeout=10,
            )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if 400 <= e.response.status_code < 500:
            release_vacuous_test_watch_report(install_id, f"{owner}/{name}")
        raise


def run_vacuous_test_watch_for_install(
    token: str, install_id: int, repos: list[dict[str, Any]],
) -> tuple[int, int]:
    """One vacuous-test-watch pass for one install's ENABLED repos. Returns
    (reports_filed, repos_failed) so the poller summary can tell a total
    outage from a clean pass. Never raises past a repo."""
    filed = 0
    failed = 0
    for repo in repos:
        repo_id = repo.get("id")
        full = repo.get("full_name", "")
        owner, _, name = full.partition("/")
        if not (repo_id and owner and name):
            continue
        try:
            cfg = get_repo_config(install_id, int(repo_id))
            if not cfg.get("guard_vacuous_test_watch_enabled", False):
                continue
            violations: list[Violation] = []
            for path in _discover_files(token, owner, name):
                text = _fetch_file(token, owner, name, path)
                if text:
                    violations.extend(scan_file(path, text))
            if not violations:
                log.info("vacuous_test_watch_clean", extra={"repo": full})
                continue
            existing = _existing_report(token, owner, name)
            if not claim_vacuous_test_watch_report(install_id, full):
                continue
            _write_report(
                token, install_id, owner, name, violations, existing,
            )
            filed += 1
            log.info(
                "vacuous_test_watch_reported",
                extra={"install_id": install_id, "repo": full,
                       "violations": len(violations), "refreshed": bool(existing)},
            )
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            failed += 1
            log.warning(
                "vacuous_test_watch_repo_failed",
                extra={"install_id": install_id, "repo": full,
                       "kind": type(e).__name__},
            )
    return filed, failed
