"""Tests for the re-run consumer (#305, ADR-0004).

External behavior: a job (keyed by repo "owner/name") re-fetches the PR's
current head and dispatches the named persona; an unsupported persona is
skipped (not retried); an infra failure RAISES so the ESM retries → DLQ.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

import rerun


def _job(**over) -> str:
    d = {"schema_version": 1, "install_id": 11, "repo": "myorg/myrepo", "pr_number": 7, "persona": "elder"}
    d.update(over)
    return json.dumps(d)


def _event(*bodies) -> dict:
    return {"Records": [{"eventSource": "aws:sqs", "body": b} for b in bodies]}


def _pr_response(head_sha="abc123", repo_id=222):
    pr = MagicMock(spec=httpx.Response)
    pr.raise_for_status = MagicMock()
    pr.json = MagicMock(return_value={
        "number": 7,
        "head": {"sha": head_sha},
        "base": {"repo": {"id": repo_id}},
    })
    return pr


def test_rerun_dispatches_persona_on_current_head():
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response(head_sha="deadbeef", repo_id=222)), \
         patch.object(rerun, "get_repo_config", return_value={"code_reviewer_blocking": False}), \
         patch.object(rerun, "dispatch_code_review") as disp:
        out = rerun.handle_rerun_jobs(_event(_job()))
    assert out == {"records": 1, "dispatched": 1, "skipped": 0}
    payload, kwargs = disp.call_args.args[0], disp.call_args.kwargs
    # Re-run targets the PR's CURRENT head (fetched), not the errored row's.
    assert payload["pull_request"]["head"]["sha"] == "deadbeef"
    assert payload["repository"]["owner"]["login"] == "myorg"  # from the repo string
    assert payload["repository"]["name"] == "myrepo"
    assert payload["repository"]["id"] == 222  # from pr.base.repo.id
    assert payload["installation"]["id"] == 11
    assert kwargs["blocking"] is False


def test_rerun_blocking_flag_from_repo_config():
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response()), \
         patch.object(rerun, "get_repo_config", return_value={"code_reviewer_blocking": True}) as cfg, \
         patch.object(rerun, "dispatch_code_review") as disp:
        rerun.handle_rerun_jobs(_event(_job()))
    cfg.assert_called_once_with(11, 222)  # repo_id derived from the PR fetch
    assert disp.call_args.kwargs["blocking"] is True


def test_rerun_skips_unsupported_persona_without_dispatch():
    with patch.object(rerun, "dispatch_code_review") as disp, \
         patch("httpx.get") as get:
        out = rerun.handle_rerun_jobs(_event(_job(persona="chief")))
    assert out == {"records": 1, "dispatched": 0, "skipped": 1}
    disp.assert_not_called()
    get.assert_not_called()  # never even fetches for a persona we don't drive


def test_rerun_raises_on_github_fetch_failure_so_esm_retries():
    # An infra failure must propagate → ESM retry (visibility timeout) → DLQ.
    with patch.object(rerun, "with_install_token_retry",
                      side_effect=httpx.RequestError("github down")):
        with pytest.raises(httpx.RequestError):
            rerun.handle_rerun_jobs(_event(_job()))


def test_rerun_raises_on_malformed_message():
    with pytest.raises(json.JSONDecodeError):
        rerun.handle_rerun_jobs(_event("not json"))  # → DLQ after retries


def test_rerun_failure_raises_and_never_reenqueues():
    """#418 loop guard: a failing re-run RAISES (so SQS redrives → DLQ) and
    NEVER enqueues another re-run — the consumer calls dispatch directly, so
    Elder self-recovery enqueues at most once per drop, never loops."""
    with patch.object(rerun, "with_install_token_retry", side_effect=lambda iid, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response()), \
         patch.object(rerun, "get_repo_config", return_value={}), \
         patch.object(rerun, "dispatch_code_review", side_effect=RuntimeError("review boom")), \
         patch.object(rerun, "enqueue_rerun") as enq:
        with pytest.raises(RuntimeError):
            rerun.handle_rerun_jobs(_event(_job()))
    enq.assert_not_called()
