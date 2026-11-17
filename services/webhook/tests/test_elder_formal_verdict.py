"""#1758: Elder posts its verdict as a formal GitHub review.

Merge gates read GitHub review state, so on every head Elder reviews the
Tier-1 pass posts one formal review whose state is the verdict: APPROVE
when the head is clean, REQUEST_CHANGES when a blocking finding stands,
COMMENT only when the run is degraded or superseded. The states do not
depend on `code_reviewer_blocking`, which still picks only the check-run
conclusion. A degraded run never approves.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from github_reviews_client import ReviewResult
from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas import review_publish
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.carry_forward import CarriedFinding
from personas.code_reviewer.persona import CodeReviewEvaluation
from personas.review_publish import verdict_event

_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""
_HEAD = "abcd1234efgh"
_ELDER_TAG = "<!-- grug:review persona=elder -->"
_BEFORE_RUN = "2000-01-01T00:00:00Z"


def _payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {
            "number": 7,
            "head": {"sha": _HEAD},
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


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry", lambda inst_id, fn: fn("fake-token"),
    )
    monkeypatch.setattr(
        review_publish, "with_install_token_retry", lambda inst_id, fn: fn("fake-token"),
    )


def _clean() -> LlmReviewResponse:
    return LlmReviewResponse(kind="reviewed", findings=(), backend_used=Backend.POOLSIDE)


def _with(severity: str) -> LlmReviewResponse:
    return LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/x.py", line=2, rule="silent-failure",
            severity=severity, message="m",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.POOLSIDE,
    )


def _review(rid, *, state, commit="old0000", submitted_at=None):
    return {"id": rid, "state": state, "commit_id": commit, "body": _ELDER_TAG,
            "user": {"login": "grug-tribe[bot]"}, "submitted_at": submitted_at}


def _run(monkeypatch, llm, *, blocking, action="opened", threads=(),
         carried=(), reviews=(), author="alice"):
    """Drive one Tier-1 review; return (posted reviews, dismissed ids)."""
    posted: list[ReviewResult] = []
    dismissed: list[int] = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, result, **kw: posted.append(result) or {"id": 500 + len(posted)},
    )
    monkeypatch.setattr(cr_dispatch, "list_reviews", lambda *a, **kw: list(reviews))
    monkeypatch.setattr(
        cr_dispatch, "dismiss_review",
        lambda *a, **kw: dismissed.append(kw["review_id"]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch.carry_forward, "list_threads",
        lambda *a, **kw: None if threads is None else list(threads),
    )
    monkeypatch.setattr(
        cr_dispatch.carry_forward, "select_carried", lambda *a, **kw: tuple(carried),
    )
    payload = _payload(action)
    payload["pull_request"]["user"]["login"] = author
    with patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(payload, blocking=blocking)
    return posted, dismissed


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_clean_review_posts_one_approve_review(monkeypatch, blocking):
    posted, _ = _run(monkeypatch, _clean(), blocking=blocking)
    assert [r.event for r in posted] == ["APPROVE"]
    assert posted[0].comments == ()
    assert posted[0].commit_id == _HEAD
    assert _ELDER_TAG in posted[0].body


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_clean_review_with_advisory_findings_approves(monkeypatch, blocking):
    """A medium finding is advice, not a block: the inline review carries it
    and still approves."""
    posted, _ = _run(monkeypatch, _with("medium"), blocking=blocking)
    assert [r.event for r in posted] == ["APPROVE"]
    assert len(posted[0].comments) == 1


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_standing_high_finding_requests_changes(monkeypatch, blocking):
    posted, _ = _run(monkeypatch, _with("high"), blocking=blocking)
    assert [r.event for r in posted] == ["REQUEST_CHANGES"]


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_carried_blocking_finding_requests_changes(monkeypatch, blocking):
    """A clean re-review with an earlier high finding still open requests
    changes, with no new inline comment to carry the state."""
    carried = (CarriedFinding(
        path="src/x.py", line=9, rule="sql-injection", severity="high",
        url="https://github.com/myorg/myrepo/pull/7#discussion_r1",
    ),)
    posted, _ = _run(
        monkeypatch, _clean(), blocking=blocking, action="synchronize",
        carried=carried,
    )
    assert [r.event for r in posted] == ["REQUEST_CHANGES"]
    assert posted[0].comments == ()
    assert _ELDER_TAG in posted[0].body


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
@pytest.mark.parametrize(
    "llm",
    [
        LlmReviewResponse(kind="all_failed", error="poolside: timeout"),
        LlmReviewResponse(kind="parse_failed", error="bad json"),
    ],
    ids=["all_failed", "parse_failed"],
)
def test_degraded_review_never_approves(monkeypatch, blocking, llm):
    posted, _ = _run(monkeypatch, llm, blocking=blocking)
    assert all(r.event == "COMMENT" for r in posted)


def test_unknown_carry_forward_never_approves(monkeypatch):
    """No thread listing: the clean pass cannot vouch for earlier findings."""
    posted, _ = _run(
        monkeypatch, _clean(), blocking=False, action="synchronize", threads=None,
    )
    assert all(r.event != "APPROVE" for r in posted)


def test_own_pull_request_gets_no_verdict_state(monkeypatch):
    """GitHub refuses a verdict review from a pull request's author, so a PR
    the Grug App opened gets no APPROVE or REQUEST_CHANGES."""
    posted, _ = _run(monkeypatch, _clean(), blocking=True, author="grug-tribe[bot]")
    assert all(r.event == "COMMENT" for r in posted)


def test_clean_advisory_rereview_withdraws_elders_stale_block(monkeypatch):
    """The verdict, not the check conclusion, withdraws an older block: an
    advisory-mode clean review approves and dismisses Elder's own block."""
    reviews = [_review(1, state="CHANGES_REQUESTED", commit=_HEAD, submitted_at=_BEFORE_RUN)]
    posted, dismissed = _run(
        monkeypatch, _clean(), blocking=False, action="synchronize", reviews=reviews,
    )
    assert [r.event for r in posted] == ["APPROVE"]
    assert dismissed == [1]


