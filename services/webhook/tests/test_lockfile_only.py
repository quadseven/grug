"""grug#1649: a lock-file-only pull request concludes success, not neutral.

Lock files are dropped from Elder's plate (#609), so a diff made only of
them reached the model with nothing and published a neutral "eyes clouded
(no_diff)" that never clears a blocking gate. A lock-file-only diff now
concludes success only when (a) every file name is on the explicit list,
(b) the head is in the base repository and its author is trusted, and
(c) every URL host on an added lock line is an allowed registry host.
Anything else stays neutral, and a diff that mixes a lock file with a
reviewable file is reviewed as before.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from llm_client import Backend, LlmReviewResponse
from personas import review_publish
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.lock_files import (
    LOCK_FILES_ONLY_TITLE as LOCK_TITLE,
    is_lock_file_path,
    judge_lock_only,
    line_hosts,
)

_REPO = {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}}
_FORK = {"id": 99, "name": "myrepo", "owner": {"login": "stranger"}}


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


def _binary(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 39dc28b90..1a2b3c4d5 100644\n"
        f"Binary files a/{path} and b/{path} differ\n"
    )


_PYPI = 'sdist = { url = "https://files.pythonhosted.org/packages/ab/cd/x-1.0.tar.gz" }'
_NPM = '"resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",'
_EVIL = 'sdist = { url = "https://evil.example.net/packages/x-1.0.tar.gz" }'

_LOCK_ONLY_DIFF = (
    _modified("pulumi/stack-a/uv.lock", new=_PYPI)
    + _modified("pulumi/stack-b/uv.lock")
    + _modified("web/package-lock.json", new=_NPM)
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


def _payload(*, head_repo: dict, user: dict, association: str) -> dict:
    return {
        "action": "opened",
        "installation": {"id": 11},
        "repository": _REPO,
        "pull_request": {
            "number": 7,
            "head": {"sha": "abcd1234efgh", "repo": head_repo},
            "base": {"sha": "base5678ijkl"},
            "title": "Bump a dependency in the locks",
            "body": "Refreshes the lock files.",
            "user": user,
            "author_association": association,
        },
    }


_MEMBER = {"login": "alice", "type": "User"}


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


def _run(
    monkeypatch, diff: str, *, head_repo: dict = _REPO, user: dict = _MEMBER,
    association: str = "MEMBER",
) -> tuple[list, list]:
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
    payload = _payload(head_repo=head_repo, user=user, association=association)
    with patch("httpx.get", side_effect=_fake_get):
        cr_dispatch.dispatch_code_review(payload, blocking=True)
    return posted_check, reviewed_paths


# (a) the explicit file-name list


@pytest.mark.parametrize("path", [
    "uv.lock", "pulumi/stack/uv.lock", "poetry.lock", "Pipfile.lock",
    "Cargo.lock", "web/package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "go.sum", "Gemfile.lock", "infra/.terraform.lock.hcl", "composer.lock",
])
def test_listed_lock_file_names_are_recognised(path):
    assert is_lock_file_path(path)


@pytest.mark.parametrize("path", [
    "src/app.py", "data/rows.jsonl", "vendor/lib/x.go", "dist/app.js",
    "docs/lockfile.md", "src/lock.py", "flake.lock", "mix.lock",
    "evil.lock", "uv.lock.bak",
])
def test_unlisted_names_are_not_lock_files(path):
    assert not is_lock_file_path(path)


def test_an_unlisted_dot_lock_file_stays_neutral(monkeypatch):
    final = _run(monkeypatch, _modified("tools/flake.lock"))[0][-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)
    assert final.title != LOCK_TITLE


# The success path and the mixed path


def test_a_trusted_lock_file_only_diff_concludes_success(monkeypatch):
    checks, _ = _run(monkeypatch, _LOCK_ONLY_DIFF)
    final = checks[-1]
    assert final.conclusion == "success", (final.conclusion, final.title)
    assert final.title == LOCK_TITLE
    assert "pulumi/stack-a/uv.lock" in final.summary


def test_a_lock_file_plus_source_file_is_reviewed_as_normal(monkeypatch):
    checks, reviewed = _run(monkeypatch, _MIXED_DIFF)
    final = checks[-1]
    assert ["src/app.py"] in reviewed, reviewed
    assert final.title != LOCK_TITLE
    assert "Lock-file-only" not in final.summary


def test_a_vendored_only_diff_does_not_take_the_lock_file_path(monkeypatch):
    final = _run(monkeypatch, _modified("vendor/lib/x.go"))[0][-1]
    assert final.title != LOCK_TITLE
    assert final.conclusion != "success", (final.conclusion, final.title)


# (b) same-repo head and a trusted author


def test_a_fork_head_stays_neutral(monkeypatch):
    final = _run(monkeypatch, _LOCK_ONLY_DIFF, head_repo=_FORK)[0][-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)
    assert final.title != LOCK_TITLE
    assert "not a trusted" in final.summary


def test_a_deleted_fork_head_stays_neutral(monkeypatch):
    final = _run(monkeypatch, _LOCK_ONLY_DIFF, head_repo={})[0][-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)


@pytest.mark.parametrize("association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"])
def test_an_untrusted_author_stays_neutral(monkeypatch, association):
    final = _run(monkeypatch, _LOCK_ONLY_DIFF, association=association)[0][-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)
    assert final.title != LOCK_TITLE


def test_an_unlisted_app_stays_neutral(monkeypatch):
    monkeypatch.delenv("GRUG_APP_AUTHOR_FAMILIES", raising=False)
    bot = {"login": "some-app[bot]", "type": "Bot"}
    final = _run(monkeypatch, _LOCK_ONLY_DIFF, user=bot, association="NONE")[0][-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)


def test_a_known_app_passes(monkeypatch):
    monkeypatch.setenv("GRUG_APP_AUTHOR_FAMILIES", "lane-app[bot]:claude")
    bot = {"login": "lane-app[bot]", "type": "Bot"}
    final = _run(monkeypatch, _LOCK_ONLY_DIFF, user=bot, association="NONE")[0][-1]
    assert final.conclusion == "success", (final.conclusion, final.title)
    assert final.title == LOCK_TITLE


# (c) the registry host allowlist and unparseable files


def test_an_unlisted_registry_host_stays_neutral_and_is_named(monkeypatch):
    final = _run(monkeypatch, _modified("uv.lock", new=_EVIL))[0][-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)
    assert final.title != LOCK_TITLE
    assert "evil.example.net" in final.summary


def test_a_lookalike_host_is_not_allowed(monkeypatch):
    line = '"resolved": "https://registry.npmjs.org.evil.example/x.tgz",'
    final = _run(monkeypatch, _modified("package-lock.json", new=line))[0][-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)
    assert "registry.npmjs.org.evil.example" in final.summary


def test_a_binary_lock_file_is_unparseable_and_stays_neutral(monkeypatch):
    final = _run(monkeypatch, _binary("uv.lock"))[0][-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)
    assert "could not scan" in final.summary


def test_a_removed_line_naming_a_bad_host_does_not_count():
    diff = _modified("uv.lock", old=_EVIL, new=_PYPI)
    verdict = judge_lock_only(file_changes(diff), parse_diff(diff), trusted=True)
    assert verdict.applies and verdict.passed, verdict.note


def test_untrusted_never_passes_even_with_clean_hosts():
    diff = _modified("uv.lock", new=_PYPI)
    verdict = judge_lock_only(file_changes(diff), parse_diff(diff), trusted=False)
    assert verdict.applies and not verdict.passed


@pytest.mark.parametrize(("line", "hosts"), [
    (_PYPI, ("files.pythonhosted.org",)),
    ('source = { registry = "https://pypi.org/simple" }', ("pypi.org",)),
    ('source = "registry+https://github.com/rust-lang/crates.io-index"', ("github.com",)),
    ('"resolved": "git+ssh://git@evil.example.org/x.git#abc",', ("evil.example.org",)),
    ('"resolved": "git@evil.example.org:x/y.git",', ("evil.example.org",)),
    ("https://PyPI.org/simple", ("pypi.org",)),
    ("https://user:pw@evil.example.org/x", ("evil.example.org",)),
    ("github.com/pkg/errors v0.9.1 h1:abc=", ()),
    ('"lodash@4.17.21":', ()),
])
def test_line_hosts(line, hosts):
    assert line_hosts(line) == hosts
