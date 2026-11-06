"""Characterization of every Elder PR surface (#1591).

Each test renders one publish path from fixed inputs and pins the bodies
byte-for-byte by SHA-256, the differential-render approach of #1284. The
digests were recorded on main before the markings projection moved out of
dispatch.py, so a passing run proves the move changed no surface: the
check-run title and summary, the inline review body and comments, and the
PR-timeline stack comment, on tier-1, on the deep append, and on the
ADR-0025 redelivery of a queued deep publish.

A digest mismatch prints the rendered body so the drift is readable.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from llm_client import Backend, Finding as LlmFinding, FindingOrigin, LlmReviewResponse
from personas import review_publish
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer import publish_retry
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

_HEAD = "abcd1234efgh"

_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,5 @@
 context
-old
+new1
+new2
+new3
"""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _surfaces_digest(surfaces: dict[str, str]) -> dict[str, str]:
    return {name: _digest(body) for name, body in surfaces.items()}


def _assert_pinned(surfaces: dict[str, str], pinned: dict[str, str]) -> None:
    got = _surfaces_digest(surfaces)
    drifted = {k: surfaces[k] for k in pinned if got.get(k) != pinned[k]}
    assert set(got) == set(pinned), sorted(got)
    assert not drifted, "surface drift:\n" + "\n\n".join(
        f"--- {k} ({got[k]}) ---\n{v}" for k, v in drifted.items()
    )


def _review_surfaces(result) -> dict[str, str]:
    """The review body plus each inline comment, keyed by position."""
    out = {"review_body": result.body, "review_event": result.event}
    for i, c in enumerate(result.comments):
        out[f"inline_{i}"] = f"{c.path}:{c.line}\n{c.body}"
    return out


@pytest.fixture(autouse=True)
def _fixed_environment(monkeypatch):
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "tiered")
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry", lambda inst, fn: fn("tok"),
    )
    monkeypatch.setattr(
        review_publish, "with_install_token_retry", lambda inst, fn: fn("tok"),
    )
    monkeypatch.setattr(cr_dispatch, "_record_hunt_run", lambda **kw: None)
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: None)
    monkeypatch.setattr(cr_dispatch, "get_review_comments", lambda *a, **kw: [])
    monkeypatch.setattr(
        cr_dispatch, "_precedent_notes_for",
        lambda repo, findings: {
            "src/x.py:3:silent-failure": "Precedent: 2 of 3 alike were fixed.",
        },
    )
    monkeypatch.setattr(cr_dispatch, "_muted_classes_for", lambda repo, cfg: {})


# ---------------------------------------------------------------- tier-1


def _tier1_llm() -> LlmReviewResponse:
    return LlmReviewResponse(
        kind="reviewed",
        findings=(
            LlmFinding(
                path="src/x.py", line=2, rule="null-deref", severity="high",  # type: ignore[arg-type]
                message="value may be None here", suggestion="new1 = guard(new1)",
            ),
            LlmFinding(
                path="src/x.py", line=3, rule="silent-failure", severity="medium",  # type: ignore[arg-type]
                message="catches Exception silently",
            ),
        ),
        backend_used=Backend.POOLSIDE,
        model_name="laguna",
        review_span_context={"span_id": "s1", "trace_id": "t1"},
    )


def _diff_response() -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = _DIFF
    return r


def _payload() -> dict:
    return {
        "action": "opened",
        "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {
            "number": 7,
            "head": {"sha": _HEAD},
            "base": {"sha": "base5678ijkl"},
            "title": "Preserve PR intent",
            "body": "Reviewer should understand the requested behavior.",
            "user": {"login": "alice"},
        },
    }


def _run_tier1(monkeypatch, *, blocking: bool) -> dict[str, str]:
    checks: list = []
    reviews: list = []
    stacks: list = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: _tier1_llm())
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            checks.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda token, owner, repo, *, pull_number, result: (
            reviews.append(result) or {"id": 2}
        ),
    )
    monkeypatch.setattr(
        cr_dispatch, "_upsert_review_stack_comment",
        lambda token, owner, repo, pr, body, *, create_if_absent: stacks.append(
            (body, create_if_absent),
        ),
    )
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=blocking)
    [check] = checks
    [review] = reviews
    [(stack, create)] = stacks
    return {
        "check_title": check.title,
        "check_summary": check.summary,
        "check_conclusion": str(check.conclusion),
        **_review_surfaces(review),
        "stack_body": stack,
        "stack_create_if_absent": str(create),
    }


_TIER1_ADVISORY = {
    "check_conclusion": "7e2372f4115c43bac7248772d891df3dbc830a85aa27eeed933688f61c44226c",
    "check_summary": "22b59a062418c4cadae14e5002ec69e5f2b3504262ffb4dd54e839565882f888",
    "check_title": "bad94850bce26b970a0876c55916601c869ead8d195cd0c7277ac576cc318157",
    "inline_0": "d1aa28ae7d264990e93ad5edcc8801d76409f4cb346662cb1190db05f38902d1",
    "inline_1": "0073265db3a9dccffe42e905144bf6d66ac1741ef3c644ab19be074c87a49126",
    "review_body": "ab9a7ad05178a51a6903e3323e581d1e2f1c3bf26c8590ccb854cea3309e3f78",
    "review_event": "71772a68c6d1eeb195691104b12656353e37dc9c3079cd85c555515a78aac90b",
    "stack_body": "7dfab6dbce24692ddee4c130cca5f1befc5f81e3a97dcc1d575e81a924120ef6",
    "stack_create_if_absent": "3cbc87c7681f34db4617feaa2c8801931bc5e42d8d0f560e756dd4cd92885f18",
}

