"""Pre-PR Elder review endpoint (grug#1438).

The GitHub fake sits at the transport (`httpx.Client.send`), so every
request the route makes, through any helper, is recorded. The LLM is
the only other seam: `review_diff` returns a canned reply, and the
expected findings are what `evaluate_diff` makes of that reply on the
same diff, which is what the Elder review would publish.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import HTTPException

from adapters.pg_rate_limit_store import ReservationResult
from adapters.user_store import UserIdentity
from llm_client import Finding as WireFinding
from llm_client import LlmReviewResponse
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.persona import evaluate_diff

_BASE_SHA = "a" * 40
_HEAD_SHA = "b" * 40
_INSTALL = 11

_DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,4 @@
 def handler(req):
-    return req
+    data = int(req.body)
+    return data
diff --git a/vendor/lib.py b/vendor/lib.py
index 1111111..2222222 100644
--- a/vendor/lib.py
+++ b/vendor/lib.py
@@ -1 +1,2 @@
 x = 1
+y = int(x)
"""

_GRUG_YAML = "ignore:\n  - vendor/**\n"

_REPLY = LlmReviewResponse(
    kind="reviewed",
    findings=(
        WireFinding(
            path="app.py", line=2, rule="unhandled-error", severity="high",
            message="a non-numeric body raises ValueError and answers 500",
        ),
        # A line the diff never showed: Elder drops it as a hallucination.
        WireFinding(
            path="app.py", line=40, rule="made-up", severity="low",
            message="not in the diff",
        ),
    ),
)


@dataclass
class FakeGitHub:
    """Transport-level GitHub fake. Answers the read endpoints the route
    needs and records every request, write or read."""

    repos: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def send(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        accept = request.headers.get("accept", "")
        if method != "GET":
            return httpx.Response(403, request=request)
        if path == "/installation/repositories":
            body = {
                "total_count": len(self.repos),
                "repositories": [{"full_name": r} for r in self.repos],
            }
            return httpx.Response(200, json=body, request=request)
        if path == "/repos/myorg/myrepo/commits/main":
            return httpx.Response(200, text=_BASE_SHA, request=request)
        if path == "/repos/myorg/myrepo/commits/feature/x":
            return httpx.Response(200, text=_HEAD_SHA, request=request)
        if path == f"/repos/myorg/myrepo/compare/{_BASE_SHA}...{_HEAD_SHA}":
            assert "diff" in accept
            return httpx.Response(200, text=_DIFF, request=request)
        if path == "/repos/myorg/myrepo/contents/.grug.yaml":
            assert request.url.params.get("ref") == _BASE_SHA
            return httpx.Response(200, text=_GRUG_YAML, request=request)
        if path == "/repos/myorg/myrepo/contents/app.py":
            return httpx.Response(
                200, text="def handler(req):\n    data = int(req.body)\n    return data\n",
                request=request,
            )
        return httpx.Response(404, request=request)

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]


def _user(user_id: str = "100") -> UserIdentity:
    return UserIdentity(
        github_user_id=user_id, login="alice", role="user", tier="free",
        allowlisted=True, created_at="",
        allowlisted_at=None, allowlisted_by=None,
    )


@pytest.fixture
def env(monkeypatch):
    import pre_pr_review as mod

    gh = FakeGitHub()
    monkeypatch.setattr(
        httpx.Client, "send", lambda self, request, **kw: gh.send(request),
    )
    monkeypatch.setattr(
        "github_app_auth.get_install_token", lambda *a, **k: "ghs_test",
    )
    scoped: list[dict] = []

    def _scoped(installation_id, *, repositories, permissions):
        scoped.append({
            "install": installation_id, "repositories": repositories,
            "permissions": permissions,
        })
        return "ghs_scoped"

    monkeypatch.setattr(mod, "get_scoped_install_token", _scoped)
    monkeypatch.setattr(
        mod, "list_user_installations",
        lambda uid: [{"PK": f"INST#{_INSTALL}", "account_login": "myorg"}]
        if str(uid) == "100" else [],
    )
    rows: list[dict] = []
    monkeypatch.setattr(mod, "put_check_verdict", lambda **kw: rows.append(kw))
    reserved: list[str] = []

    def _reserve(*, name, minute_limit, day_limit, now=None):
        reserved.append(name)
        return ReservationResult(admitted=True, minute_count=1, day_count=1)

    monkeypatch.setattr(mod, "try_reserve_slot", _reserve)
    reviewed: list = []

    def _review(evidence, installation_id, **kw):
        reviewed.append(evidence)
        return _REPLY

    monkeypatch.setattr(mod, "review_diff", _review)
    return {
        "mod": mod, "gh": gh, "rows": rows, "scoped": scoped,
        "reserved": reserved, "reviewed": reviewed,
        "monkeypatch": monkeypatch,
    }


