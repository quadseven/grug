"""grug#1212 / ADR-0023: a degraded Elder review fails closed in BLOCK mode.

A repo whose Elder is blocking has `Grug - Elder` as a required check, and
GitHub counts a required `neutral` as passing. So in BLOCK mode every
degraded exit concludes `failure`; WARN mode keeps `neutral`; and the first
summary line of every degraded check says so and how to re-run.

Each exit that can complete the Elder check for a degraded review is driven
here: the dispatch publish (every degraded reason), the fetch/parse
fallback, the durable lane's terminal checks (freshness outage, unknown
skip, intent-drift give-up) and the dead-letter announcement.
"""
from __future__ import annotations

import json
import types
from unittest.mock import MagicMock, patch

import httpx
import pytest
from llm_client import Backend, LlmReviewResponse
from llm_client import Finding as LlmFinding
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.degraded_gate import (
    degraded_conclusion,
    degraded_lead_line,
)
from personas.code_reviewer.persona import CodeReviewEvaluation
from review_pipeline import ReviewCoverage


def _lead(reason: str) -> str:
    # The decision's wording, spelled out literally so a drift in the helper
    # cannot pass by also changing the expectation.
    return (
        f"Degraded review ({reason}): not a verdict. "
        "Comment `/grug improve` to re-run once the backend recovers."
    )


_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def _payload() -> dict:
    return {
        "action": "opened",
        "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {
            "number": 7,
            "head": {"sha": "abcd1234efgh"},
            "base": {"sha": "base5678ijkl"},
            "title": "Preserve PR intent",
            "body": "Reviewer should understand the requested behavior.",
            "user": {"login": "alice"},
        },
    }


def _diff_response():
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = _DIFF
    return r


@pytest.fixture
def elder(monkeypatch):
    """Drive `dispatch_code_review` with a canned LLM answer; return the
    posted check-runs."""
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )
    # Tier-1 only: the async deep arm is covered in test_async_deep.py.
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "0")
    posted: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 2})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())

    def run(llm: LlmReviewResponse, *, blocking: bool) -> list:
        monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
        with patch("httpx.get", return_value=_diff_response()):
            cr_dispatch.dispatch_code_review(_payload(), blocking=blocking)
        return posted

    return run


_DEGRADED = {
    "all_failed": LlmReviewResponse(kind="all_failed", error="poolside: timeout"),
    "parse_failed": LlmReviewResponse(kind="parse_failed", error="prose, not JSON"),
    "partial_review": LlmReviewResponse(
        kind="reviewed",
        error="partial review: cohorts [2] failed",
        backend_used=Backend.POOLSIDE,
        coverage=ReviewCoverage(
            total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
            cohort_labels=("a", "b"),
        ),
    ),
    # "Files not walked" is a partial review whose coverage names the files.
    "partial_review_files_not_walked": LlmReviewResponse(
        kind="reviewed",
        error="partial review: cohort skipped: staged review budget exhausted",
        backend_used=Backend.POOLSIDE,
        coverage=ReviewCoverage(
            total_cohorts=2, completed_cohorts=1, failed_cohorts=(),
            cohort_labels=("a", "b"), unattempted_cohorts=(2,),
            unwalked_paths=("src/y.py",),
        ),
    ),
    "low_confidence_review": LlmReviewResponse(
        kind="reviewed",
        error="low confidence: short empty answer, no other model answered",
        backend_used=Backend.POOLSIDE,
    ),
}


def _reason(case: str) -> str:
    return "partial_review" if case.startswith("partial_review") else case


@pytest.mark.parametrize("case", sorted(_DEGRADED))
def test_block_mode_degraded_review_concludes_failure(elder, case):
    """BLOCK + every degraded reason -> `failure`, never a passing neutral."""
    posted = elder(_DEGRADED[case], blocking=True)

    assert len(posted) == 1
    assert posted[0].conclusion == "failure"
    assert posted[0].summary.split("\n")[0] == _lead(_reason(case))
    # The body must not contradict the gate.
    assert "merge not blocked" not in posted[0].summary
    assert "stays advisory" not in posted[0].summary


