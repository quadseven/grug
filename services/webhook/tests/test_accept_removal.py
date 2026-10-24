"""Tests for `/grug accept-removal <path> <sha>` (#1405).

A deletion-only PR that removes a guard path gets a blocking
`removed-guarded-path` finding (#1401). The file is gone, so no inline
thread can answer it. A maintainer answers it with this command instead.

Trust rule: the command counts only from a User account (never a Bot or a
GitHub App) whose author_association is OWNER, MEMBER or COLLABORATOR and
who has write access. The acceptance is bound to the head at the time of
the command, and a new head brings the finding back.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import dispatcher as d
import httpx
import pytest
import rerun
from adapters import install_store, pg_install_store
from grug_commands import GrugCommand, parse_command

from tests.test_removed_files import (  # noqa: F401 - _patch_token is an autouse fixture
    _DELETION_DIFF,
    _WORKFLOW,
    _deleted,
    _patch_token,
    _run,
)

_HEAD = "abcd1234efgh"  # the head `_run` reviews
_SHA = "abcdef1234567890abcdef1234567890abcdef12"  # the PR head the command sees


# Parsing.


def test_parses_accept_removal_with_its_path():
    assert parse_command(f"/grug accept-removal {_WORKFLOW} {_SHA}") == GrugCommand(
        verb="accept-removal", arg=f"{_WORKFLOW} {_SHA}",
    )


@pytest.mark.parametrize(("arg", "sha"), [
    (f"{_WORKFLOW} {_SHA}", _SHA),
    (f"`{_WORKFLOW}` `{_SHA}`", _SHA),
    (f"{_WORKFLOW} {_SHA.upper()} looks fine", _SHA),
    (f"{_WORKFLOW} {_SHA[:7]}", ""),  # a prefix is refused
    (f"{_WORKFLOW} {_SHA[:39]}", ""),
    (f"{_WORKFLOW} {_SHA}0", ""),
    (f"{_WORKFLOW} notahexsha", ""),
    (_WORKFLOW, ""),
])
def test_the_head_sha_is_the_word_after_the_path(arg, sha):
    import accept_removal

    assert accept_removal.accept_args(arg) == (_WORKFLOW, sha)


@pytest.mark.parametrize("arg", [
    _WORKFLOW,
    f"`{_WORKFLOW}`",
    f"./{_WORKFLOW}",
    f"{_WORKFLOW} it checked nothing",
])
def test_the_path_is_the_first_token_without_quotes(arg):
    import accept_removal

    assert accept_removal.removal_path(arg) == _WORKFLOW


def test_no_path_is_no_path():
    import accept_removal

    assert accept_removal.removal_path("") == ""
    assert accept_removal.removal_path("``") == ""


# The command, through the real issue_comment dispatcher.


class _Store:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []
        self.enqueued: list[dict[str, Any]] = []
        self.replies: list[str] = []
        self.claimed: set[str] = set()

    def claim(self, key: str) -> bool:
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


def _payload(
    *, login: str = "maintainer", user_type: str | None = "User",
    association: str | None = "OWNER", pr_author: str = "maintainer",
    body: str = f"/grug accept-removal {_WORKFLOW} {_SHA}", app: object = None,
) -> dict[str, Any]:
    user: dict[str, Any] = {"login": login}
    if user_type is not None:
        user["type"] = user_type
    comment: dict[str, Any] = {"id": 555, "body": body, "user": user,
                               "performed_via_github_app": app}
    if association is not None:
        comment["author_association"] = association
    return {
        "action": "created",
        "issue": {"number": 42, "user": {"login": pr_author},
                  "pull_request": {"url": "..."}},
        "comment": comment,
        "repository": {"id": 100, "name": "myrepo",
                       "owner": {"login": "myorg"}, "full_name": "myorg/myrepo"},
        "installation": {"id": 1},
        "sender": {"login": login, "type": user_type or ""},
    }


def _dispatch(monkeypatch, payload: dict[str, Any], *, permission: str = "admin",
              head_sha: str = _SHA, store: _Store | None = None,
              commits: list[dict[str, Any]] | None = None,
              commits_fail: bool = False,
              ) -> tuple[dict[str, str], _Store]:
    if commits is None:
        author = payload["issue"]["user"]["login"]
        commits = [{"author": {"login": author}, "committer": {"login": "web-flow"}}]
    store = store or _Store()
    monkeypatch.setattr(d, "is_install_allowlisted", lambda _id: True)
    monkeypatch.setattr(d, "is_persona_enabled", lambda *_: True)
    monkeypatch.setattr(
        pg_install_store, "record_removal_acceptance",
        lambda **k: store.recorded.append(k),
    )
    monkeypatch.setattr(pg_install_store, "claim_delivery", store.claim)
    monkeypatch.setattr(rerun, "enqueue_rerun", lambda **k: store.enqueued.append(k))

    class _Resp:
        def __init__(self, body: Any) -> None:
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._body

    def _get(url: str, *a: Any, **k: Any) -> _Resp:
        if "/collaborators/" in url:
            return _Resp({"permission": permission})
        if "/pulls/42/commits" in url:
            if commits_fail:
                raise httpx.ConnectError("listing down")
            return _Resp(commits)
        if "/pulls/42" in url:
            return _Resp({"head": {"sha": head_sha}, "body": "",
                          "user": {"login": payload["issue"]["user"]["login"]}})
        raise AssertionError(f"unexpected GET {url}")

    def _post(url: str, *a: Any, json: Any = None, **k: Any) -> _Resp:
        store.replies.append((json or {}).get("body", ""))
        return _Resp({})

    with patch("github_app_auth.with_install_token_retry",
               side_effect=lambda _i, fn: fn("tok")), \
         patch("httpx.get", side_effect=_get), patch("httpx.post", side_effect=_post):
        out = d.dispatch("issue_comment", payload)
    return out, store


def test_a_trusted_maintainer_records_the_acceptance_on_the_head(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload())
    assert out["status"] == "dispatched", out
    assert store.recorded == [{
        "install_id": 1, "repo": "myorg/myrepo", "pr_number": 42,
        "head_sha": _SHA, "path": _WORKFLOW, "accepted_by": "maintainer",
        "comment_id": 555,
    }]
    # Elder re-reviews, so the check updates with no further step.
    assert [j["persona"] for j in store.enqueued] == ["code_reviewer"]
    assert store.enqueued[0]["dedup_key"]
    assert any(_SHA[:8] in r and _WORKFLOW in r for r in store.replies)


@pytest.mark.parametrize(("association", "permission"), [
    ("OWNER", "write"), ("COLLABORATOR", "admin"), ("MEMBER", "admin"),
])
def test_the_pr_author_may_accept_as_owner_or_admin(monkeypatch, association, permission):
    """A solo maintainer authors their own PRs and can still accept."""
    out, store = _dispatch(monkeypatch, _payload(
        login="solo", pr_author="solo", association=association,
    ), permission=permission)
    assert out["status"] == "dispatched", out
    assert store.recorded and store.recorded[0]["accepted_by"] == "solo"


@pytest.mark.parametrize(("association", "permission"), [
    ("COLLABORATOR", "write"), ("MEMBER", "maintain"), ("MEMBER", "write"),
])
def test_the_pr_author_with_only_write_access_is_refused(monkeypatch, association, permission):
    out, store = _dispatch(monkeypatch, _payload(
        login="author", pr_author="author", association=association,
    ), permission=permission)
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


@pytest.mark.parametrize("role", ["author", "committer"])
def test_a_write_user_who_pushed_a_commit_onto_someone_elses_pr_is_refused(monkeypatch, role):
    other = "committer" if role == "author" else "author"
    out, store = _dispatch(monkeypatch, _payload(
        login="pusher", pr_author="author", association="COLLABORATOR",
    ), permission="write", commits=[
        {"author": {"login": "author"}, "committer": {"login": "web-flow"}},
        {role: {"login": "pusher"}, other: {"login": "author"}},
    ])
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


def test_an_admin_who_pushed_a_commit_may_accept(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(
        login="pusher", pr_author="author", association="MEMBER",
    ), permission="admin", commits=[{"author": {"login": "pusher"}}])
    assert out["status"] == "dispatched", out
    assert store.recorded and store.recorded[0]["accepted_by"] == "pusher"


def test_a_failed_commit_listing_fails_closed(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(
        login="reviewer", pr_author="author", association="COLLABORATOR",
    ), permission="write", commits_fail=True)
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


def test_write_access_may_accept_someone_elses_pr(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(
        login="reviewer", pr_author="author", association="COLLABORATOR",
    ), permission="write")
    assert out["status"] == "dispatched", out
    assert store.recorded and store.recorded[0]["accepted_by"] == "reviewer"


@pytest.mark.parametrize("named", [
    "1" * 40,  # another head
    _SHA[:7],  # a prefix of the head
])
def test_a_sha_that_does_not_match_the_head_is_refused(monkeypatch, named):
    out, store = _dispatch(monkeypatch, _payload(
        body=f"/grug accept-removal {_WORKFLOW} {named}",
    ))
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []
    # the reply names the exact command for the current head
    assert any(f"/grug accept-removal {_WORKFLOW} {_SHA}" in r for r in store.replies)


def test_a_command_without_a_sha_is_refused(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(
        body=f"/grug accept-removal {_WORKFLOW}",
    ))
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


def test_a_duplicate_comment_delivery_records_nothing(monkeypatch):
    store = _Store()
    first, store = _dispatch(monkeypatch, _payload(), store=store)
    again, store = _dispatch(monkeypatch, _payload(), store=store)
    assert first["status"] == "dispatched", first
    assert again["status"] == "no_op", again
    assert len(store.recorded) == 1 and len(store.enqueued) == 1


def test_a_claimed_comment_records_nothing():
    import accept_removal

    store = _Store()
    store.claimed.add("grugcmd:3")
    out = accept_removal.run_accept_removal(
        f"{_WORKFLOW} {_SHA}", install_id=1, owner="o", repo="r",
        pr_number=2, comment_id=3, head_sha=_SHA, actor="maintainer",
        token_fn=lambda fn: fn("tok"),
        record=lambda **k: store.recorded.append(k),
        enqueue=lambda **k: store.enqueued.append(k),
        claim=store.claim, reply=lambda body: store.replies.append(body) or True,
    )
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


@pytest.mark.parametrize("seam", ["record", "enqueue", "claim"])
def test_a_non_callable_seam_fails_closed(seam):
    import accept_removal

    store = _Store()
    seams: dict[str, Any] = {
        "record": lambda **k: store.recorded.append(k),
        "enqueue": lambda **k: store.enqueued.append(k),
        "claim": store.claim,
    }
    seams[seam] = "not callable"
    out = accept_removal.run_accept_removal(
        f"{_WORKFLOW} {_SHA}", install_id=1, owner="o", repo="r",
        pr_number=2, comment_id=3, head_sha=_SHA, actor="maintainer",
        token_fn=lambda fn: fn("tok"), reply=lambda body: True, **seams,
    )
    assert out["status"] == "skip", out
    assert store.recorded == [] and store.enqueued == [] and not store.claimed


@pytest.mark.parametrize("user_type", ["Bot", "Organization", "Mannequin"])
def test_a_bot_account_changes_nothing(monkeypatch, user_type):
    out, store = _dispatch(monkeypatch, _payload(
        login="lane[bot]", user_type=user_type, association="MEMBER",
    ))
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


def test_a_comment_posted_through_a_github_app_changes_nothing(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(app={"id": 9, "slug": "lane"}))
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


@pytest.mark.parametrize("association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"])
def test_a_non_collaborator_changes_nothing(monkeypatch, association):
    out, store = _dispatch(monkeypatch, _payload(
        login="stranger", pr_author="stranger", association=association,
    ))
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


def test_a_collaborator_without_write_access_changes_nothing(monkeypatch):
    """The PR author shortcut that `/grug improve` takes does not apply."""
    out, store = _dispatch(monkeypatch, _payload(
        login="reader", pr_author="reader", association="COLLABORATOR",
    ), permission="read")
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


@pytest.mark.parametrize("missing", ["association", "type"])
def test_a_missing_association_or_type_fails_closed(monkeypatch, missing):
    payload = _payload(
        association=None if missing == "association" else "OWNER",
        user_type=None if missing == "type" else "User",
    )
    out, store = _dispatch(monkeypatch, payload)
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


def test_a_comment_author_that_is_not_the_sender_fails_closed(monkeypatch):
    payload = _payload()
    payload["sender"]["login"] = "someone-else"
    out, store = _dispatch(monkeypatch, payload)
    assert out["status"] == "no_op", out
    assert store.recorded == []


def test_a_comment_without_an_id_fails_closed(monkeypatch):
    payload = _payload()
    del payload["comment"]["id"]
    out, store = _dispatch(monkeypatch, payload)
    assert out["status"] == "no_op", out
    assert store.recorded == []


def test_an_unknown_head_fails_closed(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(), head_sha="")
    assert out["status"] == "skip", out
    assert store.recorded == [] and store.enqueued == []


def test_no_path_records_nothing(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(body="/grug accept-removal"))
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


def test_a_failed_record_does_not_enqueue(monkeypatch):
    import accept_removal

    def _boom(**_k: Any) -> None:
        raise RuntimeError("db down")

    store = _Store()
    out = accept_removal.run_accept_removal(
        f"{_WORKFLOW} {_SHA}", install_id=1, owner="o", repo="r",
        pr_number=2, comment_id=3, head_sha=_SHA, actor="maintainer",
        token_fn=lambda fn: fn("tok"), record=_boom,
        enqueue=lambda **k: store.enqueued.append(k),
        claim=store.claim,
        reply=lambda body: store.replies.append(body) or True,
    )
    assert out["status"] == "skip", out
    assert store.enqueued == []
    assert store.replies  # the reader is told it did not take


# Elder: the acceptance clears that path's finding on that head only.


def _accepted(monkeypatch, rows: dict[str, dict[str, dict[str, Any]]] | Exception):
    """Stub the store read: rows maps head_sha -> {path: acceptance}."""
    def _read(*, install_id: int, repo: str, pr_number: int, head_sha: str):
        if isinstance(rows, Exception):
            raise rows
        return rows.get(head_sha, {})

    monkeypatch.setattr(install_store, "get_removal_acceptances", _read)


_ACCEPTANCE = {"accepted_by": "maintainer", "comment_id": 555}


def test_an_accepted_guard_removal_passes_and_names_who_and_the_head(monkeypatch):
    _accepted(monkeypatch, {_HEAD: {_WORKFLOW: _ACCEPTANCE}})
    final = _run(monkeypatch, _DELETION_DIFF, {})[-1]
    assert final.conclusion == "success", (final.conclusion, final.title)
    assert "maintainer" in final.summary
    assert _HEAD[:8] in final.summary
    assert _WORKFLOW in final.summary


def test_accepting_another_path_changes_nothing(monkeypatch):
    _accepted(monkeypatch, {_HEAD: {".github/workflows/other.yml": _ACCEPTANCE}})
    final = _run(monkeypatch, _DELETION_DIFF, {})[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert "removed-guarded-path" in final.summary


def test_a_new_head_brings_the_finding_back(monkeypatch):
    _accepted(monkeypatch, {"0ldhead00000": {_WORKFLOW: _ACCEPTANCE}})
    final = _run(monkeypatch, _DELETION_DIFF, {})[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert "removed-guarded-path" in final.summary


def test_an_unreadable_acceptance_store_keeps_the_block(monkeypatch):
    _accepted(monkeypatch, RuntimeError("db down"))
    final = _run(monkeypatch, _DELETION_DIFF, {})[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert "removed-guarded-path" in final.summary


def test_accepting_one_guard_leaves_the_others_blocking(monkeypatch):
    other = "CODEOWNERS"
    _accepted(monkeypatch, {_HEAD: {_WORKFLOW: _ACCEPTANCE}})
    final = _run(monkeypatch, _deleted(_WORKFLOW, other), {})[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert other in final.summary


def test_an_acceptance_never_clears_a_live_reference(monkeypatch):
    _accepted(monkeypatch, {_HEAD: {_WORKFLOW: _ACCEPTANCE}})
    final = _run(monkeypatch, _deleted(_WORKFLOW), {
        "docs/ci.md": f"See `{_WORKFLOW}`.\n",
        "Makefile": f"lint:\n\tact -W {_WORKFLOW}\n",
    })[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert "removed-but-referenced" in final.summary


def test_an_acceptance_rerun_is_not_collapsed_into_an_earlier_one(monkeypatch):
    """A re-run queued moments earlier never read the new acceptance, so the
    queue's 5-minute dedup must not swallow this one."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "https://sqs.example/q.fifo")
    monkeypatch.setattr(rerun._sqs, "send_message", lambda **kw: sent.append(kw))
    coords: dict[str, Any] = {"install_id": 11, "repo": "o/r", "pr_number": 7,
                              "persona": "code_reviewer"}
    rerun.enqueue_rerun(**coords)
    rerun.enqueue_rerun(**coords, dedup_key="accept-removal:1")
    rerun.enqueue_rerun(**coords, dedup_key="accept-removal:2")
    ids = [m["MessageDeduplicationId"] for m in sent]
    assert len(set(ids)) == 3
    assert all(len(i) <= 128 for i in ids)
    assert len({m["MessageGroupId"] for m in sent}) == 1  # still serial per PR


def test_the_blocking_finding_prints_the_exact_command_for_this_head(monkeypatch):
    _accepted(monkeypatch, {})
    final = _run(monkeypatch, _DELETION_DIFF, {})[-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert f"/grug accept-removal {_WORKFLOW} {_HEAD}" in final.summary