_TIER1_BLOCKING = {
    "check_conclusion": "16d34b5e7bcb341ee6cb3d16495d90e93fbe57c46d3827432613210a24ebca30",
    "check_summary": "22b59a062418c4cadae14e5002ec69e5f2b3504262ffb4dd54e839565882f888",
    "check_title": "bad94850bce26b970a0876c55916601c869ead8d195cd0c7277ac576cc318157",
    "inline_0": "d1aa28ae7d264990e93ad5edcc8801d76409f4cb346662cb1190db05f38902d1",
    "inline_1": "0073265db3a9dccffe42e905144bf6d66ac1741ef3c644ab19be074c87a49126",
    "review_body": "ab9a7ad05178a51a6903e3323e581d1e2f1c3bf26c8590ccb854cea3309e3f78",
    "review_event": "fded070eb4e135b74ab7cde2e12cfb321cf73c032f0132c03403b9d41982cae5",
    "stack_body": "f80021759c1700b9ca491349b55d786ccdcc56c6caad2a46dcc18fddbbf5f798",
    "stack_create_if_absent": "3cbc87c7681f34db4617feaa2c8801931bc5e42d8d0f560e756dd4cd92885f18",
}


def test_tier1_surfaces_are_unchanged_advisory(monkeypatch):
    _assert_pinned(_run_tier1(monkeypatch, blocking=False), _TIER1_ADVISORY)


def test_tier1_surfaces_are_unchanged_blocking(monkeypatch):
    _assert_pinned(_run_tier1(monkeypatch, blocking=True), _TIER1_BLOCKING)


# -------------------------------------------------- deep append + redelivery


def _deep_finding(line: int, rule: str, severity: str, suggestion: str = "") -> Finding:
    return Finding(
        file="src/x.py", line=line, severity=severity, rule_name=rule,  # type: ignore[arg-type]
        message=f"deep finding {rule}", suggestion=suggestion,
        origins=(FindingOrigin(
            backend=Backend.POOLSIDE, model="laguna",
            review_span_context={"span_id": "s", "trace_id": "t"},
            evidence_paths=("src/x.py",), head_sha=_HEAD,
        ),),
    )


class _FakeGitHub:
    def __init__(self) -> None:
        self.posted: list = []
        self.stacks: list[tuple[str, bool]] = []
        self.fail_next: Exception | None = None

    def post_review(self, token, owner, repo, *, pull_number, result):
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        self.posted.append(result)
        return {"id": 900 + len(self.posted), "body": result.body}

    def upsert_stack(self, token, owner, repo, pr, body, *, create_if_absent):
        self.stacks.append((body, create_if_absent))


@pytest.fixture()
def github(monkeypatch) -> _FakeGitHub:
    fake = _FakeGitHub()
    monkeypatch.setattr(cr_dispatch, "post_review", fake.post_review)
    monkeypatch.setattr(cr_dispatch, "_upsert_review_stack_comment", fake.upsert_stack)
    monkeypatch.setattr(cr_dispatch, "list_reviews", lambda *a, **kw: [])
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda token, owner, repo, pr: ("snap", _HEAD, "open", False),
    )
    return fake


def _publish_deep() -> None:
    tier1 = _deep_finding(2, "null-deref", "high")
    novel = (
        _deep_finding(3, "silent-failure", "medium"),
        _deep_finding(4, "race", "critical", suggestion="new3 = lock(new3)"),
    )
    deep_eval = CodeReviewEvaluation(findings=novel, conclusion="failure")
    combined = CodeReviewEvaluation(findings=(tier1, *novel), conclusion="failure")
    cr_dispatch._publish_deep_review(
        deep_eval,
        novel_deep=novel, head_sha=_HEAD, event="REQUEST_CHANGES",
        all_prior=frozenset({"src/x.py:2:null-deref"}), installation_id=11,
        owner="myorg", repo_name="myrepo", pull_number=7,
        deep_llm=LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
            model_name="laguna",
            review_span_context={"span_id": "deep-span", "trace_id": "deep-trace"},
        ),
        author_login="alice", mode="blocking", living_range="",
        deep_suppressed_count=1, combined_eval=combined,
        pr_title="Preserve PR intent", base_sha="base5678ijkl",
        file_contents={"src/x.py": "context\nnew1\nnew2\nnew3\n"},
        linked_issues=(42,), diff_text=_DIFF,
    )


