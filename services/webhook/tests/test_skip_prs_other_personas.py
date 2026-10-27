"""`.grug.yaml` skip_prs (grug#1439) never skips Chief or Guard, and skips
the Teller walkthrough comment."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import personas.tpm.persona  # noqa: F401 - register submodule for patch path
from dispatcher import dispatch
from personas.code_reviewer import dispatch as cr_dispatch
from personas.guard import dispatch as guard_dispatch
from personas.walkthrough import dispatch as wt_dispatch

_BASE, _HEAD = "base5678ijkl", "abcd1234efgh"
_SKIP = "skip_prs:\n  authors: ['dependabot[bot]']\n"
_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,2 +1,2 @@
 context
-old
+new
"""


def _payload() -> dict:
    return {
        "action": "opened",
        "installation": {"id": 11},
        "repository": {
            "id": 22, "name": "myrepo", "owner": {"login": "myorg"},
            "full_name": "myorg/myrepo",
        },
        "pull_request": {
            "number": 7,
            "head": {"sha": _HEAD},
            "base": {"sha": _BASE, "ref": "main"},
            "title": "bump x",
            "body": "b",
            "user": {"login": "dependabot[bot]"},
        },
    }


def _get(url, **kw):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    if url.endswith("/contents/.grug.yaml"):
        assert kw["params"]["ref"] == _BASE
        r.text = _SKIP
    elif url.endswith("/files"):
        r.json = MagicMock(return_value=[])
    else:
        r.text = _DIFF
    return r


def test_dispatch_loop_still_runs_chief_and_guard_on_a_skipped_pr():
    mock_pub = MagicMock(return_value={"persona": "tpm", "result": "pass"})
    with patch("dispatcher.is_install_allowlisted", return_value=True), \
         patch("dispatcher.is_persona_enabled",
               side_effect=lambda *a: a[2] in {"tpm", "guard"}), \
         patch("dispatcher.get_repo_config", return_value={}), \
         patch("personas.tpm.persona.evaluate_pull_request") as mock_eval, \
         patch("personas.tpm.persona.publish_tpm_evaluation", mock_pub), \
         patch("async_dispatch.enqueue_guard_review", return_value=True) as mock_guard, \
         patch("httpx.get", side_effect=_get):
        mock_eval.return_value = type("R", (), {"passed": True})()
        out = dispatch("pull_request", _payload())
    ran = {p["persona"] for p in out["personas"]}
    assert {"tpm", "guard"} <= ran
    mock_eval.assert_called_once()
    mock_pub.assert_called_once()
    mock_guard.assert_called_once()


def test_guard_worker_still_publishes_on_a_skipped_pr(monkeypatch):
    checks: list = []
    monkeypatch.setattr(guard_dispatch, "with_install_token_retry", lambda i, fn: fn("tok"))
    monkeypatch.setattr(guard_dispatch, "scan_candidates", lambda *a, **kw: ())
    monkeypatch.setattr(guard_dispatch, "scan_dependencies", lambda hunks: ())
    monkeypatch.setattr(
        guard_dispatch, "post_check_run",
        lambda t, o, r, result, external_id=None: checks.append(result) or {"id": 1},
    )
    monkeypatch.setattr(
        guard_dispatch, "post_review",
        lambda t, o, r, *, pull_number, result: {"id": 2},
    )
    with patch("httpx.get", side_effect=_get):
        guard_dispatch.dispatch_guard_review(_payload(), blocking=True)
    assert [c.name for c in checks] == ["Grug - Guard"]
    assert "skip_prs" not in checks[0].summary


def test_teller_posts_no_walkthrough_on_a_skipped_pr(monkeypatch):
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda i, fn: fn("tok"))
    monkeypatch.setattr(wt_dispatch, "with_install_token_retry", lambda i, fn: fn("tok"))
    posts: list = []
    with patch("httpx.get", side_effect=_get), \
         patch("httpx.post", side_effect=lambda *a, **kw: posts.append(a)), \
         patch("httpx.patch", side_effect=lambda *a, **kw: posts.append(a)):
        out = wt_dispatch.dispatch_walkthrough_review(_payload(), blocking=False)
    assert out["result"] == "skipped_config"
    assert out["skipped_by"] == "author `dependabot[bot]`"
    assert posts == []
