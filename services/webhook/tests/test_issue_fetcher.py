"""Tests for personas.tpm.issue_fetcher - the ONE builder behind Chief's
linked-issue fetch (#782).

Both the pull_request webhook path and `/grug recheck` must import this
builder rather than carry their own fetcher: the recheck bug was exactly a
second call site that never got one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from personas.tpm.issue_fetcher import build_issue_details_fetcher, build_issue_fetcher

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_fetcher_reads_issue_body_with_install_token():
    fetcher = build_issue_fetcher(installation_id=7, owner="my org", repo="my.repo")
    seen: dict[str, object] = {}

    def _get(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers")

        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"body": "## Acceptance\n- [ ] open\n"}
        return _R()

    with patch("github_app_auth.with_install_token_retry",
               side_effect=lambda install_id, fn: fn(f"tok-{install_id}")), \
         patch("httpx.get", side_effect=_get):
        body = fetcher(42)

    assert body == "## Acceptance\n- [ ] open\n"
    # Owner/repo are URL-encoded path components; the token is the install token.
    assert seen["url"] == "https://api.github.com/repos/my%20org/my.repo/issues/42"
    assert seen["headers"]["Authorization"] == "token tok-7"


def test_fetcher_null_body_reads_as_empty():
    fetcher = build_issue_fetcher(installation_id=1, owner="o", repo="r")

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"body": None}

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("t")), \
         patch("httpx.get", return_value=_R()):
        assert fetcher(1) == ""


def test_details_fetcher_reads_title_and_body():
    fetcher = build_issue_details_fetcher(installation_id=1, owner="o", repo="r")

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"title": "Health: low income", "body": "quoted evidence"}

    with patch(
        "github_app_auth.with_install_token_retry",
        side_effect=lambda _i, fn: fn("t"),
    ), patch("httpx.get", return_value=_R()):
        assert fetcher(1) == {"title": "Health: low income", "body": "quoted evidence"}


def test_fetcher_raises_on_http_error_so_check_can_fail_open(mock_transport_client):
    """The builder does NOT swallow errors: `check_linked_issue_completeness`
    owns the fail-open decision (and marks the check skipped). A fetcher
    that returned "" on 404 would read as "no boxes" - an earned pass."""
    fetcher = build_issue_fetcher(installation_id=1, owner="o", repo="r")
    client = mock_transport_client(status_codes=[404], json_bodies=[{"message": "Not Found"}])

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("t")), \
         patch("httpx.get", side_effect=lambda *a, **kw: client.get(*a, **kw)):
        with pytest.raises(httpx.HTTPStatusError):
            fetcher(404)


def _calls_build_issue_fetcher(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name == "build_issue_fetcher":
                return True
    return False


@pytest.mark.parametrize("rel", [
    "services/_shared/personas/tpm/webhook_dispatch.py",
    "services/webhook/dispatcher.py",
])
def test_both_evaluation_paths_use_the_shared_builder(rel):
    """Structural guard: a path that stops calling the builder is a path
    that can drift back to a bare `evaluate_pull_request(body)` - the
    #782 defect - or to a private copy of the fetcher."""
    assert _calls_build_issue_fetcher(_REPO_ROOT / rel), rel
