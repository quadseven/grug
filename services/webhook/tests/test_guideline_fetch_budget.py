"""grug#1535: one review reads each guideline file once.

Live: every cohort, chunk and retry refetched all five candidates, and most
of those reads were 404s. Two pods spent over a third of their GitHub budget
on it, and one head took 74 rounds of five reads.
"""
from __future__ import annotations

import json
from collections import Counter
from unittest.mock import patch

import httpx
import llm_client as lc
import pytest
from llm_client import Hunk, review_diff
from repo_file_cache import RepoFileCache
from review_evidence import assemble_evidence

_REQUEST = httpx.Request("GET", "https://api.github.com/repos/o/r/contents/f")
_PR_CONTEXT = {
    "repo": "o/r", "installation_id": 1, "head_sha": "a" * 40, "base_sha": "b" * 40,
}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr("github_app_auth.get_install_token", lambda *a, **k: "t")


def _contents_api(reads: Counter, *, broken: int | None = None):
    """CLAUDE.md exists, the other candidates 404, `broken` fails them all."""

    def get(url, params=None, headers=None, timeout=None):
        path = url.split("/contents/", 1)[1]
        reads[path, params["ref"]] += 1
        if broken is not None:
            return httpx.Response(broken, request=_REQUEST)
        if path == "CLAUDE.md":
            return httpx.Response(200, text="never use bare except", request=_REQUEST)
        return httpx.Response(404, request=_REQUEST)

    return get


def _completion(_url, **_kwargs) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {
            "role": "assistant", "content": json.dumps({"findings": []}),
        }}],
        "model": "test-model-id",
    })


def test_a_multi_cohort_review_reads_each_guideline_file_once(monkeypatch):
    for loader in ("_load_poolside_key", "_load_openrouter_key", "_load_opencode_go_key"):
        monkeypatch.setattr(lc, loader, lambda: "test-key")
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda _s: None)
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.setenv("GRUG_REVIEW_COHORT_CHARS", "8000")
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)
    hunks = [
        Hunk(path=f"src/{name}.py", body="@@ -1 +1 @@\n" + "\n".join(
            f"+{name} line {i}" for i in range(500)
        ))
        for name in ("a", "b", "c")
    ]
    assert lc.review_is_staged(hunks)
    reads: Counter = Counter()
    systems: list[str] = []

    def post(url, **kwargs):
        systems.append(kwargs["json"]["messages"][0]["content"])
        return _completion(url, **kwargs)

    with patch.object(httpx, "get", side_effect=_contents_api(reads)), \
            patch.object(httpx, "post", side_effect=post):
        out = review_diff(
            assemble_evidence(hunks, pr_context=_PR_CONTEXT), installation_id=1,
        )

    assert out.coverage is not None and out.coverage.completed_cohorts == 3
    assert sum(reads.values()) == len(lc._GUIDELINE_CANDIDATES), reads
    assert set(reads.values()) == {1}, reads
    # The cached answer still reaches every cohort's prompt.
    assert len(systems) >= 3
    assert all("never use bare except" in system for system in systems)


def test_a_404_is_remembered_for_the_head():
    reads: Counter = Counter()
    with patch.object(httpx, "get", side_effect=_contents_api(reads)):
        first = lc._fetch_guideline_files(1, "o", "r", "sha1")
        second = lc._fetch_guideline_files(1, "o", "r", "sha1")
    assert first == second == {"CLAUDE.md": "never use bare except"}
    assert reads[".windsurfrules", "sha1"] == 1


@pytest.mark.parametrize("status", [403, 500, 502])
def test_a_failed_read_is_not_taken_for_an_absent_file(status):
    reads: Counter = Counter()
    with patch.object(httpx, "get", side_effect=_contents_api(reads, broken=status)):
        assert lc._fetch_guideline_files(1, "o", "r", "sha1") == {}
    with patch.object(httpx, "get", side_effect=_contents_api(reads)):
        files = lc._fetch_guideline_files(1, "o", "r", "sha1")
    assert files == {"CLAUDE.md": "never use bare except"}
    assert reads["CLAUDE.md", "sha1"] == 2


def test_a_timeout_retries_only_the_file_it_hit():
    reads: Counter = Counter()
    api = _contents_api(reads)

    def flaky(url, params=None, headers=None, timeout=None):
        if url.endswith("AGENTS.md") and not reads["AGENTS.md", params["ref"]]:
            reads["AGENTS.md", params["ref"]] += 1
            raise httpx.ReadTimeout("slow", request=_REQUEST)
        return api(url, params=params, headers=headers, timeout=timeout)

    with patch.object(httpx, "get", side_effect=flaky):
        lc._fetch_guideline_files(1, "o", "r", "sha1")
        lc._fetch_guideline_files(1, "o", "r", "sha1")
    assert reads["AGENTS.md", "sha1"] == 2
    assert reads["CLAUDE.md", "sha1"] == 1


def test_a_new_head_or_another_installation_reads_again():
    reads: Counter = Counter()
    with patch.object(httpx, "get", side_effect=_contents_api(reads)):
        lc._fetch_guideline_files(1, "o", "r", "sha1")
        lc._fetch_guideline_files(1, "o", "r", "sha2")
        lc._fetch_guideline_files(2, "o", "r", "sha2")
    assert reads["CLAUDE.md", "sha1"] == 1
    assert reads["CLAUDE.md", "sha2"] == 2


def test_a_fully_answered_head_needs_no_token(monkeypatch):
    with patch.object(httpx, "get", side_effect=_contents_api(Counter())):
        lc._fetch_guideline_files(1, "o", "r", "sha1")

    def _no_token(*_a, **_k):
        raise AssertionError("a cached head must not mint a token")

    monkeypatch.setattr("github_app_auth.get_install_token", _no_token)
    assert lc._fetch_guideline_files(1, "o", "r", "sha1") == {
        "CLAUDE.md": "never use bare except",
    }


def test_a_huge_file_is_bounded_in_memory():
    def get(url, params=None, headers=None, timeout=None):
        return httpx.Response(200, text="x" * 200_000, request=_REQUEST)

    with patch.object(httpx, "get", side_effect=get):
        files = lc._fetch_guideline_files(1, "o", "r", "sha1")
    assert {len(text) for text in files.values()} == {lc._GUIDELINE_CACHE_CHARS}


def test_the_cache_evicts_the_least_recently_used_head():
    cache = RepoFileCache(max_heads=2)
    cache.remember("a", "f", "1")
    cache.remember("b", "f", None)
    assert cache.known("a") == {"f": "1"}
    cache.remember("c", "f", "3")
    assert cache.known("b") == {}
    assert cache.known("a") == {"f": "1"}
    assert cache.known("c") == {"f": "3"}