def _request(mod, repo: str = "myorg/myrepo"):
    return mod.PrePrReviewRequest(repo=repo, base="main", head="feature/x")


def test_returns_the_findings_elder_would_publish_and_writes_nothing(env):
    mod, gh = env["mod"], env["gh"]

    out = mod.pre_pr_review(_request(mod), _user())

    kept_hunks = tuple(h for h in parse_diff(_DIFF) if h.file_path == "app.py")
    expected = evaluate_diff(kept_hunks, _REPLY).findings
    assert [f["file"] for f in out["findings"]] == ["app.py"]
    assert out["findings"] == [
        {
            "file": f.file, "line": f.line, "severity": f.severity,
            "class": f.rule_name, "text": f.message,
        }
        for f in expected
    ]
    assert out["head_sha"] == _HEAD_SHA
    assert out["base_sha"] == _BASE_SHA
    # The base-ref `.grug.yaml` `ignore` kept vendor/ out of the review.
    assert out["ignored_paths"] == ["vendor/lib.py"]
    assert [h.path for h in env["reviewed"][0].hunks] == ["app.py"]
    # Read-only: no write call, and the token itself can only read.
    assert gh.calls, "the fake saw no GitHub traffic"
    assert gh.writes() == []
    assert env["scoped"] == [{
        "install": 11, "repositories": ["myrepo"],
        "permissions": {"contents": "read", "metadata": "read"},
    }]
    assert env["reserved"] == ["pre_pr_review:11"]
    assert len(env["rows"]) == 1
    row = env["rows"][0]
    assert row["persona"] == "pre_pr_review"
    assert row["install_id"] == 11
    assert row["repo"] == "myorg/myrepo"
    assert row["head_sha"] == _HEAD_SHA
    assert row["findings_count"] == 1


def test_repo_outside_the_callers_installation_is_404(env):
    mod, gh = env["mod"], env["gh"]
    gh.repos = ["myorg/myrepo"]

    with pytest.raises(HTTPException) as exc:
        mod.pre_pr_review(_request(mod, repo="strangers/repo"), _user())

    assert exc.value.status_code == 404
    assert env["reserved"] == []
    assert env["reviewed"] == []
    assert env["rows"] == []
    assert gh.writes() == []


def test_user_with_no_installation_is_404(env):
    mod = env["mod"]

    with pytest.raises(HTTPException) as exc:
        mod.pre_pr_review(_request(mod), _user(user_id="999"))

    assert exc.value.status_code == 404
    assert env["reviewed"] == []


def test_past_the_per_installation_cap_is_429_and_records_nothing(env):
    mod, gh = env["mod"], env["gh"]

    def _full(*, name, minute_limit, day_limit, now=None):
        env["reserved"].append(name)
        return ReservationResult(admitted=False, minute_count=5, day_count=5)

    env["monkeypatch"].setattr(mod, "try_reserve_slot", _full)

    with pytest.raises(HTTPException) as exc:
        mod.pre_pr_review(_request(mod), _user())

    assert exc.value.status_code == 429
    assert env["reserved"] == ["pre_pr_review:11"]
    assert env["rows"] == []
    assert env["reviewed"] == []
    assert env["scoped"] == []
    assert gh.calls == []


def test_unknown_ref_is_404_without_a_review(env):
    mod = env["mod"]
    req = mod.PrePrReviewRequest(repo="myorg/myrepo", base="main", head="gone")

    with pytest.raises(HTTPException) as exc:
        mod.pre_pr_review(req, _user())

    assert exc.value.status_code == 404
    assert env["reviewed"] == []
    assert env["rows"] == []


@pytest.mark.parametrize("ref", ["a..b", "has space", "", "-x", "x" * 300])
def test_malformed_refs_are_rejected_at_the_edge(ref):
    import pre_pr_review as mod
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        mod.PrePrReviewRequest(repo="myorg/myrepo", base=ref, head="main")


def test_route_is_wired_and_requires_a_session(monkeypatch):
    monkeypatch.setenv("GRUG_CF_AUTH_FAIL_OPEN", "1")
    import main
    from fastapi.testclient import TestClient

    resp = TestClient(main.app).post(
        "/api/v1/pre-pr-review",
        json={"repo": "myorg/myrepo", "base": "main", "head": "feature/x"},
    )
    assert resp.status_code == 401
