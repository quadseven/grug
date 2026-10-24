"""Tests for the deletion-only review (#1401).

Recorded shape (an infrastructure-repo revert): it deletes a workflow, the script it
runs and the script's tests, 255 lines removed and none added. The parser
skips `+++ /dev/null` blocks, so Elder had no hunks and answered "Grug eyes
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
    removed_paths,
    scan_removed_references,
    search_term,
)

_WORKFLOW = ".github/workflows/check.isolation.yml"
_SCRIPT = "ops/scripts/isolation/check.sh"
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


def _run(monkeypatch, referrers: dict[str, str]) -> list:
    posted_check: list = []

    def _fake_get(url, *args, **kwargs):
        if "/search/code" in url:
            return _response(json={"items": [{"path": p} for p in referrers]})
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            if path in referrers:
                return _response(text=referrers[path])
            return _response(status=404)
        if "/git/trees/" in url:
            return _response(json={"tree": []})
        return _response(text=_DELETION_DIFF)

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
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)
    return posted_check


def test_a_deletion_only_pr_gets_a_real_verdict_not_no_diff(monkeypatch):
    posted = _run(monkeypatch, {})
    final = posted[-1]
    assert final.conclusion == "success", (final.conclusion, final.title)
    assert "no_diff" not in final.title
    assert "clouded" not in final.title
    assert "2 deleted file(s)" in final.summary


def test_a_deletion_that_something_still_relies_on_blocks(monkeypatch):
    posted = _run(monkeypatch, {
        ".github/workflows/nightly.yml": f"steps:\n  - run: bash {_SCRIPT}\n",
    })
    final = posted[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert "removed-but-referenced" in final.summary
    assert ".github/workflows/nightly.yml" in final.summary
