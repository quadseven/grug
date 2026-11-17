"""Linked issue summary note survives the Elder deep-append path."""

from unittest.mock import patch

import httpx
from llm_client import Backend, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.repo_config import RepoConfig

_DIFF = """diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,1 +1,2 @@
-old
+new
"""


def _run_deep_append(monkeypatch, deep):
    checks = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **k: LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.CAVE,
        model_name="coder", backends_used=(Backend.CAVE,), models_used=("coder",),
    ))
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry", lambda _id, fn, **kw: fn("token"),
    )
    monkeypatch.setattr(cr_dispatch, "fetch_pr_diff", lambda *a, **k: _DIFF)
    monkeypatch.setattr(cr_dispatch, "fetch_file_contents", lambda *a, **k: {})
    monkeypatch.setattr(cr_dispatch, "review_reasoner_diff", lambda *a, **k: deep)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda _token, _owner, _repo, result, external_id=None: (
            checks.append({"external_id": external_id, "summary": result.summary})
            or {"id": len(checks)}
        ),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **k: ())
    monkeypatch.setattr(cr_dispatch, "_load_pr_repo_config", lambda *a, **k: RepoConfig())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "title", "body"),
    )
    monkeypatch.setattr(cr_dispatch, "list_check_runs_for_ref", lambda *a, **k: [])
    monkeypatch.setattr(cr_dispatch, "list_reviews", lambda *a, **k: [])
    response = httpx.Response(200, text=_DIFF)
    with (
        patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None),
        patch("adapters.install_store.get_elder_last_reviewed", return_value=None),
        patch("httpx.get", return_value=response),
    ):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)
    return [c for c in checks if (c["external_id"] or "").startswith("grug-cr-deep:")]


def _payload():
    return {
        "action": "opened", "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {
            "number": 7, "head": {"sha": "abcd1234efgh"},
            "base": {"sha": "base5678ijkl"}, "title": "auth change",
            "body": "Refs #42", "user": {"login": "contributor"},
        },
    }


def test_deep_append_summary_includes_linked_issue_criteria(monkeypatch):
    linked_context = (
        "### LINKED ISSUE ACCEPTANCE CRITERIA\n"
        "#42 criterion 1 [open]: First criterion\n"
        "#42 criterion 2 [open]: Second criterion"
    )

    def attach_context(context, **_kwargs):
        context["linked_issue_context"] = linked_context

    monkeypatch.setattr(
        cr_dispatch, "_attach_linked_issue_context", attach_context,
    )
    deep = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.CAVE_REASONER,
        model_name="reasoner", backends_used=(Backend.CAVE_REASONER,),
        models_used=("reasoner",),
    )

    posted = _run_deep_append(monkeypatch, deep)

    summary = posted[-1]["summary"]
    assert "Linked issue criteria read: #42 (2 criteria)." in summary