@pytest.mark.parametrize("case", sorted(_DEGRADED))
def test_warn_mode_degraded_review_stays_neutral(elder, case):
    """WARN mode is unchanged: the same degraded review stays `neutral`, and
    still leads with the degraded line."""
    posted = elder(_DEGRADED[case], blocking=False)

    assert len(posted) == 1
    assert posted[0].conclusion == "neutral"
    assert posted[0].summary.split("\n")[0] == _lead(_reason(case))


def test_block_mode_clean_review_still_succeeds(elder):
    """The fail-closed rule touches degraded reviews only."""
    clean = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.POOLSIDE,
    )
    posted = elder(clean, blocking=True)

    assert posted[0].conclusion == "success"
    assert not posted[0].summary.startswith("Degraded review")


def test_block_mode_blocking_finding_still_fails_with_request_changes(elder):
    llm = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity="high", message="error is discarded",
        ),),
        backend_used=Backend.POOLSIDE,
    )
    posted = elder(llm, blocking=True)

    assert posted[0].conclusion == "failure"
    assert not posted[0].summary.startswith("Degraded review")


def test_block_mode_no_diff_is_not_a_degraded_review(elder):
    """`no_diff` means there was nothing to review, not a failed review."""
    posted = elder(LlmReviewResponse(kind="no_diff"), blocking=True)

    assert posted[0].conclusion == "neutral"
    assert not posted[0].summary.startswith("Degraded review")


# --- grug#1231: a findings answer cut off at the output cap -----------------

def _cut_off_review(monkeypatch, severity: str) -> LlmReviewResponse:
    """Run a cut-off findings answer through the REAL review path (parser,
    arm, response) so the dispatch test sees what production would."""
    import llm_client as lc

    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")

    complete = ", ".join(
        json.dumps({"path": "src/x.py", "line": 2, "rule": f"rule-{i}",
                    "severity": severity, "message": "Grug see a problem."})
        for i in range(3)
    )
    content = '{"findings": [' + complete + ', {"path": "src/x.py", "li'
    body = {
        "model": "m",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    }
    with patch.object(httpx, "post", return_value=httpx.Response(200, json=body)):
        return lc.review_diff([lc.Hunk(path="src/x.py", body="@@ -1 +1 @@\n-a\n+b")],
                              installation_id=1)


def _publish_offline(elder, llm: LlmReviewResponse, *, blocking: bool) -> list:
    """Publish with every later model call (judge, refute) failing fast
    instead of reaching the network."""
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("offline")):
        return elder(llm, blocking=blocking)


@pytest.mark.parametrize("blocking, want", [(True, "failure"), (False, "neutral")])
def test_cut_off_findings_answer_is_a_degraded_partial_review(
    elder, monkeypatch, blocking, want,
):
    """grug#1231: a recovered cut-off answer holds fewer findings than the
    model meant to write. It must never read as a clean complete review:
    it is `partial_review`, so BLOCK fails closed (grug#1222) and WARN stays
    neutral. Fails on main (the answer was parse_failed with no findings)."""
    llm = _cut_off_review(monkeypatch, "low")
    assert llm.kind == "reviewed" and len(llm.findings) == 3

    posted = _publish_offline(elder, llm, blocking=blocking)

    assert posted[0].conclusion == want
    assert posted[0].summary.split("\n")[0] == _lead("partial_review")


def test_cut_off_findings_with_a_blocking_finding_fails_in_block_mode(elder, monkeypatch):
    """A recovered blocking finding is a real failure either way."""
    llm = _cut_off_review(monkeypatch, "high")

    posted = _publish_offline(elder, llm, blocking=True)

    assert posted[0].conclusion == "failure"


@pytest.mark.parametrize("blocking, want", [(True, "failure"), (False, "neutral")])
def test_fetch_or_parse_fallback_honours_the_mode(monkeypatch, blocking, want):
    """`_publish_degraded` (diff fetch/parse failed) is a degraded exit too."""
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )
    posted: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    with patch("httpx.get", side_effect=httpx.ConnectError("github down")):
        out = cr_dispatch.dispatch_code_review(_payload(), blocking=blocking)

    assert out["degraded_reason"] == "fetch_or_parse_failed"
    assert posted[0].conclusion == want
    assert posted[0].summary.split("\n")[0] == _lead("fetch_or_parse_failed")