def test_degraded_rereview_withdraws_elders_older_approval(monkeypatch):
    """Fail closed: Elder's newest word on the head is "could not review",
    so an approval it gave earlier no longer stands."""
    reviews = [
        _review(1, state="APPROVED", commit="old0000"),
        _review(2, state="CHANGES_REQUESTED", commit="old0000"),
    ]
    _, dismissed = _run(
        monkeypatch, LlmReviewResponse(kind="all_failed", error="poolside: timeout"),
        blocking=True, action="synchronize", reviews=reviews,
    )
    assert dismissed == [1]


def test_clean_rereview_keeps_elders_older_approval(monkeypatch):
    reviews = [_review(1, state="APPROVED", commit="old0000")]
    _, dismissed = _run(
        monkeypatch, _clean(), blocking=True, action="synchronize", reviews=reviews,
    )
    assert dismissed == []


# --- the pure verdict rule ----------------------------------------------------


def _evaluation(conclusion, degraded_reason=None) -> CodeReviewEvaluation:
    return CodeReviewEvaluation(
        findings=(), conclusion=conclusion, degraded_reason=degraded_reason,
    )


@pytest.mark.parametrize(
    ("evaluation", "kwargs", "event"),
    [
        (_evaluation("success"), {"conclusion": "neutral"}, "APPROVE"),
        (_evaluation("success"), {"conclusion": "success"}, "APPROVE"),
        (_evaluation("success"), {"conclusion": "neutral", "vouched": False}, "COMMENT"),
        (_evaluation("success"), {"conclusion": "failure"}, "REQUEST_CHANGES"),
        (_evaluation("success"), {"conclusion": "neutral", "carried_blocking": True},
         "REQUEST_CHANGES"),
        (_evaluation("failure"), {"conclusion": "neutral"}, "REQUEST_CHANGES"),
        (_evaluation("failure", "partial_review"), {"conclusion": "failure"}, "COMMENT"),
        (_evaluation("neutral", "all_failed"), {"conclusion": "failure"}, "COMMENT"),
        (_evaluation("neutral", "no_diff"), {"conclusion": "neutral"}, "COMMENT"),
        (_evaluation("success"), {"conclusion": "success", "superseded": True}, "COMMENT"),
        (_evaluation("failure"), {"conclusion": "failure", "superseded": True}, "COMMENT"),
    ],
)
def test_verdict_event(evaluation, kwargs, event):
    assert verdict_event(evaluation, **kwargs) == event
