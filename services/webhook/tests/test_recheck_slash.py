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

from unittest.mock import patch

import httpx
import pytest

import dispatcher as d
import personas.tpm.persona as tpm_persona


def _comment_payload(
    *,
    body: str,
    is_pr: bool = True,
    sender_login: str = "evan",
    pr_author: str = "evan",
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
    payload = _comment_payload(body="/grug recheck", sender_login="evan", pr_author="evan")
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
    payload = _comment_payload(body="/grug recheck", sender_login="bob", pr_author="evan")
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
    payload = _comment_payload(body="/grug recheck", sender_login="random", pr_author="evan")

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
    payload = _comment_payload(body="/grug recheck", sender_login="bob", pr_author="evan")
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
    payload = _comment_payload(body="/grug recheck", sender_login="evan", pr_author="evan")
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
    payload = _comment_payload(body="/grug recheck", sender_login="evan", pr_author="evan")
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
    payload = _comment_payload(body="/grug recheck", sender_login="evan", pr_author="evan")
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
    payload = _comment_payload(body="/grug recheck", sender_login="evan", pr_author="evan")
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


def _pull_request_payload():
    """The same PR as `_comment_payload` (myorg/myrepo #42, install 1),
    arriving as a `pull_request` webhook instead of a comment."""
    return {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "body": _PR_BODY_CLOSES_7,
            "head": {"sha": "abc123"},
            "user": {"login": "evan"},
        },
        "repository": {
            "id": 100, "name": "myrepo",
            "owner": {"login": "myorg"}, "full_name": "myorg/myrepo",
        },
        "installation": {"id": 1},
    }


def _fake_github(issue_7=_ISSUE_7_ONE_BOX_OPEN, *, issue_exc=None, parent_of_7=1):
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
            return _R({"head": {"sha": "abc123"}, "body": _PR_BODY_CLOSES_7})
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


def _run_both_paths(_github_get):
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
        webhook_out = d.dispatch("pull_request", _pull_request_payload())
        recheck_out = d.dispatch(
            "issue_comment",
            _comment_payload(body="/grug recheck", sender_login="evan", pr_author="evan"),
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
    assert "all 7 checks" not in title
    assert "5/7 checks" in title and "ticket done, which epic skipped" in title
    assert "| ticket done | skipped |" in summary


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