def test_shared_gate_is_unchanged_for_personas_that_do_not_opt_in():
    """Guard and Smasher share `_publish_shape`; #1212 is Elder's policy."""
    ev = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="all_failed",
    )
    assert cr_dispatch._publish_shape(ev, mode="blocking") == ("neutral", "COMMENT")
    assert cr_dispatch._publish_shape(
        ev, mode="blocking", degraded_fails_closed=True,
    ) == ("failure", "COMMENT")
    assert cr_dispatch._publish_shape(
        ev, mode="advisory", degraded_fails_closed=True,
    ) == ("neutral", "COMMENT")


def test_degraded_gate_helpers():
    assert degraded_conclusion(True) == "failure"
    assert degraded_conclusion(False) == "neutral"
    # A mode that cannot be read fails closed.
    assert degraded_conclusion(None) == "failure"
    assert degraded_lead_line("all_failed") == _lead("all_failed")
    assert degraded_lead_line("") == _lead("unknown")


# --- the durable lane's terminal checks ------------------------------------

def _pr_data(head_sha="fresh-head", title="Improve review depth"):
    return {
        "number": 7, "title": title, "body": "Refs #6.", "state": "open",
        "draft": False, "user": {"login": "alice"},
        "head": {"sha": head_sha},
        "base": {"sha": "base-sha", "repo": {"id": 222}},
    }


def _job(**over) -> str:
    d = {
        "schema_version": 1, "install_id": 11, "repo": "myorg/myrepo",
        "pr_number": 7, "persona": "elder", "kind": "review",
        "settle_seconds": 0,
    }
    d.update(over)
    return json.dumps(d)


def _patch_claims(monkeypatch):
    for name, value in (
        ("acquire_review_claim", "acquired"),
        ("complete_review_claim", True),
        ("release_review_claim", True),
    ):
        monkeypatch.setattr(
            f"adapters.install_store.{name}", MagicMock(return_value=value),
        )


def _run_terminal(monkeypatch, *, degraded_reason: str, blocking: bool) -> list:
    import rerun

    pr = _pr_data()
    fetches = iter((pr, pr))

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    posted: list = []
    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(
        rerun, "get_repo_config",
        lambda iid, rid: {"code_reviewer_blocking": blocking},
    )
    monkeypatch.setattr(rerun, "_set_board_in_progress_note", lambda *a: None)
    _patch_claims(monkeypatch)
    monkeypatch.setattr(
        rerun, "dispatch_code_review",
        MagicMock(return_value={
            "persona": "code_reviewer", "result": "skipped",
            "degraded_reason": degraded_reason,
        }),
    )
    rerun._run_one(_job())
    return posted


@pytest.mark.parametrize(
    "reason", ["freshness_check_failed", "something_nobody_modelled"],
)
@pytest.mark.parametrize("blocking, want", [(True, "failure"), (False, "neutral")])
def test_rerun_fail_open_terminal_check_honours_the_mode(
    monkeypatch, reason, blocking, want,
):
    """The durable lane's own terminal completions are degraded exits: a
    `neutral` here in BLOCK mode would pass the required check."""
    posted = _run_terminal(monkeypatch, degraded_reason=reason, blocking=blocking)

    assert len(posted) == 1
    assert posted[0].conclusion == want
    assert posted[0].summary.split("\n")[0] == _lead(reason)


