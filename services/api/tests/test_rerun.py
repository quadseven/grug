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
        github_user_id=user_id, login="alice", role=role, tier="free",
        allowlisted=True, created_at="",
        allowlisted_at=None, allowlisted_by=None,
    )


def _install(owner_id="100", account_login="myorg"):
    return {"install_id": 11, "installed_by_user_id": owner_id,
            "account_login": account_login}


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


def test_rerun_endpoint_mismatched_repo_404_and_never_enqueues():
    # grug#1628: a repo outside the install's account, and not in the
    # install's visible repo list, is refused before it can reach the queue
    # (where the consumer would retry it into the DLQ).
    body = inst.RerunRequest(repo="someone-else/their-repo", pr_number=7, persona="elder")
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("installations._install_repo_names", return_value={"myorg/myrepo"}) as names, \
         patch("rerun.enqueue_rerun") as enq:
        with pytest.raises(HTTPException) as exc:
            inst.rerun_check(install_id=11, body=body, user=_user(user_id="100"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "repo not found"
    names.assert_called_once_with(11)
    enq.assert_not_called()


def test_rerun_endpoint_matching_owner_skips_membership_lookup():
    # The owner check is case-insensitive and needs no GitHub call.
    body = inst.RerunRequest(repo="MyOrg/myrepo", pr_number=7, persona="elder")
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("installations._install_repo_names") as names, \
         patch("rerun.enqueue_rerun") as enq:
        out = inst.rerun_check(install_id=11, body=body, user=_user(user_id="100"))
    assert out == {"status": "queued"}
    names.assert_not_called()
    enq.assert_called_once()


def test_rerun_endpoint_other_owner_repo_visible_to_install_is_queued():
    # Membership fallback (update_repo_config's lookup): a stale account_login
    # after an account rename must not block a repo the install can still see.
    body = inst.RerunRequest(repo="renamed-org/myrepo", pr_number=7, persona="elder")
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("installations._install_repo_names", return_value={"renamed-org/myrepo"}), \
         patch("rerun.enqueue_rerun") as enq:
        out = inst.rerun_check(install_id=11, body=body, user=_user(user_id="100"))
    assert out == {"status": "queued"}
    enq.assert_called_once_with(
        install_id=11, repo="renamed-org/myrepo", pr_number=7, persona="elder",
    )


def test_install_repo_names_lowercases_full_names():
    repos = [{"id": 1, "full_name": "MyOrg/Repo"}, {"id": 2, "full_name": "myorg/other"}]
    with patch("installations.with_install_token_retry",
               side_effect=lambda _iid, fn: fn("tok")), \
         patch("installations.paginate_all", return_value=repos) as pag:
        assert inst._install_repo_names(11) == {"myorg/repo", "myorg/other"}
    assert pag.call_args.kwargs["max_pages"] is None


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
        _row("myorg/b", 1, "elder"),                 # errored
        _row("myorg/b", 1, "elder"),                 # dup (repo,pr,persona) → 1 job
        _row("myorg/b", 2, "elder"),                 # errored, distinct
        _row("myorg/b", 3, "elder", errored=False),  # pass → skipped
    ]
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("installations.list_check_verdicts", return_value=rows), \
         patch("rerun.enqueue_rerun") as enq:
        out = inst.rerun_all_errored(install_id=11, user=_user(user_id="100"))
    assert out == {"queued": 2}  # 2 distinct errored, dup deduped, pass skipped
    keys = {(c.kwargs["repo"], c.kwargs["pr_number"]) for c in enq.call_args_list}
    assert keys == {("myorg/b", 1), ("myorg/b", 2)}


def test_rerun_all_skips_rows_for_repos_the_install_cannot_see():
    # grug#1628 defense in depth: a stored row naming a repo outside the
    # install is skipped, and the membership list is fetched once per call.
    rows = [
        _row("myorg/a", 1, "elder"),
        _row("stranger/x", 2, "elder"),
        _row("stranger/y", 3, "elder"),
    ]
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("installations.list_check_verdicts", return_value=rows), \
         patch("installations._install_repo_names", return_value={"myorg/a"}) as names, \
         patch("rerun.enqueue_rerun") as enq:
        out = inst.rerun_all_errored(install_id=11, user=_user(user_id="100"))
    assert out == {"queued": 1}
    assert [c.kwargs["repo"] for c in enq.call_args_list] == ["myorg/a"]
    names.assert_called_once_with(11)


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
         patch("installations.list_check_verdicts", return_value=[_row("myorg/b", 1, "elder")]), \
         patch("rerun.enqueue_rerun", side_effect=RuntimeError("no queue")):
        with pytest.raises(HTTPException) as exc:
            inst.rerun_all_errored(install_id=11, user=_user(user_id="100"))
    assert exc.value.status_code == 503


def test_rerun_all_no_errored_returns_zero():
    with patch("installations.get_installation", return_value=_install(owner_id="100")), \
         patch("installations.list_check_verdicts", return_value=[_row("myorg/b", 1, "elder", errored=False)]), \
         patch("rerun.enqueue_rerun") as enq:
        out = inst.rerun_all_errored(install_id=11, user=_user(user_id="100"))
    assert out == {"queued": 0}
    enq.assert_not_called()