_DEEP = {
    "inline_0": "79c7054617bb5086284117f321b64bd72b78774fd15dd4ee5551b3c9769dca70",
    "inline_1": "ca3c7a1ddc9026ab62bff9344885b5ce08f9443eb57c30616b626b2b116e564a",
    "review_body": "29fe6206d384fdc5f5a587c5d8357041124c67f5a0f76ac0e1442abd94f6de24",
    "review_event": "fded070eb4e135b74ab7cde2e12cfb321cf73c032f0132c03403b9d41982cae5",
    "stack_body": "5558a492e4712811d683a1784fb2a1501da947d154445f2e4df23fac49460dfd",
    "stack_create_if_absent": "3cbc87c7681f34db4617feaa2c8801931bc5e42d8d0f560e756dd4cd92885f18",
}


def test_deep_append_surfaces_are_unchanged(github):
    _publish_deep()
    [review] = github.posted
    [(stack, create)] = github.stacks
    surfaces = {
        **_review_surfaces(review),
        "stack_body": stack,
        "stack_create_if_absent": str(create),
    }
    # The deep review carries its ADR-0025 publication key; the key hashes
    # the payload, so it is pinned along with the body.
    _assert_pinned(surfaces, _DEEP)


def test_redelivered_deep_publish_applies_the_same_bodies(github, monkeypatch):
    """A transient POST failure queues the finished publish; the poller's
    redelivery posts exactly the bodies the first attempt would have."""
    queued: list = []
    monkeypatch.setattr(cr_dispatch, "_queue_deep_publication", queued.append)
    request = httpx.Request("POST", "https://api.github.com/x")
    github.fail_next = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(503, request=request),
    )
    _publish_deep()
    assert github.posted == [] and github.stacks == []
    [pending] = queued
    # Round-trip through the store's JSON shape, as the poller reads it.
    pending = publish_retry.PendingPublication.from_record(
        json.loads(json.dumps(pending.to_record())),
    )

    assert cr_dispatch.deliver_pending_publication(pending) == "posted"
    [review] = github.posted
    [(stack, create)] = github.stacks
    surfaces = {
        **_review_surfaces(review),
        "stack_body": stack,
        "stack_create_if_absent": str(create),
    }
    _assert_pinned(surfaces, _DEEP)


# ------------------------------------------------- the projection interface


def _finished():
    from personas.code_reviewer.markings import FinishedPass

    finding = _deep_finding(2, "null-deref", "high", suggestion="new1 = guard(new1)")
    evaluation = CodeReviewEvaluation(findings=(finding,), conclusion="failure")
    return FinishedPass(
        board_evaluation=evaluation, review_evaluation=evaluation,
        conclusion="failure", event="REQUEST_CHANGES", review_phase="tier1",
        head_sha=_HEAD, base_sha="base5678ijkl", pr_title="Preserve PR intent",
        living_range="", suppressed_count=0,
        file_contents={"src/x.py": "context\nnew1\nnew2\nnew3\n"},
        diff_text=_DIFF, linked_issues=(42,), precedent_notes={}, muted={},
        prior_keys=frozenset(), held_back=frozenset(), held_note="",
        repo_config=None,
    )


def test_projection_renders_every_surface_without_github(monkeypatch):
    """Plain data in, bodies out: any network call fails the test."""
    from personas.code_reviewer import markings

    def _no_network(*a, **kw):
        raise AssertionError("the projection reached for the network")

    for verb in ("get", "post", "patch", "request"):
        monkeypatch.setattr(httpx, verb, _no_network)
    out = markings.project(_finished())
    assert out.review is not None
    assert out.review.event == "REQUEST_CHANGES"
    assert [f.rule_name for f in out.posted_findings] == ["null-deref"]
    # The markers other code reads back are intact (#1581, #791, #189).
    assert "<!-- grug:review persona=elder -->" in out.review.body
    assert "<!-- grug-rule:null-deref -->" in out.review.comments[0].body
    assert out.stack_body is not None
    assert out.stack_body.startswith(markings._STACK_MARKER)
    assert "grug-sec:elder" in out.stack_body
    assert out.stack_create_if_absent is True and out.stack_error == ""


def test_finished_pass_rejects_a_missing_input():
    """Input drift between tier-1 and deep is a construction error, not a
    quieter body (#1474, #1246)."""
    from personas.code_reviewer.markings import FinishedPass

    full = _finished()
    fields: dict[str, Any] = {f.name: getattr(full, f.name) for f in dataclasses.fields(full)}
    del fields["linked_issues"]
    with pytest.raises(TypeError):
        FinishedPass(**fields)


def test_stack_render_failure_keeps_the_review(monkeypatch):
    from personas.code_reviewer import markings

    def _boom(*a, **kw):
        raise ValueError("render broke")

    monkeypatch.setattr(markings, "_review_stack_body", _boom)
    out = markings.project(_finished())
    assert out.review is not None
    assert out.stack_body is None and out.stack_error == "ValueError"


def test_build_review_result_keeps_its_import_path():
    """Guard and Smasher import it from dispatch; it is the projection's."""
    from personas.code_reviewer import markings

    assert cr_dispatch.build_review_result is markings.build_review_result
    assert cr_dispatch.severity_chip is markings.severity_chip
