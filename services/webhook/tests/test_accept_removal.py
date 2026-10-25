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
        self.marked: list[int] = []
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
              perm_fail: str = "", diff: str | None = None,
              ) -> tuple[dict[str, str], _Store]:
    store = store or _Store()
    monkeypatch.setattr(d, "is_install_allowlisted", lambda _id: True)
    monkeypatch.setattr(d, "is_persona_enabled", lambda *_: True)
    monkeypatch.setattr(
        pg_install_store, "record_removal_acceptance",
        lambda **k: store.recorded.append(k),
    )
    monkeypatch.setattr(pg_install_store, "claim_delivery", store.claim)
    monkeypatch.setattr(rerun, "enqueue_rerun", lambda **k: store.enqueued.append(k))
    import interactive
    monkeypatch.setattr(
        interactive, "_mark_reviewing",
        lambda **k: store.marked.append(k["pr_number"]) or True,
    )

    class _Resp:
        def __init__(self, body: Any) -> None:
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._body

        @property
        def text(self) -> str:
            return str(self._body)

    def _get(url: str, *a: Any, **k: Any) -> _Resp:
        if "/collaborators/" in url:
            if perm_fail == "transport":
                raise httpx.ConnectError("permission lookup down")
            if perm_fail == "http":
                request = httpx.Request("GET", url)
                raise httpx.HTTPStatusError(
                    "boom", request=request,
                    response=httpx.Response(502, request=request),
                )
            return _Resp({"permission": permission})
        if "/pulls/42" in url and "diff" in str((k.get("headers") or {}).get("Accept")):
            return _Resp(_DELETION_DIFF if diff is None else diff)
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


def test_an_admin_records_the_acceptance_on_the_head(monkeypatch):
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
    # The same sign `/grug improve` raises while Elder re-reviews.
    assert store.marked == [42]


@pytest.mark.parametrize(("association", "permission"), [
    ("OWNER", "write"), ("COLLABORATOR", "admin"), ("MEMBER", "admin"),
])
def test_an_admin_may_accept_their_own_pr(monkeypatch, association, permission):
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


@pytest.mark.parametrize(("association", "permission"), [
    ("COLLABORATOR", "write"), ("MEMBER", "maintain"), ("MEMBER", "write"),
])
def test_write_only_access_is_refused_even_on_someone_elses_pr(
    monkeypatch, association, permission,
):
    out, store = _dispatch(monkeypatch, _payload(
        login="reviewer", pr_author="author", association=association,
    ), permission=permission)
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


@pytest.mark.parametrize("association", ["COLLABORATOR", "MEMBER", "OWNER"])
def test_an_admin_may_accept_someone_elses_pr(monkeypatch, association):
    out, store = _dispatch(monkeypatch, _payload(
        login="reviewer", pr_author="author", association=association,
    ), permission="admin")
    assert out["status"] == "dispatched", out
    assert store.recorded and store.recorded[0]["accepted_by"] == "reviewer"


@pytest.mark.parametrize("failure", ["http", "transport"])
def test_a_failed_permission_lookup_is_refused(monkeypatch, failure):
    out, store = _dispatch(monkeypatch, _payload(
        login="reviewer", pr_author="author", association="MEMBER",
    ), perm_fail=failure)
    assert out["status"] == "skip", out
    assert store.recorded == [] and store.enqueued == []


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


# Every refusal is answered once (the live case: a write-only collaborator
# got no reply and no reaction).


@pytest.mark.parametrize(("association", "permission"), [
    ("COLLABORATOR", "write"), ("MEMBER", "maintain"),
])
def test_a_non_admin_is_told_why_and_what_works(monkeypatch, association, permission):
    out, store = _dispatch(monkeypatch, _payload(
        login="reviewer", pr_author="author", association=association,
    ), permission=permission)
    assert out["status"] == "no_op", out
    assert len(store.replies) == 1, store.replies
    assert ("only a repo admin, or a trusted operator on a pr they did not write, can accept a "
            f"removal; your permission here is {permission}") in store.replies[0].lower()
    assert "ask a repo admin or a trusted operator" in store.replies[0].lower()
    assert store.recorded == [] and store.enqueued == []


def test_a_refused_comment_is_answered_once_across_redeliveries(monkeypatch):
    store = _Store()
    for _ in range(2):
        _dispatch(monkeypatch, _payload(
            login="reviewer", pr_author="author", association="COLLABORATOR",
        ), permission="write", store=store)
    assert len(store.replies) == 1, store.replies


