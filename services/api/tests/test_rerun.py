"""Tests for the re-run enqueuer + endpoint (#305, ADR-0004).

The endpoint enforces: install exists (404), caller owns it (403), queue
configured (503), else enqueue + 202. The enqueuer sends a FIFO message keyed
by repo "owner/name" with the (install,repo,pr,persona) dedup id. Deps patched.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import installations as inst
import rerun
from adapters.user_store import UserIdentity


def _user(user_id="100", role="user"):
    return UserIdentity(
        github_user_id=user_id, login="evan", role=role, tier="free",
        allowlisted=True, created_at="",
        allowlisted_at=None, allowlisted_by=None,
    )


def _install(owner_id="100"):
    return {"install_id": 11, "installed_by_user_id": owner_id}


# --- enqueuer -------------------------------------------------------------


def test_enqueue_rerun_sends_fifo_with_content_dedup(monkeypatch):
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs/grug-rerun-jobs.fifo")
    with patch.object(rerun._sqs, "send_message") as send:
        rerun.enqueue_rerun(install_id=11, repo="myorg/myrepo", pr_number=7, persona="elder")
    kw = send.call_args.kwargs
    assert kw["QueueUrl"] == "https://sqs/grug-rerun-jobs.fifo"
    assert kw["MessageGroupId"] == rerun.rerun_group_id(
        11, "myorg/myrepo", 7, "elder",
    )
    assert len(kw["MessageGroupId"]) <= 128
    # Dedup on (install,repo,pr,persona) — NO head_sha (re-run targets current head).
    assert kw["MessageDeduplicationId"] == "11:myorg/myrepo:7:elder"
    body = json.loads(kw["MessageBody"])
    assert body == {"schema_version": 1, "install_id": 11, "repo": "myorg/myrepo", "pr_number": 7, "persona": "elder"}


def test_enqueue_rerun_raises_when_queue_unconfigured(monkeypatch):
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "")
    with pytest.raises(RuntimeError):
        rerun.enqueue_rerun(install_id=1, repo="a/b", pr_number=3, persona="elder")


# --- endpoint -------------------------------------------------------------

_BODY = inst.RerunRequest(repo="myorg/myrepo", pr_number=7, persona="elder")


def test_rerun_endpoint_unknown_install_404():
    with patch("installations.get_installation", return_value=None):
        with pytest.raises(HTTPException) as exc:
            inst.rerun_check(install_id=999, body=_BODY, user=_user())
    assert exc.value.status_code == 404


def test_rerun_endpoint_not_owner_403():
    with patch("installations.get_installation", return_value=_install(owner_id="999")):
        with pytest.raises(HTTPException) as exc:
            inst.rerun_check(install_id=11, body=_BODY, user=_user(user_id="100"))
    assert exc.value.status_code == 403


def test_rerun_endpoint_enqueues_and_returns_queued():
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("rerun.enqueue_rerun") as enq:
        out = inst.rerun_check(install_id=11, body=_BODY, user=_user(user_id="100"))
    enq.assert_called_once_with(install_id=11, repo="myorg/myrepo", pr_number=7, persona="elder")
    assert out == {"status": "queued"}


def test_rerun_endpoint_503_when_queue_unconfigured():
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("rerun.enqueue_rerun", side_effect=RuntimeError("no queue")):
        with pytest.raises(HTTPException) as exc:
            inst.rerun_check(install_id=11, body=_BODY, user=_user(user_id="100"))
    assert exc.value.status_code == 503


def test_rerun_request_rejects_junk_persona_and_repo():
    with pytest.raises(Exception):
        inst.RerunRequest(repo="myorg/myrepo", pr_number=1, persona="carrier-pigeon")
    with pytest.raises(Exception):
        inst.RerunRequest(repo="not-a-slug", pr_number=1, persona="elder")  # needs owner/name


def test_rerun_request_accepts_teller_and_walkthrough():
    # #581: the consumer re-runs Teller/walkthrough, so the request model must
    # accept them - previously they 422'd, making the capability dead.
    for persona in ("teller", "walkthrough"):
        inst.RerunRequest(repo="o/r", pr_number=1, persona=persona)


def test_every_rerunnable_persona_is_requestable():
    # The #581 drift guard: no persona the consumer RE-RUNS may be rejected by
    # the request model (that is the dead-capability class). Derived from the
    # single shared source so the two can't diverge again.
    from rerun_personas import RERUNNABLE, REQUESTABLE

    assert RERUNNABLE <= REQUESTABLE
    for persona in RERUNNABLE:
        inst.RerunRequest(repo="o/r", pr_number=1, persona=persona)  # no 422


# --- batch: rerun_all_errored (#306) --------------------------------------

def _row(repo, pr, persona, errored=True):
    return {
        "repo": repo, "pr_number": pr, "persona": persona, "head_sha": "h",
        "conclusion": "neutral", "findings_count": 0,
        "degraded_reason": "llm_outage" if errored else None,
        "summary": "",
    }


def test_rerun_all_enqueues_distinct_errored_rows_only():
    rows = [
        _row("a/b", 1, "elder"),                 # errored
        _row("a/b", 1, "elder"),                 # dup (repo,pr,persona) → 1 job
        _row("a/b", 2, "elder"),                 # errored, distinct
        _row("a/b", 3, "elder", errored=False),  # pass → skipped
    ]
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("installations.list_check_verdicts", return_value=rows), \
         patch("rerun.enqueue_rerun") as enq:
        out = inst.rerun_all_errored(install_id=11, user=_user(user_id="100"))
    assert out == {"queued": 2}  # 2 distinct errored, dup deduped, pass skipped
    keys = {(c.kwargs["repo"], c.kwargs["pr_number"]) for c in enq.call_args_list}
    assert keys == {("a/b", 1), ("a/b", 2)}


def test_rerun_all_unknown_install_404():
    with patch("installations.get_installation", return_value=None):
        with pytest.raises(HTTPException) as exc:
            inst.rerun_all_errored(install_id=999, user=_user())
    assert exc.value.status_code == 404


def test_rerun_all_not_owner_403():
    with patch("installations.get_installation", return_value=_install(owner_id="999")):
        with pytest.raises(HTTPException) as exc:
            inst.rerun_all_errored(install_id=11, user=_user(user_id="100"))
    assert exc.value.status_code == 403


def test_rerun_all_503_when_queue_unconfigured():
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("installations.list_check_verdicts", return_value=[_row("a/b", 1, "elder")]), \
         patch("rerun.enqueue_rerun", side_effect=RuntimeError("no queue")):
        with pytest.raises(HTTPException) as exc:
            inst.rerun_all_errored(install_id=11, user=_user(user_id="100"))
    assert exc.value.status_code == 503


def test_rerun_all_no_errored_returns_zero():
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("installations.list_check_verdicts", return_value=[_row("a/b", 1, "elder", errored=False)]), \
         patch("rerun.enqueue_rerun") as enq:
        out = inst.rerun_all_errored(install_id=11, user=_user(user_id="100"))
    assert out == {"queued": 0}
    enq.assert_not_called()
