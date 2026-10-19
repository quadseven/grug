"""grug#1269: Elder's Cave calls name their PR and say whether it is public.

The spark-gateway shows which PR a call serves from `X-Spark-Job`
(`owner/repo#N`, nothing else) and routes only `X-Spark-Data-Class: public`
work to its free remote providers. An unknown repo is private.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import httpx
import pytest

import llm_client as lc
import rerun
from llm_client import Backend, Hunk, review_diff

_CAVE_ARMS = ("judge", Backend.CAVE, Backend.CAVE_REASONER)
_PUBLIC = {"repo": "quadseven/grug", "pr_number": 7, "repo_private": False}


@pytest.fixture(autouse=True)
def _cave(monkeypatch):
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    # The prompt's repo blocks read the store; none of them matter here.
    for name in (
        "_repo_learnings_block", "_repo_guidelines_block",
        "_team_practices_block", "_few_shot_block",
    ):
        monkeypatch.setattr(lc, name, lambda *_a, **_kw: "")


def _cave_config(arm):
    return lc._cave_judge_config() if arm == "judge" else lc._cave_review_config(arm)


def _headers(config, pr_context):
    with lc._spark_job_scope(pr_context):
        return lc._merged_headers(config, "k")


def _reply(body: str) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": body}}]},
    )


@pytest.mark.parametrize("arm", _CAVE_ARMS)
@pytest.mark.parametrize(
    ("visibility", "data_class"),
    [({"repo_private": False}, "public"), ({"repo_private": True}, "private"), ({}, "private")],
    ids=["public", "private", "unknown"],
)
def test_cave_arm_names_the_job_and_its_data_class(arm, visibility, data_class) -> None:
    headers = _headers(
        _cave_config(arm), {"repo": "quadseven/grug", "pr_number": 7, **visibility},
    )
    assert headers["X-Spark-Job"] == "quadseven/grug#7"
    assert headers["X-Spark-Data-Class"] == data_class


@pytest.mark.parametrize(
    "pr_context",
    [
        {"repo": "quadseven/grug", "repo_private": False},
        {"pr_number": 7, "repo_private": False},
        {"repo": "grug", "pr_number": 7},
        {"repo": "quadseven/grug", "pr_number": True},
        {"repo": "quadseven/grug", "pr_number": 0},
        {"repo": "quadseven/grug evil", "pr_number": 7},
        None,
    ],
    ids=["no-pr-number", "no-repo", "no-owner", "bool-number", "zero", "bad-repo", "none"],
)
def test_job_header_is_absent_unless_it_is_owner_repo_number(pr_context) -> None:
    headers = _headers(_cave_config(Backend.CAVE), pr_context)
    assert "X-Spark-Job" not in headers
    assert "X-Spark-Data-Class" in headers


def test_cave_call_outside_a_review_is_private_and_unnamed() -> None:
    headers = lc._merged_headers(_cave_config(Backend.CAVE), "k")
    assert "X-Spark-Job" not in headers
    assert headers["X-Spark-Data-Class"] == "private"


def test_job_headers_go_to_cave_arms_only() -> None:
    saas = [
        config for backend, config in lc._BACKEND_CONFIGS.items()
        if backend not in (Backend.CAVE, Backend.CAVE_REASONER)
    ]
    saas += [
        lc._saas_overload_fallback_config(backend)
        for backend in (Backend.POOLSIDE, Backend.OPENROUTER)
    ]
    assert saas
    for config in saas:
        headers = _headers(config, _PUBLIC)
        assert "X-Spark-Job" not in headers, config.backend
        assert "X-Spark-Data-Class" not in headers, config.backend


def test_the_job_reaches_both_arm_threads(monkeypatch) -> None:
    """Deep review runs the arms on pool threads; the job must ride the same
    context copy as the deadline, or both arms would send `private`."""
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "deep")
    seen: list[tuple[str | None, str | None, threading.Thread]] = []

    def capture(_url, *, headers, **_kwargs) -> httpx.Response:
        seen.append((
            headers.get("X-Spark-Job"), headers.get("X-Spark-Data-Class"),
            threading.current_thread(),
        ))
        return _reply('{"findings": []}')

    with patch.object(httpx, "post", side_effect=capture):
        review_diff(
            [Hunk(path="src/x.py", body="@@ -1 +1 @@\n-a\n+b")],
            installation_id=1, pr_context=_PUBLIC,
        )

    assert len(seen) == 2
    assert {(job, data_class) for job, data_class, _ in seen} == {
        ("quadseven/grug#7", "public"),
    }
    assert all(thread is not threading.main_thread() for _, _, thread in seen)
    assert lc._SPARK_JOB.get() is None, "the review must not leak its job"


def test_the_cave_judge_names_the_job() -> None:
    sent: list[dict] = []

    def capture(_url, *, headers, **_kwargs) -> httpx.Response:
        sent.append(headers)
        return _reply('{"verdicts": []}')

    finding = {"rule_name": "r", "file": "src/x.py", "line": 1,
               "severity": "low", "message": "m"}
    with patch.object(httpx, "post", side_effect=capture):
        lc.judge_findings(
            [finding], [Hunk(path="src/x.py", body="@@ -1 +1 @@\n-a\n+b")],
            installation_id=1, pr_context=_PUBLIC, config=lc._cave_judge_config(),
        )

    assert sent and sent[0]["X-Spark-Job"] == "quadseven/grug#7"
    assert sent[0]["X-Spark-Data-Class"] == "public"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"repository": {"private": False}}, False),
        ({"repository": {"private": True}}, True),
        ({"repository": {}, "pull_request": {"base": {"repo": {"private": False}}}}, False),
        ({"repository": {"private": None}}, None),
        ({"repository": {"private": "false"}}, None),
        ({"repository": {}, "pull_request": {}}, None),
    ],
    ids=["public", "private", "base-repo", "null", "string", "absent"],
)
def test_elder_reads_repo_visibility_from_the_payload(payload, expected) -> None:
    from personas.code_reviewer.dispatch import _repo_private

    assert _repo_private(payload) is expected


def test_rerun_payload_carries_repo_visibility() -> None:
    """The rerun and `/grug improve` paths rebuild the payload from the
    fetched PR; its base repo says whether the repo is private."""
    from personas.code_reviewer.dispatch import _repo_private

    pr = {"head": {"sha": "h"}, "base": {"sha": "b", "repo": {"id": 1, "private": False}}}
    payload = rerun._review_payload(
        install_id=1, owner="quadseven", repo_name="grug", pr_number=7, pr=pr,
    )
    assert _repo_private(payload) is False