def test_a_failed_permission_lookup_is_answered(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(
        login="reviewer", pr_author="author", association="MEMBER",
    ), perm_fail="http")
    assert out["status"] == "skip", out
    assert len(store.replies) == 1 and "could not check" in store.replies[0]


def test_a_non_collaborator_is_told_why(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(
        login="stranger", pr_author="stranger", association="NONE",
    ))
    assert out["status"] == "no_op", out
    assert len(store.replies) == 1 and "repo admin" in store.replies[0]


@pytest.mark.parametrize("user_type", ["Bot", "Organization", "Mannequin"])
def test_a_bot_is_never_answered(monkeypatch, user_type):
    _, store = _dispatch(monkeypatch, _payload(
        login="lane[bot]", user_type=user_type, association="MEMBER",
    ))
    assert store.replies == []


def test_a_path_that_is_not_flagged_is_answered_and_records_nothing(monkeypatch):
    out, store = _dispatch(monkeypatch, _payload(
        body=f"/grug accept-removal docs/other.md {_SHA}",
    ))
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []
    assert len(store.replies) == 1 and "not a deleted guard" in store.replies[0]
    assert _WORKFLOW in store.replies[0]  # names what would work


def test_an_unreadable_diff_records_nothing_and_says_so(monkeypatch):
    import accept_removal

    monkeypatch.setattr(accept_removal, "flagged_removals", lambda *a, **k: None)
    out, store = _dispatch(monkeypatch, _payload())
    assert out["status"] == "skip", out
    assert store.recorded == [] and store.enqueued == []
    assert len(store.replies) == 1 and "could not read" in store.replies[0]


# Trusted operators. A personal account's repos cannot grant a collaborator
# admin, so the owner names extra logins (owner:login pairs, in SSM) who may
# accept a removal with write access, on a PR someone else wrote.


def _trust(monkeypatch, pairs: set[tuple[str, str]]) -> None:
    import secrets_loader

    monkeypatch.setattr(secrets_loader, "get_removal_acceptors", lambda: frozenset(pairs))


@pytest.mark.parametrize("permission", ["write", "maintain"])
def test_a_trusted_operator_with_write_may_accept_someone_elses_pr(monkeypatch, permission):
    _trust(monkeypatch, {("myorg", "reviewer")})
    out, store = _dispatch(monkeypatch, _payload(
        login="Reviewer", pr_author="author", association="COLLABORATOR",
    ), permission=permission)
    assert out["status"] == "dispatched", out
    assert store.recorded and store.recorded[0]["accepted_by"] == "Reviewer"


@pytest.mark.parametrize(("pairs", "login", "pr_author", "permission"), [
    ({("otherorg", "reviewer")}, "reviewer", "author", "write"),  # trusted for another owner
    ({("myorg", "reviewer")}, "reviewer", "reviewer", "write"),  # their own PR
    ({("myorg", "reviewer")}, "reviewer", "author", "read"),  # no write access
    ({("myorg", "someone")}, "reviewer", "author", "write"),  # not listed
])
def test_a_trusted_operator_is_refused_outside_its_grant(
    monkeypatch, pairs, login, pr_author, permission,
):
    _trust(monkeypatch, pairs)
    out, store = _dispatch(monkeypatch, _payload(
        login=login, pr_author=pr_author, association="COLLABORATOR",
    ), permission=permission)
    assert out["status"] == "no_op", out
    assert store.recorded == [] and store.enqueued == []


def test_the_trusted_list_is_read_from_ssm_and_fails_closed(monkeypatch):
    import secrets_loader

    class _Ssm:
        def __init__(self, value: str | None) -> None:
            self.value = value

        def get_parameter(self, Name: str) -> dict[str, Any]:
            if self.value is None:
                raise RuntimeError("ParameterNotFound")
            return {"Parameter": {"Value": self.value}}

    def read(env: str, value: str | None) -> frozenset[tuple[str, str]]:
        monkeypatch.setenv("GRUG_REMOVAL_ACCEPTORS_SSM", env)
        monkeypatch.setattr(secrets_loader, "_ssm", _Ssm(value))
        secrets_loader.get_removal_acceptors.cache_clear()
        return secrets_loader.get_removal_acceptors()

    assert read("/grug/removal-acceptors", "MyOrg:Reviewer, other:login\nbad bad: :x a:b:c") == {
        ("myorg", "reviewer"), ("other", "login"),
    }
    assert read("/grug/removal-acceptors", None) == frozenset()
    assert read("", "myorg:reviewer") == frozenset()
