"""grug#1649: a lock-file-only pull request concludes success, not neutral.

Lock files are dropped from Elder's plate (#609), so a diff made only of
them reached the model with nothing and published a neutral "eyes clouded
(no_diff)" that never clears a blocking gate. When every changed file is a
lock file, Elder concludes success with a title that says so. A diff that
mixes a lock file with a reviewable file is reviewed as before.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from llm_client import Backend, LlmReviewResponse
from personas import review_publish
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.diff_parser import is_lock_file_path

LOCK_TITLE = "lock files only: nothing for Elder to review"


def _modified(path: str, old: str = "a = 1", new: str = "a = 2") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 39dc28b90..1a2b3c4d5 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " keep\n"
        f"-{old}\n"
        f"+{new}\n"
    )


_LOCK_ONLY_DIFF = (
    _modified("pulumi/stack-a/uv.lock")
    + _modified("pulumi/stack-b/uv.lock")
    + _modified("web/package-lock.json")
)
_MIXED_DIFF = _modified("pulumi/stack-a/uv.lock") + _modified("src/app.py")


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
            "title": "Bump a dependency in the locks",
            "body": "Refreshes the lock files.",
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


def _run(monkeypatch, diff: str) -> tuple[list, list]:
    posted_check: list = []
    reviewed_paths: list = []

    def _fake_get(url, *args, **kwargs):
        if "/contents/" in url:
            return _response(status=404)
        if "/git/trees/" in url:
            return _response(json={"tree": []})
        if "/search/code" in url:
            return _response(json={"total_count": 0, "items": []})
        return _response(text=diff)

    def _review(evidence, **kw):
        # The real review_diff short-circuits to no_diff on empty hunks.
        paths = [h.path for h in evidence.hunks]
        reviewed_paths.append(paths)
        if not paths:
            return LlmReviewResponse(kind="no_diff")
        return LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
        )

    monkeypatch.setattr(cr_dispatch, "review_diff", _review)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted_check.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **k: {"id": 2})
    with patch("httpx.get", side_effect=_fake_get):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)
    return posted_check, reviewed_paths


@pytest.mark.parametrize("path", [
    "uv.lock", "pulumi/stack/uv.lock", "poetry.lock", "Cargo.lock",
    "web/package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.sum",
    "Gemfile.lock", "composer.lock",
])
def test_lock_file_paths_are_recognised(path):
    assert is_lock_file_path(path)


@pytest.mark.parametrize("path", [
    "src/app.py", "data/rows.jsonl", "vendor/lib/x.go", "dist/app.js",
    "docs/lockfile.md", "src/lock.py",
])
def test_other_paths_are_not_lock_files(path):
    assert not is_lock_file_path(path)


def test_a_lock_file_only_diff_concludes_success_with_a_clear_title(monkeypatch):
    checks, _ = _run(monkeypatch, _LOCK_ONLY_DIFF)
    final = checks[-1]
    assert final.conclusion == "success", (final.conclusion, final.title)
    assert final.title == LOCK_TITLE
    assert "no_diff" not in final.title
    assert "pulumi/stack-a/uv.lock" in final.summary


def test_a_lock_file_plus_source_file_is_reviewed_as_normal(monkeypatch):
    checks, reviewed = _run(monkeypatch, _MIXED_DIFF)
    final = checks[-1]
    assert ["src/app.py"] in reviewed, reviewed
    assert final.title != LOCK_TITLE
    assert "lock files only" not in final.title
    assert "Lock-file-only" not in final.summary


def test_a_vendored_only_diff_does_not_take_the_lock_file_path(monkeypatch):
    final = _run(monkeypatch, _modified("vendor/lib/x.go"))[0][-1]
    assert final.title != LOCK_TITLE
    assert final.conclusion != "success", (final.conclusion, final.title)
