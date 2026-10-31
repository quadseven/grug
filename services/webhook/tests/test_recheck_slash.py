"""Tests for #2 — `/grug recheck` slash command via issue_comment.

Covers:
- Trigger pattern matches case-insensitively + with surrounding whitespace
- Non-PR comments no_op
- Non-trigger comments no_op
- PR author always authorized
- Non-author with admin/maintain/write authorized
- Non-author with read/triage/none REJECTED
- Non-allowlisted install no_op
- Per-repo TPM disable no_op
- Successful path re-fetches PR + dispatches to TPM persona
- #782: recheck and the pull_request webhook reach the SAME verdict on a
  PR whose linked issue has an unchecked box (recheck used to skip the
  linked-issue check by never wiring a fetcher), and a genuine fetch
  failure still fails open - as a visibly `skipped` check, not a pass.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml

import dispatcher as d
import personas.tpm.persona as tpm_persona

_K8S = Path(__file__).resolve().parents[3] / "k8s"


def _comment_payload(
    *,
    body: str,
    is_pr: bool = True,
    sender_login: str = "alice",
    pr_author: str = "alice",
    install_id: int = 1,
    repo_id: int = 100,
):
    return {
        "action": "created",
        "issue": {
            "number": 42,
            "user": {"login": pr_author},
            "pull_request": {"url": "..."} if is_pr else None,
        },
        "comment": {"body": body},
        "repository": {
            "id": repo_id, "name": "myrepo",
            "owner": {"login": "myorg"}, "full_name": "myorg/myrepo",
        },
        "installation": {"id": install_id},
        "sender": {"login": sender_login},
    }


@pytest.fixture
def _no_install_lookups(monkeypatch):
    """Skip allowlist + persona toggle DDB calls."""
    monkeypatch.setattr(d, "is_install_allowlisted", lambda _id: True)
    monkeypatch.setattr(d, "is_persona_enabled", lambda *_: True)


def test_recheck_pattern_matches_case_insensitive():
    assert d._RECHECK_PAT.search("/grug recheck")
    assert d._RECHECK_PAT.search("/Grug Recheck")
    assert d._RECHECK_PAT.search("  /grug   recheck please  ")


def test_recheck_pattern_rejects_other_commands():
    assert not d._RECHECK_PAT.search("/grug status")
    assert not d._RECHECK_PAT.search("grug recheck")  # missing /
    assert not d._RECHECK_PAT.search("nope")


def test_no_trigger_text_no_ops(_no_install_lookups):
    payload = _comment_payload(body="lgtm")
    out = d.dispatch("issue_comment", payload)
    assert out["status"] == "no_op"
    assert "no /grug command trigger" in out["reason"]


def test_non_pr_issue_comment_no_ops(_no_install_lookups):
    payload = _comment_payload(body="/grug recheck", is_pr=False)
    out = d.dispatch("issue_comment", payload)
    assert out["status"] == "no_op"
    assert "non-PR" in out["reason"]


def test_non_created_action_no_ops(_no_install_lookups):
    payload = _comment_payload(body="/grug recheck")
    payload["action"] = "edited"
    out = d.dispatch("issue_comment", payload)
    assert out["status"] == "no_op"
    assert "issue_comment action=edited" in out["reason"]


def test_pr_author_authorized_path_dispatches(_no_install_lookups):
    payload = _comment_payload(body="/grug recheck", sender_login="alice", pr_author="alice")
    fake_pr = {"head": {"sha": "abc123"}, "body": "## Why\nbecause closes #1\n## Acceptance criteria\n- [x] one\n- [x] two\n- [x] three\n## Out of scope\nnone\n\n**Size:** S"}

    class _Result:
        passed = True

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")):
        with patch("httpx.get") as get_mock, \
             patch("personas.tpm.persona.evaluate_pull_request", return_value=_Result()) as eval_mock, \
             patch("personas.tpm.persona.publish_tpm_evaluation") as _pub_mock:
            get_mock.return_value.raise_for_status = lambda: None
            get_mock.return_value.json.return_value = fake_pr
            out = d.dispatch("issue_comment", payload)

    assert out["status"] == "dispatched"
    assert out["trigger"] == "recheck"
    assert out["result"] == "pass"
    eval_mock.assert_called_once()
    # Pure evaluate now takes only pr_body; head_sha / pr_number flow
    # to publish_tpm_evaluation per spec 0002's pure/impure split.
    pub_kwargs = _pub_mock.call_args.kwargs
    assert pub_kwargs["head_sha"] == "abc123"
    assert pub_kwargs["pr_number"] == 42


def test_non_author_with_write_perm_authorized(_no_install_lookups):
    payload = _comment_payload(body="/grug recheck", sender_login="bob", pr_author="alice")
    fake_pr = {"head": {"sha": "def456"}, "body": ""}

    class _Result:
        passed = False

    perm_responses = [
        # First call: permission lookup
        {"raise_for_status": None, "json": {"permission": "write"}},
        # Second call: PR fetch
        {"raise_for_status": None, "json": fake_pr},
    ]
    call_idx = [0]

    def _httpx_get(*args, **kwargs):
        resp = perm_responses[call_idx[0]]
        call_idx[0] += 1

        class _R:
            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return resp["json"]
        return _R()

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")):
        with patch("httpx.get", side_effect=_httpx_get), \
             patch("personas.tpm.persona.evaluate_pull_request", return_value=_Result()), \
             patch("personas.tpm.persona.publish_tpm_evaluation"):
            out = d.dispatch("issue_comment", payload)

    assert out["status"] == "dispatched"
    assert out["result"] == "fail"


def test_non_author_with_read_perm_rejected(_no_install_lookups):
    payload = _comment_payload(body="/grug recheck", sender_login="random", pr_author="alice")

    def _httpx_get(*args, **kwargs):
        class _R:
            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return {"permission": "read"}
        return _R()

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")):
        with patch("httpx.get", side_effect=_httpx_get):
            out = d.dispatch("issue_comment", payload)

    assert out["status"] == "no_op"
    assert "lacks write perm" in out["reason"]


def test_non_allowlisted_install_no_ops(monkeypatch):
    monkeypatch.setattr(d, "is_install_allowlisted", lambda _id: False)
    monkeypatch.setattr(d, "is_persona_enabled", lambda *_: True)
    payload = _comment_payload(body="/grug recheck")
    out = d.dispatch("issue_comment", payload)
    assert out["status"] == "no_op"
    assert "not allowlisted" in out["reason"]


def test_tpm_disabled_per_repo_no_ops(monkeypatch):
    monkeypatch.setattr(d, "is_install_allowlisted", lambda _id: True)
    monkeypatch.setattr(d, "is_persona_enabled", lambda *_: False)
    payload = _comment_payload(body="/grug recheck")
    out = d.dispatch("issue_comment", payload)
    assert out["status"] == "no_op"
    assert "tpm disabled" in out["reason"]


def test_perm_lookup_transport_error_returns_skip(_no_install_lookups, mock_transport_client):
    """async-blocker-hunter F-01: transport error during perm lookup
    must return skip + log, not 500.

    Real-transport-backed (issue #105): ConnectError comes from
    httpx.MockTransport handler raising.
    """
    payload = _comment_payload(body="/grug recheck", sender_login="bob", pr_author="alice")
    client = mock_transport_client(raise_exc=httpx.ConnectError("DNS failure"))

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")):
        with patch("httpx.get", side_effect=lambda *a, **kw: client.get(*a, **kw)):
            out = d.dispatch("issue_comment", payload)

    assert out["status"] == "skip"
    assert "transport" in out["reason"]


def test_pr_fetch_transport_error_returns_skip(_no_install_lookups, mock_transport_client):
    """async-blocker-hunter F-01: transport error during PR re-fetch
    must return skip + log, not 500.

    Real-transport-backed (issue #105): ReadTimeout comes from
    httpx.MockTransport handler raising.
    """
    payload = _comment_payload(body="/grug recheck", sender_login="alice", pr_author="alice")
    client = mock_transport_client(raise_exc=httpx.ReadTimeout("github.com slow"))

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")):
        with patch("httpx.get", side_effect=lambda *a, **kw: client.get(*a, **kw)):
            out = d.dispatch("issue_comment", payload)

    assert out["status"] == "skip"
    assert "transport" in out["reason"]


def test_recheck_unexpected_raise_contained_not_500(_no_install_lookups):
    """FLINT on #575: main.py forwards dispatch() exceptions into a
    webhook 500 and GitHub does NOT auto-redeliver on 5xx — an
    unexpected raise from evaluate/publish must be contained by the
    recheck final guard (mirror of
    test_pull_request_publish_unexpected_raise_hits_final_guard)."""
    payload = _comment_payload(body="/grug recheck", sender_login="alice", pr_author="alice")
    fake_pr = {"head": {"sha": "abc123"}, "body": "irrelevant"}

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")):
        with patch("httpx.get") as get_mock, \
             patch("personas.tpm.persona.evaluate_pull_request",
                   side_effect=RuntimeError("evaluator regression")):
            get_mock.return_value.raise_for_status = lambda: None
            get_mock.return_value.json.return_value = fake_pr
            out = d.dispatch("issue_comment", payload)

    assert out == {"status": "skip", "trigger": "recheck", "reason": "unhandled_error"}


def test_recheck_publish_failed_sentinel_returns_skip(_no_install_lookups):
    """#550: publish no longer raises on a failed publish — the seam
    returns the sentinel. The recheck path must map it to the same
    skip/publish_failed shape it returned pre-migration (and the seam
    already recorded the honest errored Activity row)."""
    payload = _comment_payload(body="/grug recheck", sender_login="alice", pr_author="alice")
    fake_pr = {"head": {"sha": "abc123"}, "body": "irrelevant"}

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")):
        with patch("httpx.get") as get_mock, \
             patch("personas.tpm.persona.evaluate_pull_request") as eval_mock, \
             patch("personas.tpm.persona.publish_tpm_evaluation",
                   return_value={"persona": "tpm", "result": "publish_failed"}):
            eval_mock.return_value = type("R", (), {"passed": True})()
            get_mock.return_value.raise_for_status = lambda: None
            get_mock.return_value.json.return_value = fake_pr
            out = d.dispatch("issue_comment", payload)

    assert out == {"status": "skip", "trigger": "recheck", "reason": "publish_failed"}


def test_recheck_resultless_map_lands_in_guard_not_500(_no_install_lookups):
    """The result_map subscript sits INSIDE the recheck final guard on
    purpose: a seam regression returning a map without "result" must
    yield skip/unhandled_error, not a webhook 500 (GitHub does not
    redeliver on 5xx). A refactor hoisting the subscript above the try
    fails this test."""
    payload = _comment_payload(body="/grug recheck", sender_login="alice", pr_author="alice")
    fake_pr = {"head": {"sha": "abc123"}, "body": "irrelevant"}

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")):
        with patch("httpx.get") as get_mock, \
             patch("personas.tpm.persona.evaluate_pull_request") as eval_mock, \
             patch("personas.tpm.persona.publish_tpm_evaluation",
                   return_value={"persona": "tpm"}):
            eval_mock.return_value = type("R", (), {"passed": True})()
            get_mock.return_value.raise_for_status = lambda: None
            get_mock.return_value.json.return_value = fake_pr
            out = d.dispatch("issue_comment", payload)

    assert out == {"status": "skip", "trigger": "recheck", "reason": "unhandled_error"}


# --- #782: recheck must evaluate the same checks the webhook path does ---

# A Hunt Plan that passes every pure check and closes #7. The ONLY thing
# that can fail it is the linked issue's unchecked box - so a "pass" from
# either path below can only come from the linked-issue check not running.
_PR_BODY_CLOSES_7 = (
    "## Why\nbecause we need it badly\n"
    "## Acceptance criteria\n- a\n- b\n- c\n"
    "## Out of scope\nx\nSize: S\ncloses #7"
)
_ISSUE_7_ONE_BOX_OPEN = "## Acceptance criteria\n- [x] done\n- [ ] still open\n"


def _pull_request_payload(pr_extra=None):
    """The same PR as `_comment_payload` (myorg/myrepo #42, install 1),
    arriving as a `pull_request` webhook instead of a comment."""
    return {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "body": _PR_BODY_CLOSES_7,
            "head": {"sha": "abc123"},
            "user": {"login": "alice"},
            **(pr_extra or {}),
        },
        "repository": {
            "id": 100, "name": "myrepo",
            "owner": {"login": "myorg"}, "full_name": "myorg/myrepo",
        },
        "installation": {"id": 1},
    }


def _fake_github(
    issue_7=_ISSUE_7_ONE_BOX_OPEN, *, issue_exc=None, parent_of_7=1, pr_extra=None,
):
    """One fake GitHub for BOTH paths: the PR re-fetch only recheck needs,
    and the linked-issue fetch both paths must make. Anything else is a
    call the test did not model and must not be silently absorbed."""
    def _get(url, **_kw):
        class _R:
            def __init__(self, body):
                self._body = body

            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return self._body

        if url.endswith("/repos/myorg/myrepo/pulls/42"):
            return _R({
                "head": {"sha": "abc123"}, "body": _PR_BODY_CLOSES_7,
                **(pr_extra or {}),
            })
        if url.endswith("/repos/myorg/myrepo/issues/7/parent"):
            # grug#1034's facts fetcher: 404 means "no parent", not a failure.
            if issue_exc is not None:
                raise issue_exc
            if parent_of_7 is None:
                r = _R({"message": "Not Found"})
                r.status_code = 404
                return r
            return _R({"number": parent_of_7})
        if url.endswith("/repos/myorg/myrepo/issues/7"):
            if issue_exc is not None:
                raise issue_exc
            return _R({"body": issue_7})
        raise AssertionError(f"unmodelled GitHub call: {url}")
    return _get


def _run_both_paths(_github_get, pr_extra=None):
    """Drive the pull_request webhook AND `/grug recheck` for the same PR
    through the REAL evaluator, capturing the TpmEvaluation each path
    hands to the publisher. Returns (webhook_out, webhook_eval,
    recheck_out, recheck_eval)."""
    captured: list[tpm_persona.TpmEvaluation] = []

    def _publish(evaluation, **_kw):
        captured.append(evaluation)
        return {"persona": "tpm", "result": "pass" if evaluation.passed else "fail"}

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")), \
         patch("httpx.get", side_effect=_github_get), \
         patch("personas.tpm.persona.publish_tpm_evaluation", side_effect=_publish), \
         patch("personas.tpm.ticket_compliance_run.run_ticket_compliance",
               return_value={"status": "ok"}):
        webhook_out = d.dispatch("pull_request", _pull_request_payload(pr_extra))
        recheck_out = d.dispatch(
            "issue_comment",
            _comment_payload(body="/grug recheck", sender_login="alice", pr_author="alice"),
        )
    assert len(captured) == 2, captured
    return webhook_out, captured[0], recheck_out, captured[1]


@pytest.fixture
def _tpm_only(monkeypatch):
    """Allowlisted install, Chief the only persona on - so the webhook
    path's `personas` list holds exactly the tpm result."""
    monkeypatch.setattr(d, "is_install_allowlisted", lambda _id: True)
    monkeypatch.setattr(d, "is_persona_enabled", lambda *a: a[2] == "tpm")


def test_recheck_and_webhook_agree_on_unchecked_linked_issue(_tpm_only):
    """#782 acceptance: recheck and the pull_request webhook must produce
    the SAME verdict for a PR whose linked issue has an unchecked box.

    Comparative on purpose. Asserting only "recheck now fails" would stay
    green if a later refactor made BOTH paths fail open; asserting only
    equality would stay green for the same reason. Both together pin the
    honest outcome: the two paths agree, AND what they agree on is the
    red the unchecked box earns."""
    webhook_out, webhook_eval, recheck_out, recheck_eval = _run_both_paths(
        _fake_github(),
    )

    # Same per-check results, check for check - not just the same rollup.
    assert recheck_eval.results == webhook_eval.results
    assert recheck_eval.passed is webhook_eval.passed
    assert recheck_eval.conclusion == webhook_eval.conclusion

    # ...and that shared verdict is the honest one.
    assert webhook_out["personas"][0]["result"] == "fail"
    assert recheck_out["result"] == "fail"
    lic = {r.name: r for r in recheck_eval.results}["linked-issue-completeness"]
    assert lic.passed is False and lic.skipped is False
    assert "#7" in lic.detail and "still open" in lic.detail


def test_recheck_fetch_failure_still_fails_open_but_visibly_skipped(_tpm_only):
    """#782 criterion 4: a GitHub blip fetching the linked issue must NOT
    block the merge - and criterion 3: it must not render as a pass
    either. Both paths fall open identically, the check is `skipped`,
    and the title stops claiming all six ran."""
    webhook_out, webhook_eval, recheck_out, recheck_eval = _run_both_paths(
        _fake_github(issue_exc=httpx.ConnectError("github.com unreachable")),
    )

    assert recheck_eval.results == webhook_eval.results
    assert webhook_out["personas"][0]["result"] == "pass"
    assert recheck_out["result"] == "pass"
    lic = {r.name: r for r in recheck_eval.results}["linked-issue-completeness"]
    assert lic.passed is True and lic.skipped is True

    title, summary = tpm_persona._summary(list(recheck_eval.results))
    assert "all 9 checks" not in title
    assert "6/9 checks" in title and "ticket done, which epic, in scope skipped" in title
    assert "| ticket done | skipped |" in summary
    assert "| in scope | skipped |" in summary


def test_recheck_and_webhook_agree_that_an_orphan_ticket_blocks(_tpm_only):
    """grug#1034 through the REAL evaluator on both paths: #7's boxes are all
    ticked, so the only red is `which epic` - its native parent is missing
    (404) and its body names none."""
    webhook_out, webhook_eval, recheck_out, recheck_eval = _run_both_paths(
        _fake_github(issue_7="## Acceptance criteria\n- [x] done\n", parent_of_7=None),
    )
    assert recheck_eval.results == webhook_eval.results
    assert webhook_out["personas"][0]["result"] == "fail"
    assert recheck_out["result"] == "fail"
    epic = {r.name: r for r in recheck_eval.results}["linked-issue-epic"]
    assert epic.passed is False and epic.skipped is False and "#7" in epic.detail


def test_recheck_and_webhook_pass_when_the_ticket_has_a_parent(_tpm_only):
    webhook_out, webhook_eval, recheck_out, recheck_eval = _run_both_paths(
        _fake_github(issue_7="## Acceptance criteria\n- [x] done\n", parent_of_7=3),
    )
    assert recheck_eval.results == webhook_eval.results
    assert webhook_out["personas"][0]["result"] == "pass"
    assert recheck_out["result"] == "pass"
    epic = {r.name: r for r in recheck_eval.results}["linked-issue-epic"]
    assert epic.passed is True and epic.skipped is False


_OPERATOR_FOOTER_NO_WHY = {
    # grug#1251: an operator session's PR - agent footer, no `## Why`.
    "body": _PR_BODY_CLOSES_7.replace("## Why\n", "## Context\n")
    + "\nhttps://claude.ai/code/session_01Ht7JaVzv2aacJ9r6Srso9r\n",
    "user": {"login": "alice", "type": "User"},
    "author_association": "COLLABORATOR",
}


def test_recheck_and_webhook_both_check_an_operator_agent_pr(_tpm_only):
    """grug#1251 through the REAL evaluator on both paths: the operator's
    agent footer no longer switches Chief off, so the missing `## Why`
    fails on the webhook AND on `/grug recheck`."""
    webhook_out, webhook_eval, recheck_out, recheck_eval = _run_both_paths(
        _fake_github(
            issue_7="## Acceptance criteria\n- [x] done\n",
            pr_extra=_OPERATOR_FOOTER_NO_WHY,
        ),
        pr_extra=_OPERATOR_FOOTER_NO_WHY,
    )
    assert recheck_eval.results == webhook_eval.results
    assert webhook_eval.conclusion == recheck_eval.conclusion == "failure"
    assert webhook_eval.rule == recheck_eval.rule == tpm_persona.RULE_OPERATOR_CHECKED
    assert [r.name for r in recheck_eval.results if not r.passed] == ["why"]
    assert webhook_out["personas"][0]["result"] == "fail"
    assert recheck_out["result"] == "fail"


def test_recheck_and_webhook_skip_an_agent_pr_from_a_non_operator(_tpm_only):
    extra = {**_OPERATOR_FOOTER_NO_WHY, "author_association": "CONTRIBUTOR"}
    _, webhook_eval, _, recheck_eval = _run_both_paths(
        _fake_github(pr_extra=extra), pr_extra=extra,
    )
    assert webhook_eval.conclusion == recheck_eval.conclusion == "neutral"
    assert webhook_eval.rule == tpm_persona.RULE_SKIP_NOT_OPERATOR


def test_run_chief_recheck_evaluates_then_publishes(monkeypatch):
    """The shared function behind `/grug recheck` and the poller's Chief
    self-heal: evaluate with the PR body, publish at the given head."""
    import personas.tpm.issue_fetcher as fetchers

    seen = {}
    monkeypatch.setattr(fetchers, "build_issue_fetcher", lambda **kw: "F")
    monkeypatch.setattr(fetchers, "build_issue_facts_fetcher", lambda **kw: "FF")

    def _eval(body, fetch_issue=None, fetch_issue_facts=None, author_trusted=False,
              fetch_changed_files=None, advisory_checks=None):
        seen["eval"] = (body, fetch_issue, fetch_issue_facts, fetch_changed_files,
                        advisory_checks)
        seen["trusted"] = author_trusted
        return "EVAL"

    def _publish(evaluation, **kw):
        seen["publish"] = (evaluation, kw)
        return {"persona": "tpm", "result": "pass"}

    monkeypatch.setattr(tpm_persona, "evaluate_pull_request", _eval)
    monkeypatch.setattr(tpm_persona, "publish_tpm_evaluation", _publish)
    evaluation, result_map = d.run_chief_recheck(
        installation_id=5, owner="o", repo="r", head_sha="abc", pr_number=9, pr_body="B",
    )
    assert evaluation == "EVAL" and result_map["result"] == "pass"
    body, fi, ff, fcf, ac = seen["eval"]
    assert body == "B" and fi == "F" and ff == "FF"
    assert callable(fcf)  # the shared changed-files fetcher is wired through
    assert "in-scope" in ac  # advisory set passed through
    assert seen["trusted"] is False  # default keeps the pre-#1251 skip
    assert seen["publish"][1] == {
        "installation_id": 5, "owner": "o", "repo": "r", "head_sha": "abc",
        "pr_number": 9, "advisory_checks": ac,
    }


# --- grug#1270: a rate-limited command is KEPT, never dropped --------------


class _InteractiveHttp:
    """Answers the command's reply post without touching the network."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.urls: list[str] = []

    def post(self, url: str, **kw: object):
        self.posts.append(kw.get("json") or {})
        self.urls.append(url)
        return _Ok()

    def get(self, url: str, **kw: object):
        return _Ok()


class _Ok:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {}


def _rate_limited_get(*_a: object, **_kw: object):
    request = httpx.Request("GET", "https://api.github.com/repos/myorg/myrepo/pulls/42")
    response = httpx.Response(
        403,
        headers={
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": "1791218759",
            "x-ratelimit-resource": "core",
        },
        request=request,
    )
    raise httpx.HTTPStatusError("rate limited", request=request, response=response)


def test_grug_improve_rate_limited_is_kept_not_dropped(_no_install_lookups, monkeypatch):
    """grug#1270 AC3: the installation's hourly GitHub budget was exhausted,
    so the PR fetch is refused with a 403. The command must be KEPT - deferred
    to the budget's reset - and never dropped with no reply and no retry,
    which is what made the 2026-10-18 burst look like it worked."""
    import interactive

    payload = _comment_payload(body="/grug improve", sender_login="alice", pr_author="alice")
    enqueued: list[dict] = []
    http = _InteractiveHttp()
    monkeypatch.setattr(interactive, "httpx", http)
    monkeypatch.setattr("adapters.pg_install_store.claim_delivery", lambda key: True)

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")), \
         patch("httpx.get", side_effect=_rate_limited_get), \
         patch("rerun.enqueue_rerun", side_effect=lambda **kw: enqueued.append(kw)):
        out = d.dispatch("issue_comment", payload)

    assert out["status"] == "deferred"
    assert out["reason"] == "github_rate_limited"
    assert enqueued, "the command must be kept on the queue, not dropped"
    assert enqueued[0]["not_before"] == 1791218759.0
    assert enqueued[0]["persona"] == "code_reviewer"
    assert http.posts and "budget" in str(http.posts[0].get("body", ""))


def test_grug_improve_that_is_not_rate_limited_still_runs(_no_install_lookups, monkeypatch):
    """A plain permission 403 is NOT a budget problem: it must not be
    answered by waiting for the window to reset."""
    import interactive

    payload = _comment_payload(body="/grug improve", sender_login="alice", pr_author="alice")
    http = _InteractiveHttp()
    monkeypatch.setattr(interactive, "httpx", http)
    monkeypatch.setattr("adapters.pg_install_store.claim_delivery", lambda key: True)

    def _forbidden(*_a: object, **_kw: object):
        request = httpx.Request("GET", "https://api.github.com/repos/myorg/myrepo/pulls/42")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")), \
         patch("httpx.get", side_effect=_forbidden), \
         patch("rerun.enqueue_rerun") as enq:
        out = d.dispatch("issue_comment", payload)

    assert out["status"] == "skip" and out["reason"] == "pr_fetch_failed"
    enq.assert_not_called()


def _forbidden_get(*_a: object, **_kw: object):
    request = httpx.Request("GET", "https://api.github.com/repos/myorg/myrepo/pulls/42")
    response = httpx.Response(403, request=request)
    raise httpx.HTTPStatusError("forbidden", request=request, response=response)


def _timeout_get(*_a: object, **_kw: object):
    raise httpx.ReadTimeout("github.com slow")


@pytest.mark.parametrize("verb", ["improve", "resume", "review"])
@pytest.mark.parametrize("fetch,reason", [
    (_forbidden_get, "pr_fetch_failed"),
    (_timeout_get, "pr_fetch_transport_failed"),
])
def test_an_elder_command_whose_pr_fetch_fails_is_answered(
    _no_install_lookups, monkeypatch, verb, fetch, reason,
):
    """grug#1430: a PR fetch that fails leaves no head to queue against, so
    the command is ANSWERED ("comment again") instead of skipped in silence."""
    import interactive

    payload = _comment_payload(body=f"/grug {verb}", sender_login="alice", pr_author="alice")
    http = _InteractiveHttp()
    monkeypatch.setattr(interactive, "httpx", http)

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")), \
         patch("httpx.get", side_effect=fetch), \
         patch("rerun.enqueue_rerun") as enq:
        out = d.dispatch("issue_comment", payload)

    assert out["status"] == "skip" and out["reason"] == reason
    enq.assert_not_called()
    bodies = [str(p.get("body", "")) for p in http.posts]
    assert len(bodies) == 1
    assert "could not read this PR" in bodies[0]
    assert f"/grug {verb}` again" in bodies[0]


def test_a_recheck_whose_pr_fetch_fails_keeps_its_own_handling(
    _no_install_lookups, monkeypatch,
):
    """Only Elder re-run verbs get the new answer; Chief's recheck is not
    changed by grug#1430."""
    import interactive

    payload = _comment_payload(body="/grug recheck", sender_login="alice", pr_author="alice")
    http = _InteractiveHttp()
    monkeypatch.setattr(interactive, "httpx", http)

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")), \
         patch("httpx.get", side_effect=_forbidden_get):
        out = d.dispatch("issue_comment", payload)

    assert out["status"] == "skip"
    assert http.posts == []


def test_budget_reset_epoch_defers_when_the_budget_is_low(monkeypatch):
    """grug#1270: below the floor of the installation's hourly budget, a
    re-run waits for the reset instead of spending what is left."""
    import github_app_auth

    monkeypatch.setattr(
        github_app_auth, "core_rate_limit", lambda token: (10, 5000, 1791218759),
    )
    monkeypatch.setattr(
        github_app_auth, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )
    assert d._budget_reset_epoch(1) == 1791218759.0


def test_budget_reset_epoch_is_zero_while_there_is_budget(monkeypatch):
    import github_app_auth

    monkeypatch.setattr(
        github_app_auth, "core_rate_limit", lambda token: (4000, 5000, 1791218759),
    )
    monkeypatch.setattr(
        github_app_auth, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )
    assert d._budget_reset_epoch(1) == 0.0


def test_budget_reset_epoch_is_zero_when_the_budget_cannot_be_read(monkeypatch):
    """An unreadable budget must never block a command."""
    import github_app_auth

    monkeypatch.setattr(github_app_auth, "core_rate_limit", lambda token: None)
    monkeypatch.setattr(
        github_app_auth, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )
    assert d._budget_reset_epoch(1) == 0.0


# --- grug#1445: an allowed App Bot runs Elder re-runs like a collaborator ---

_ALLOWED_BOTS = {"loom-brain[bot]", "loom-station[bot]"}


def _improve_payload(
    *,
    sender_login: str = "loom-brain[bot]",
    sender_type: str = "Bot",
    pr_author: str = "alice",
    install_id: int = 1,
    repo_id: int = 100,
    comment_id: int = 777,
) -> dict:
    return {
        "action": "created",
        "issue": {
            "number": 42,
            "user": {"login": pr_author},
            "pull_request": {"url": "..."},
        },
        "comment": {"body": "/grug improve", "id": comment_id},
        "repository": {
            "id": repo_id, "name": "myrepo",
            "owner": {"login": "myorg"}, "full_name": "myorg/myrepo",
        },
        "installation": {"id": install_id},
        "sender": {"login": sender_login, "type": sender_type},
    }


def _pr_response(sha: str = "abc123"):
    class _R:
        def raise_for_status(self_inner):
            pass

        def json(self_inner):
            return {"head": {"sha": sha}, "body": ""}

    return _R()


def test_allowed_app_bot_queues_improve_with_no_collaborator_record(
    _no_install_lookups, monkeypatch,
):
    """An allowed App Bot (not the PR author, no collaborator record) queues
    an Elder re-run on the head."""
    import interactive
    import review_label

    monkeypatch.setenv("GRUG_ALLOWED_APP_BOT_LOGINS", "loom-brain[bot], loom-station[bot]")
    monkeypatch.setattr(d, "_budget_reset_epoch", lambda _id: 0.0)
    # No review is in flight on this head.
    monkeypatch.setattr(review_label, "elder_check_state", lambda **k: "absent")
    http = _InteractiveHttp()
    monkeypatch.setattr(interactive, "httpx", http)
    monkeypatch.setattr("adapters.pg_install_store.claim_delivery", lambda key: True)

    enqueued: list[dict] = []

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")), \
         patch("httpx.get", return_value=_pr_response()), \
         patch("rerun.enqueue_rerun", side_effect=lambda **kw: enqueued.append(kw)):
        out = d.dispatch("issue_comment", _improve_payload())

    assert out["status"] == "dispatched"
    assert out["persona"] == "code_reviewer"
    assert enqueued, "the allowed bot's improve must queue an Elder re-run"
    assert enqueued[0]["persona"] == "code_reviewer"
    assert enqueued[0]["repo"] == "myorg/myrepo"
    # Only the PR fetch is made - the collaborator lookup is short circuited.
    # The command is an `issue_comment`, so the reaction goes to issues/comments
    # and is the only post: no reply comment (grug#1433).
    assert http.urls == [
        "https://api.github.com/repos/myorg/myrepo/issues/comments/777/reactions",
    ]
    assert http.posts == [{"content": "eyes"}]


def test_unknown_app_bot_is_refused_with_a_reply(
    _no_install_lookups, monkeypatch, mock_transport_client,
):
    """A Bot outside the allowed list is refused with a reply naming why,
    and no Elder re-run is queued."""
    import interactive

    monkeypatch.setenv("GRUG_ALLOWED_APP_BOT_LOGINS", "loom-brain[bot], loom-station[bot]")
    http = _InteractiveHttp()
    monkeypatch.setattr(interactive, "httpx", http)
    monkeypatch.setattr("adapters.pg_install_store.claim_delivery", lambda key: True)

    client = mock_transport_client(status_codes=[404], json_bodies=[{}])
    enqueued: list[dict] = []

    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")), \
         patch("httpx.get", side_effect=lambda *a, **kw: client.get(*a, **kw)), \
         patch("rerun.enqueue_rerun", side_effect=lambda **kw: enqueued.append(kw)):
        out = d.dispatch(
            "issue_comment",
            _improve_payload(sender_login="random-bot[bot]", sender_type="Bot"),
        )

    assert out["status"] == "skip"
    assert out["reason"] == "perm_lookup_failed"
    assert not enqueued, "an unknown bot must not queue an Elder re-run"
    assert len(http.posts) == 1
    body = str(http.posts[0].get("body", ""))
    assert "refused" in body and "/grug improve" in body


# --- production wiring --------------------------------------------------------

def test_deployed_allowed_app_bot_logins_is_not_empty():
    """The webhook deployment must list at least one allowed App bot login,
    else an allowed-App /grug improve is dropped exactly as the ticket's Why
    describes (empty = feature off)."""
    docs = [doc for doc in yaml.safe_load_all((_K8S / "webhook-deployment.yaml").read_text()) if doc]
    (deploy,) = [doc for doc in docs if doc.get("kind") == "Deployment"]
    (container,) = deploy["spec"]["template"]["spec"]["containers"]
    env = {entry["name"]: entry.get("value") for entry in container.get("env", [])}
    value = env.get("GRUG_ALLOWED_APP_BOT_LOGINS", "")
    assert value, "GRUG_ALLOWED_APP_BOT_LOGINS must be set on the webhook container"
    logins = {name.lower() for name in value.replace(",", " ").split()}
    assert {"loom-brain[bot]", "loom-station[bot]", "loom-lanes[bot]",
            "agent-anthropic-claude-code[bot]", "agent-meta-muse[bot]",
            "agent-openai-dot[bot]", "agent-openai-codex-luna[bot]",
            "agent-google-gemini[bot]"} <= logins

