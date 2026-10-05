"""dispatch_code_review honors `.grug.yaml` read from the PR BASE ref."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch

_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
diff --git a/extras/y.py b/extras/y.py
--- a/extras/y.py
+++ b/extras/y.py
@@ -1,3 +1,4 @@
 context
-old
+v1
+v2
"""
_BASE, _HEAD = "base5678ijkl", "abcd1234efgh"


def _payload() -> dict:
    return {
        "action": "opened",
        "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {
            "number": 7,
            "head": {"sha": _HEAD},
            "base": {"sha": _BASE, "ref": "main"},
            "title": "t",
            "body": "b",
            "user": {"login": "alice"},
        },
    }


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry", lambda i, fn: fn("fake-token"),
    )


class _Run:
    def __init__(self, configs: dict[str, str], findings=()):
        self.configs = configs  # ref -> .grug.yaml text
        self.config_refs: list[str] = []
        self.seen_paths: list[str] = []
        self.docs_context: str | None = None
        self.checks: list = []
        self.reviews: list = []
        self.findings = findings

    def get(self, url, **kw):
        r = MagicMock(spec=httpx.Response)
        if url.endswith("/contents/.grug.yaml"):
            ref = kw["params"]["ref"]
            self.config_refs.append(ref)
            if ref in self.configs:
                r.status_code = 200
                r.raise_for_status = MagicMock()
                r.text = self.configs[ref]
                return r
            r.status_code = 404
            r.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
                "404", request=MagicMock(), response=r))
            return r
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.text = _DIFF
        return r

    def review_diff(self, hunks, installation_id, pr_context=None,
                    file_contents=None, cross_file_contents=None,
                    runtime_context=None, ci_context=None,
                    repo_docs_context=None, voice="caveman", cancel_event=None):
        self.seen_paths = [h.path for h in hunks]
        self.docs_context = repo_docs_context
        return LlmReviewResponse(
            kind="reviewed", findings=tuple(self.findings),
            backend_used=Backend.POOLSIDE, model_name="m",
        )

    def go(self, monkeypatch):
        monkeypatch.setattr(cr_dispatch, "review_diff", self.review_diff)
        monkeypatch.setattr(
            cr_dispatch, "post_check_run",
            lambda tok, o, r, result, external_id=None: self.checks.append(result) or {"id": 1},
        )
        monkeypatch.setattr(
            cr_dispatch, "post_review",
            lambda tok, o, r, *, pull_number, result: self.reviews.append(result) or {"id": 2},
        )
        with patch("httpx.get", side_effect=self.get):
            cr_dispatch.dispatch_code_review(_payload(), blocking=False)
        return self


def _f(path, line, sev, rule, msg):
    return LlmFinding(path=path, line=line, rule=rule, severity=sev, message=msg)  # type: ignore[arg-type]


def test_missing_file_changes_nothing(monkeypatch):
    run = _Run({}).go(monkeypatch)
    assert run.seen_paths == ["src/x.py", "extras/y.py"]
    assert ".grug.yaml" not in run.checks[0].summary


def test_ignored_paths_never_reach_the_model(monkeypatch):
    run = _Run({_BASE: "ignore: ['extras/**']\n"}).go(monkeypatch)
    assert run.seen_paths == ["src/x.py"]


def test_config_is_read_from_base_never_head(monkeypatch):
    # A PR that adds an ignore-everything file on its own head must not
    # loosen its own review.
    run = _Run({_HEAD: "ignore: ['**']\n"}).go(monkeypatch)
    assert run.config_refs == [_BASE]
    assert run.seen_paths == ["src/x.py", "extras/y.py"]


def test_path_instructions_reach_prompt_for_matching_files_only(monkeypatch):
    cfg = (
        "path_instructions:\n"
        "  - path: 'src/**'\n    instructions: SRC-RULE-ALPHA\n"
        "  - path: 'k8s/**'\n    instructions: K8S-RULE-BETA\n"
    )
    run = _Run({_BASE: cfg}).go(monkeypatch)
    assert run.docs_context is not None
    assert "SRC-RULE-ALPHA" in run.docs_context
    assert "K8S-RULE-BETA" not in run.docs_context


def test_min_inline_severity_keeps_low_findings_in_summary_only(monkeypatch):
    findings = (
        _f("src/x.py", 2, "low", "style-nit", "LOWMSG-nit"),
        _f("src/x.py", 3, "high", "silent-failure", "HIGHMSG-bug"),
    )
    run = _Run({_BASE: "min_inline_severity: high\n"}, findings).go(monkeypatch)
    inline = run.reviews[0].comments
    assert [c.line for c in inline] == [3]
    assert "LOWMSG-nit" in run.checks[0].summary
    assert "HIGHMSG-bug" in run.checks[0].summary


def test_no_floor_posts_everything_inline(monkeypatch):
    findings = (
        _f("src/x.py", 2, "low", "style-nit", "LOWMSG-nit"),
        _f("src/x.py", 3, "high", "silent-failure", "HIGHMSG-bug"),
    )
    run = _Run({}, findings).go(monkeypatch)
    assert sorted(c.line for c in run.reviews[0].comments) == [2, 3]


def test_malformed_file_one_note_and_review_still_runs(monkeypatch):
    findings = (_f("src/x.py", 3, "high", "silent-failure", "HIGHMSG-bug"),)
    run = _Run({_BASE: "ignore: [unclosed\n  : :\n"}, findings).go(monkeypatch)
    assert run.seen_paths == ["src/x.py", "extras/y.py"]
    assert run.checks[0].summary.count(".grug.yaml") == 1
    assert len(run.reviews[0].comments) == 1


def test_unknown_key_noted_and_known_keys_apply(monkeypatch):
    run = _Run({_BASE: "ignore: ['extras/**']\nbogus: 1\n"}).go(monkeypatch)
    assert run.seen_paths == ["src/x.py"]
    assert run.checks[0].summary.count(".grug.yaml problem") == 1
    assert "bogus" in run.checks[0].summary
