"""Tests for the deletion-only review (#1401).

Recorded shape: an infrastructure-repo revert deletes a workflow, the
script it runs and the script's tests, 255 lines removed and none added.
The parser skips `+++ /dev/null` blocks, so Elder had no hunks and answered "Grug eyes
clouded (no_diff)" on both passes, which never satisfies a blocking gate.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from llm_client import LlmReviewResponse
from personas import review_publish
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.removed_files import (
    RemovedReferenceCheckIncomplete,
    find_referrer_candidates,
    removed_paths,
    removed_reference_findings,
    scan_guarded_deletions,
    scan_removed_references,
    search_term,
)

_WORKFLOW = ".github/workflows/check.isolation.yml"
_SCRIPT = "ops/scripts/isolation/check.sh"
def _deleted(*paths: str) -> str:
    return "".join(
        f"diff --git a/{p} b/{p}\n"
        "deleted file mode 100644\n"
        "index 39dc28b90..000000000\n"
        f"--- a/{p}\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-line one\n"
        "-line two\n"
        for p in paths
    )


_DELETION_DIFF = (
    f"diff --git a/{_WORKFLOW} b/{_WORKFLOW}\n"
    "deleted file mode 100644\n"
    "index 39dc28b90..000000000\n"
    f"--- a/{_WORKFLOW}\n"
    "+++ /dev/null\n"
    "@@ -1,3 +0,0 @@\n"
    "-name: Check isolation\n"
    "-on: pull_request\n"
    "-jobs: {}\n"
    f"diff --git a/{_SCRIPT} b/{_SCRIPT}\n"
    "deleted file mode 100755\n"
    "index 1a2b3c4d5..000000000\n"
    f"--- a/{_SCRIPT}\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-#!/usr/bin/env bash\n"
    "-kubectl auth can-i get pods\n"
)


def test_removed_paths_lists_only_deleted_files():
    diff = _DELETION_DIFF + (
        "diff --git a/src/x.py b/src/x.py\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert removed_paths(diff) == (_WORKFLOW, _SCRIPT)


def test_search_term_is_the_parent_and_name():
    assert search_term(_SCRIPT) == "isolation/check.sh"
    assert search_term("setup.sh") == "setup.sh"


def test_a_file_that_still_names_a_removed_script_is_flagged_high():
    contents = {
        ".github/workflows/nightly.yml": (
            "jobs:\n  guard:\n    steps:\n"
            "      - run: bash ops/scripts/isolation/check.sh\n"
        ),
    }
    found = scan_removed_references((_WORKFLOW, _SCRIPT), contents)
    assert len(found) == 1
    finding = found[0]
    assert finding.rule_name == "removed-but-referenced"
    assert finding.severity == "high"
    assert finding.file == _SCRIPT
    assert ".github/workflows/nightly.yml:4" in finding.message
    assert finding.outside_diff_reason


def test_a_doc_only_reference_is_advisory():
    contents = {"docs/runbook.md": f"Run `{_SCRIPT}` first.\n"}
    found = scan_removed_references((_SCRIPT,), contents)
    assert [f.severity for f in found] == ["medium"]


def test_a_longer_name_that_merely_contains_the_path_is_not_a_reference():
    contents = {"Makefile": "test:\n\tbash ops/isolation/check.sh.bak\n"}
    assert scan_removed_references((_SCRIPT,), contents) == ()


def test_a_path_relative_to_the_referrer_is_a_reference():
    contents = {"ops/scripts/run-all.sh": "bash isolation/check.sh\n"}
    assert [f.severity for f in scan_removed_references((_SCRIPT,), contents)] == ["high"]


def test_a_same_named_file_in_another_directory_is_not_a_reference():
    contents = {"ops/run.sh": "bash other/isolation/check.sh\n"}
    assert scan_removed_references((_SCRIPT,), contents) == ()


def test_a_removed_file_naming_another_removed_file_is_not_a_reference():
    contents = {_WORKFLOW: f"run: {_SCRIPT}\n"}
    assert scan_removed_references((_WORKFLOW, _SCRIPT), contents) == ()


# Guarded paths: a deletion that removes a guard by its path alone.


@pytest.mark.parametrize("path", [
    ".github/workflows/leak-scan.yml",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "infra/github/rulesets/main.json",
    "config/branch-protection.yaml",
    "services/security/allowlist.py",
    "tools/leak-scan/deny.txt",
])
def test_deleting_a_guard_path_is_a_blocking_finding(path):
    found = scan_guarded_deletions((path,), ())
    assert [(f.rule_name, f.severity, f.file) for f in found] == [
        ("removed-guarded-path", "high", path),
    ]
    assert found[0].outside_diff_reason


def test_deleting_tests_whose_code_stays_removes_coverage():
    found = scan_guarded_deletions(("src/billing/tests/test_refunds.py",), ())
    assert [f.severity for f in found] == ["high"]
    assert "test coverage" in found[0].message


def test_tests_deleted_with_the_code_they_covered_are_not_a_guard():
    removed = (_SCRIPT, "ops/scripts/isolation/tests/test_check.py")
    assert scan_guarded_deletions(removed, ()) == ()


def test_an_ordinary_file_is_not_a_guard():
    assert scan_guarded_deletions(("src/old_helper.py", "docs/old.md"), ()) == ()


def test_a_linked_criterion_naming_the_removal_makes_it_advisory():
    criteria = (f"`{_WORKFLOW}` no longer exists on main.",)
    found = scan_guarded_deletions((_WORKFLOW,), criteria)
    assert [f.severity for f in found] == ["medium"]


# The referrer check never vouches for what it could not finish.


def test_more_deletions_than_the_search_cap_is_incomplete():
    many = tuple(f"src/m{i}.py" for i in range(6))
    with pytest.raises(RemovedReferenceCheckIncomplete):
        find_referrer_candidates("t", "o", "r", many)


def test_a_failed_search_is_incomplete():
    with (
        patch("httpx.get", side_effect=httpx.ConnectError("down")),
        pytest.raises(RemovedReferenceCheckIncomplete),
    ):
        find_referrer_candidates("t", "o", "r", (_SCRIPT,))


def test_a_rate_limited_search_is_incomplete():
    limited = MagicMock(spec=httpx.Response)
    limited.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "403", request=MagicMock(), response=limited,
    ))
    with (
        patch("httpx.get", return_value=limited),
        pytest.raises(RemovedReferenceCheckIncomplete),
    ):
        find_referrer_candidates("t", "o", "r", (_SCRIPT,))


def test_a_referrer_unreadable_at_head_is_incomplete():
    with pytest.raises(RemovedReferenceCheckIncomplete):
        removed_reference_findings(
            (_SCRIPT,), search=lambda p: ("ops/run.sh",), fetch_at_head=lambda p: {},
        )


# Dispatch: the real deletion-only path.


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry", lambda inst_id, fn: fn("fake-token"),
    )
    monkeypatch.setattr(
        review_publish, "with_install_token_retry", lambda inst_id, fn: fn("fake-token"),
    )


def _payload() -> dict:
    return {
        "action": "opened",
        "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {
            "number": 7,
            "head": {"sha": "abcd1234efgh"},
            "base": {"sha": "base5678ijkl"},
            "title": "Revert the isolation check",
            "body": "Removes the check and its workflow.",
            "user": {"login": "alice"},
        },
    }


def _response(*, text: str = "", json: object = None, status: int = 200):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.text = text
    r.content = text.encode()
    r.json = MagicMock(return_value=json)
    if status >= 400:
        r.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
            "missing", request=MagicMock(), response=r,
        ))
    else:
        r.raise_for_status = MagicMock()
    return r


def _run(monkeypatch, diff: str, referrers: dict[str, str], *,
         blocking: bool = True, search_error: Exception | None = None) -> list:
    posted_check: list = []

    def _fake_get(url, *args, **kwargs):
        if "/search/code" in url:
            if search_error is not None:
                raise search_error
            return _response(json={
                "total_count": len(referrers),
                "items": [{"path": p} for p in referrers],
            })
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            if path in referrers:
                return _response(text=referrers[path])
            return _response(status=404)
        if "/git/trees/" in url:
            return _response(json={"tree": []})
        return _response(text=diff)

    monkeypatch.setattr(
        cr_dispatch, "review_diff",
        lambda evidence, **kw: LlmReviewResponse(kind="no_diff"),
    )
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted_check.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **k: {"id": 2})
    with patch("httpx.get", side_effect=_fake_get):
        cr_dispatch.dispatch_code_review(_payload(), blocking=blocking)
    return posted_check


def test_deleting_an_ordinary_unreferenced_file_passes(monkeypatch):
    final = _run(monkeypatch, _deleted("src/old_helper.py"), {})[-1]
    assert final.conclusion == "success", (final.conclusion, final.title)
    assert "no_diff" not in final.title
    assert "clouded" not in final.title
    assert "1 deleted file(s)" in final.summary


def test_deleting_a_workflow_file_blocks(monkeypatch):
    final = _run(monkeypatch, _DELETION_DIFF, {})[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert "removed-guarded-path" in final.summary
    assert _WORKFLOW in final.summary


def test_a_deletion_that_something_still_relies_on_blocks(monkeypatch):
    final = _run(monkeypatch, _deleted(_SCRIPT), {
        ".github/workflows/nightly.yml": f"steps:\n  - run: bash {_SCRIPT}\n",
    })[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert "removed-but-referenced" in final.summary
    assert ".github/workflows/nightly.yml" in final.summary


def test_a_referrer_check_that_raises_is_neutral_not_success(monkeypatch):
    final = _run(
        monkeypatch, _deleted("src/old_helper.py"), {},
        blocking=False, search_error=httpx.ConnectError("search down"),
    )[-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)
    assert "removed_reference_check_failed" in final.title


def test_a_referrer_check_that_raises_never_passes_a_blocking_gate(monkeypatch):
    final = _run(
        monkeypatch, _deleted("src/old_helper.py"), {},
        search_error=httpx.ConnectError("search down"),
    )[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
