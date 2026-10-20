"""Tests for scripts/chief_check.py, the local Hunt Plan pre-flight.

The CLI calls Chief's own `evaluate_pull_request`, so these tests do not
re-test the rules. They pin the CLI's contract: the gh-backed fetchers build
the same facts Chief's webhook fetchers do, the exit code follows the
blocking verdict, the which-epic hint names the repo's epics, and a failed
read fails open the way Chief does. Every gh/git call goes through a fake
runner, so nothing touches the network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chief_check  # noqa: E402

REPO = "example-org/example-repo"

FULL_BODY = """## Why

Operators only learn the body fails the plan check after the PR is open.

## Acceptance criteria

- the first thing holds
- the second thing holds
- the third thing holds

Size: S

## Out of scope

Anything about the rules themselves.

Refs #12
"""

MISSING_FENCE_BODY = """## Why

Operators only learn the body fails the plan check after the PR is open.

## Acceptance criteria

- the first thing holds
- the second thing holds
- the third thing holds

Refs #12
"""


class FakeGh:
    """Answers the argv chief_check runs, keyed on what each one reads."""

    def __init__(self, *, parent: int | None = None, parent_error: bool = False):
        self.parent = parent
        self.parent_error = parent_error
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        if argv[:2] == ["gh", "api"]:
            path = argv[2]
            if path == f"repos/{REPO}/issues/12":
                return json.dumps({
                    "number": 12,
                    "title": "Make the plan check runnable locally",
                    "body": "Touches scripts/chief_check.py and its tests.",
                    "labels": [{"name": "enhancement"}],
                    "sub_issues_summary": {"total": 0},
                })
            if path == f"repos/{REPO}/issues/12/parent":
                if self.parent_error:
                    raise RuntimeError("connection reset")
                if self.parent is None:
                    raise chief_check.GhError("gh: Not Found (HTTP 404)")
                return json.dumps({"number": self.parent})
        if argv[:3] == ["gh", "issue", "list"]:
            return json.dumps([
                {"number": 7, "title": "Epic: plan checks people can trust"},
                {"number": 9, "title": "Not an epic, mentions Epic: in passing"},
            ])
        if argv[:2] == ["git", "diff"]:
            return "scripts/chief_check.py\nscripts/tests/test_chief_check.py\n"
        raise AssertionError(f"unexpected command: {argv}")


def _run(tmp_path: Path, body: str, gh: FakeGh, capsys) -> tuple[int, str]:
    f = tmp_path / "body.md"
    f.write_text(body)
    code = chief_check.main(["--repo", REPO, "--body-file", str(f)], run=gh)
    return code, capsys.readouterr().out


def test_orphan_ticket_fails_which_epic_and_names_epics(tmp_path, capsys):
    code, out = _run(tmp_path, FULL_BODY, FakeGh(parent=None), capsys)
    assert code == 1
    assert "| which epic | fail |" in out
    assert "#7 Epic: plan checks people can trust" in out
    # Only titles that START with "Epic:" are epics; a search hit is not.
    assert "#9" not in out
    assert "sub_issues" in out and "Part of #<epic>" in out


def test_ticket_with_parent_passes(tmp_path, capsys):
    code, out = _run(tmp_path, FULL_BODY, FakeGh(parent=7), capsys)
    assert code == 0
    assert "Hunt Plan ready" in out
    assert "| which epic | pass |" in out


def test_missing_fence_and_size_fail(tmp_path, capsys):
    code, out = _run(tmp_path, MISSING_FENCE_BODY, FakeGh(parent=7), capsys)
    assert code == 1
    assert "| where stop | fail |" in out
    assert "| how big | fail |" in out


def test_parent_read_error_fails_open_like_chief(tmp_path, capsys):
    code, out = _run(tmp_path, FULL_BODY, FakeGh(parent_error=True), capsys)
    assert code == 0
    assert "| which epic | skipped |" in out


def test_pr_mode_reads_body_and_files_from_the_pr(capsys):
    calls: list[list[str]] = []

    def gh(argv: list[str]) -> str:
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return FULL_BODY
        if argv[:3] == ["gh", "pr", "diff"]:
            return "scripts/chief_check.py\n"
        return FakeGh(parent=7)(argv)

    code = chief_check.main(["--repo", REPO, "--pr", "5"], run=gh)
    assert code == 0
    assert "Hunt Plan ready" in capsys.readouterr().out
    assert ["gh", "pr", "view", "5", "-R", REPO, "--json", "body", "--jq", ".body"] in calls


def test_changed_files_come_from_the_pr_or_the_local_branch():
    # in-scope only reads the diff when the body uses a closing keyword,
    # which agent PRs must not, so pin the fetcher directly.
    calls: list[list[str]] = []

    def run(argv: list[str]) -> str:
        calls.append(argv)
        return "a.py\n\nb.py\n"

    *_, from_pr = chief_check.build_fetchers(run, REPO, 5, "origin/main")
    *_, from_branch = chief_check.build_fetchers(run, REPO, None, "origin/main")
    assert from_pr() == ["a.py", "b.py"]
    assert from_branch() == ["a.py", "b.py"]
    assert calls == [
        ["gh", "pr", "diff", "5", "-R", REPO, "--name-only"],
        ["git", "diff", "--name-only", "origin/main...HEAD"],
    ]


def test_usage_errors_exit_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        chief_check.main(["--repo", REPO], run=FakeGh())
    assert exc.value.code == 2
    assert chief_check.main(
        ["--repo", "no-slash", "--body-file", str(tmp_path / "x")], run=FakeGh(),
    ) == 2
    assert chief_check.main(
        ["--repo", REPO, "--body-file", str(tmp_path / "missing.md")], run=FakeGh(),
    ) == 2