def test_rerun_terminal_check_uses_the_mode_the_review_ran_under(monkeypatch):
    """The mode is read once per pass and frozen: a toggle flip after the
    review started cannot change the terminal check's conclusion."""
    import rerun

    modes = iter(({"code_reviewer_blocking": True}, {"code_reviewer_blocking": False}))
    reads: list = []

    def flipping_config(iid, rid):
        reads.append(rid)
        return next(modes)

    _run_terminal(monkeypatch, degraded_reason="freshness_check_failed", blocking=True)
    monkeypatch.setattr(rerun, "get_repo_config", flipping_config)
    dispatched = MagicMock(return_value={
        "persona": "code_reviewer", "result": "skipped",
        "degraded_reason": "freshness_check_failed",
    })
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatched)
    pr = _pr_data()
    fetches = iter((pr, pr))
    posted: list = []

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    rerun._run_one(_job())

    assert len(reads) == 1
    assert dispatched.call_args.kwargs["blocking"] is True
    assert posted[-1].conclusion == "failure"


@pytest.mark.parametrize("blocking, want", [(True, "failure"), (False, "neutral")])
def test_rerun_intent_drift_give_up_honours_the_mode(monkeypatch, blocking, want):
    """Giving up on a PR that kept changing mid-review leaves no review on
    the current head."""
    import rerun

    pulls = iter((
        _pr_data(head_sha="same-head", title="Before"),
        _pr_data(head_sha="same-head", title="After"),
    ))

    def _fake_token_retry(iid, fn):
        try:
            return next(pulls)
        except StopIteration:
            return fn("tok")

    posted: list = []
    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(
        rerun, "get_repo_config",
        lambda iid, rid: {"code_reviewer_blocking": blocking},
    )
    _patch_claims(monkeypatch)
    monkeypatch.setattr(rerun.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(rerun, "_enqueue_current_review", MagicMock())
    monkeypatch.setattr(rerun, "dispatch_code_review", MagicMock())

    status = rerun._run_one(_job(
        requested_head_sha="same-head", settle_seconds=90,
        redrive_count=rerun._MAX_INTENT_DRIFT_REDRIVES,
    ))

    assert status == "intent_drift_exhausted"
    assert len(posted) == 1
    assert posted[0].conclusion == want
    assert posted[0].summary.split("\n")[0] == _lead("intent_drift_exhausted")


def test_superseded_head_stays_neutral_in_block_mode(monkeypatch):
    """A superseded head is not the head that merges; closing it neutral is
    by design and not a degraded review of the current head."""
    import rerun

    fetches = iter((_pr_data(head_sha="new-head"),))

    def _fake_token_retry(iid, fn):
        try:
            return next(fetches)
        except StopIteration:
            return fn("tok")

    posted: list = []
    monkeypatch.setattr(rerun, "with_install_token_retry", _fake_token_retry)
    monkeypatch.setattr(
        rerun, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(
        rerun, "get_repo_config",
        lambda iid, rid: {"code_reviewer_blocking": True},
    )

    assert rerun._run_one(_job(requested_head_sha="old-head")) == "superseded_at_entry"
    assert posted[0].conclusion == "neutral"


# --- the dead-letter announcement ------------------------------------------

def _dlq_message() -> dict:
    body = {
        "kind": "review", "persona": "elder", "install_id": 11,
        "repo": "myorg/myrepo", "pr_number": 7,
        "requested_head_sha": "a" * 40, "schema_version": 1,
    }
    return {
        "MessageId": "m-1", "ReceiptHandle": "r-1", "Body": json.dumps(body),
        "Attributes": {"ApproximateReceiveCount": "5"},
    }


@pytest.mark.parametrize(
    "blocking, want",
    [(True, "failure"), (False, "neutral"), (None, "failure")],
    ids=["block", "warn", "mode_unreadable"],
)
def test_dead_letter_announcement_honours_the_mode(monkeypatch, blocking, want):
    """The job exhausted its retries: no verdict exists for this commit, so
    in BLOCK mode (or when the mode cannot be read) the check fails."""
    import consumer
    import rerun

    seen: dict = {}
    monkeypatch.setattr(rerun, "elder_blocking_for", lambda *a: blocking)
    monkeypatch.setattr(
        rerun, "_complete_elder_check_open",
        lambda **kw: seen.update(kw) or True,
    )

    consumer._announce_terminal_failure(
        types.SimpleNamespace(kind="rerun"), _dlq_message(), RuntimeError("x"),
    )

    assert seen["conclusion"] == want
    assert seen["summary"].split("\n")[0] == _lead("retries_exhausted")
