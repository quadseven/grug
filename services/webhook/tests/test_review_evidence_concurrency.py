"""Concurrency tests for Elder review evidence gathering."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from personas import review_publish
from personas.code_reviewer import dispatch, repo_docs
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.repo_config import RepoConfig


def _ok_response(url: str, text: str) -> httpx.Response:
    return httpx.Response(200, text=text, request=httpx.Request("GET", url))


def _status_response(url: str, status: int) -> httpx.Response:
    return httpx.Response(status, text="err", request=httpx.Request("GET", url))


def test_fetch_file_contents_fetches_concurrently_and_preserves_order():
    barrier = threading.Barrier(3, timeout=2.0)

    def fake_get(url, *, params=None, headers=None, timeout=None):
        if url.endswith("/missing.py"):
            return _status_response(url, 404)
        barrier.wait()
        name = url.rsplit("/", 1)[-1]
        return _ok_response(url, f"content:{name}")

    paths = ("a.py", "missing.py", "b.py", "c.py")
    with patch.object(review_publish.httpx, "get", side_effect=fake_get):
        out = review_publish.fetch_file_contents("tok", "acme", "widgets", paths, "deadbeef")

    assert list(out.keys()) == ["a.py", "b.py", "c.py"]
    assert out == {"a.py": "content:a.py", "b.py": "content:b.py", "c.py": "content:c.py"}


def test_fetch_file_contents_only_404_skips_raises_non_404_concurrently():
    def fake_get(url, *, params=None, headers=None, timeout=None):
        if url.endswith("/missing.py"):
            return _status_response(url, 404)
        if url.endswith("/broken.py"):
            return _status_response(url, 500)
        return _ok_response(url, "ok")

    with patch.object(review_publish.httpx, "get", side_effect=fake_get):
        with pytest.raises(httpx.HTTPStatusError):
            review_publish.fetch_file_contents(
                "tok", "acme", "widgets", ("ok.py", "missing.py", "broken.py"), "deadbeef",
                only_404_skips=True,
            )


def test_assemble_review_evidence_runs_stages_concurrently(monkeypatch):
    barrier = threading.Barrier(5, timeout=2.0)
    seen_cross_file_inputs: list[dict[str, str]] = []

    def fake_with_token(_, fn):
        return fn("tok")

    def fake_head_files(token, owner, repo, paths, ref):
        barrier.wait()
        return {"app.py": "def run():\n    return 1\n"}

    def fake_base_files(installation_id, owner, repo_name, pull_number, paths, base_ref):
        barrier.wait()
        return {"app.py": "def run():\n    return 0\n"}

    def fake_cross_file(run, hunks, file_contents, changed_paths):
        seen_cross_file_inputs.append(dict(file_contents))
        return {"caller.py": "run()\n"}

    def fake_runtime(owner, repo_name, hunks):
        barrier.wait()
        return "runtime-ok"

    def fake_ci(*, fetch_check_runs, head_sha, hunks):
        barrier.wait()
        fetch_check_runs()
        return "ci-ok"

    def fake_docs(run, diff, changed_paths):
        barrier.wait()
        run.pr_context["linked_issue_context"] = "issue-ok"
        return "docs-ok"

    monkeypatch.setattr(dispatch, "with_install_token_retry", fake_with_token)
    monkeypatch.setattr(dispatch, "fetch_file_contents", fake_head_files)
    monkeypatch.setattr(dispatch, "_fetch_base_contents", fake_base_files)
    monkeypatch.setattr(dispatch, "_review_cross_file_context", fake_cross_file)
    monkeypatch.setattr(dispatch, "build_runtime_context", fake_runtime)
    monkeypatch.setattr(dispatch, "list_check_runs_for_ref", lambda *a, **kw: [{"name": "ci"}])
    monkeypatch.setattr(dispatch, "build_ci_status_context", fake_ci)
    monkeypatch.setattr(dispatch, "_review_docs_and_intent", fake_docs)

    hunk = DiffHunk(
        file_path="app.py",
        new_start=1,
        new_lines=frozenset({1, 2}),
        body="@@ -1,1 +1,2 @@\n-def run(): return 0\n+def run():\n+    return 1\n",
    )
    run = SimpleNamespace(
        installation_id=1,
        owner="quadseven",
        repo_name="grug",
        pull_number=42,
        head_sha="head123",
        pr_name="quadseven/grug#42",
        pr_context={"base_sha": "base123", "pr_base_sha": "base123", "body": ""},
    )
    diff = SimpleNamespace(
        text="diff --git a/app.py b/app.py\n",
        hunks=(hunk,),
        repo_config=RepoConfig(),
    )

    result = dispatch._assemble_review_evidence(run, diff)

    assert result.file_contents == {"app.py": "def run():\n    return 1\n"}
    assert result.base_file_contents == {"app.py": "def run():\n    return 0\n"}
    assert result.head_check_runs == [{"name": "ci"}]
    assert seen_cross_file_inputs == [{"app.py": "def run():\n    return 1\n"}]
    assert run.pr_context["linked_issue_context"] == "issue-ok"


def test_build_repo_docs_context_fetches_context_and_adrs_concurrently():
    initial_barrier = threading.Barrier(2, timeout=2.0)
    adr_barrier = threading.Barrier(3, timeout=2.0)

    def fetch_file(path: str) -> str | None:
        if path == "CONTEXT.md":
            initial_barrier.wait()
            return "# CONTEXT\nGlossary text."
        adr_barrier.wait()
        return f"Body for {path}"

    def list_dir(path: str) -> list[str] | None:
        initial_barrier.wait()
        return [
            "0001-dispatch-one.md",
            "0002-dispatch-two.md",
            "0003-dispatch-three.md",
        ]

    ctx = repo_docs.build_repo_docs_context(
        fetch_file=fetch_file,
        list_dir=list_dir,
        hunks=[SimpleNamespace(file_path="services/dispatch.py", body="diff")],
    )

    assert ctx is not None
    assert "Glossary text." in ctx
    pos1 = ctx.index("0001-dispatch-one.md")
    pos2 = ctx.index("0002-dispatch-two.md")
    pos3 = ctx.index("0003-dispatch-three.md")
    assert pos1 < pos2 < pos3
