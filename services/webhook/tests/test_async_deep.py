"""Async deep append after Tier-1 publish (#646)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.markings import review_run_started, run_marker
from review_pipeline import ReviewCoverage


_DIFF = """diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
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
        "repository": {
            "id": 22,
            "name": "myrepo",
            "owner": {"login": "myorg"},
        },
        "pull_request": {
            "number": 7,
            "head": {"sha": "abcd1234efgh"},
            "base": {"sha": "base5678ijkl"},
            "title": "auth change",
            "body": "deep-review please",
            "user": {"login": "alice"},
        },
    }


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "tiered")
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "1")
    monkeypatch.setenv("GRUG_DEEP_SAMPLE_RATE", "0")
    monkeypatch.setenv("GRUG_DEEP_DIFF_LINES", "1")


def _diff_response(diff: str = _DIFF):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = diff
    return r


def test_async_deep_appends_after_tier1_publish(monkeypatch):
    """Required path publishes first; reasoner appends when escalation fires."""
    tier1 = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/auth.py", line=2, rule="silent-failure",
            severity="medium", message="tier1",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.CAVE,
        model_name="coder",
        backends_used=(Backend.CAVE,),
        models_used=("coder",),
    )
    deep = LlmReviewResponse(
        kind="reviewed",
        findings=(LlmFinding(
            path="src/auth.py", line=3, rule="null-deref",
            severity="high", message="deep",  # type: ignore[arg-type]
        ),),
        backend_used=Backend.CAVE_REASONER,
        model_name="reasoner",
        backends_used=(Backend.CAVE_REASONER,),
        models_used=("reasoner",),
    )
    posted_checks: list = []
    posted_reviews: list = []
    call_order: list[str] = []

    def _fake_review_diff(*a, **kw):
        call_order.append("tier1")
        return tier1

    def _fake_reasoner(*a, **kw):
        call_order.append("deep")
        return deep

    def _fake_post_check(install_token, owner, repo, result, external_id=None):
        posted_checks.append({
            "title": result.title,
            "external_id": external_id,
            "conclusion": result.conclusion,
        })
        call_order.append("check")
        return {"id": len(posted_checks)}

    def _fake_post_review(install_token, owner, repo, *, pull_number, result):
        posted_reviews.append(result)
        call_order.append("review")
        return {"id": len(posted_reviews)}

    monkeypatch.setattr(cr_dispatch, "review_diff", _fake_review_diff)
    monkeypatch.setattr(cr_dispatch, "review_reasoner_diff", _fake_reasoner)
    monkeypatch.setattr(cr_dispatch, "post_check_run", _fake_post_check)
    monkeypatch.setattr(cr_dispatch, "post_review", _fake_post_review)
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch(
        "adapters.install_store.put_elder_last_reviewed",
        lambda **k: None,
    ), patch(
        "adapters.install_store.get_elder_last_reviewed",
        return_value=None,
    ):
        with patch("httpx.get", return_value=_diff_response()):
            out = cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert out["result"] == "pass"
    # Tier-1 check before deep arm; deep check after.
    assert call_order.index("tier1") < call_order.index("check")
    assert call_order.index("check") < call_order.index("deep")
    assert any("deep" in (c.get("title") or "") for c in posted_checks)
    assert any(
        (c.get("external_id") or "").startswith("grug-cr-deep:")
        for c in posted_checks
    )
    # Two reviews: tier1 findings + deep findings (different lines).
    assert len(posted_reviews) >= 2


def test_async_deep_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "0")
    tier1 = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE, model_name="coder",
        backends_used=(Backend.CAVE,), models_used=("coder",),
    )
    reasoner_calls = []

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: tier1)
    monkeypatch.setattr(
        cr_dispatch, "review_reasoner_diff",
        lambda *a, **kw: reasoner_calls.append(1) or tier1,
    )
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **k: {"id": 1},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **k: {"id": 2},
    )
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None), \
         patch("adapters.install_store.get_elder_last_reviewed", return_value=None), \
         patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert reasoner_calls == []


def test_async_deep_skipped_after_staged_tier1(monkeypatch):
    tier1 = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE, model_name="coder",
        backends_used=(Backend.CAVE,), models_used=("coder",),
    )
    reasoner_calls = []

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: tier1)
    monkeypatch.setattr(cr_dispatch, "review_is_staged", lambda hunks: True)
    monkeypatch.setattr(
        cr_dispatch, "review_reasoner_diff",
        lambda *a, **kw: reasoner_calls.append(1) or tier1,
    )
    monkeypatch.setattr(
        cr_dispatch, "post_check_run", lambda *a, **k: {"id": 1},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review", lambda *a, **k: {"id": 2},
    )
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None), \
         patch("adapters.install_store.get_elder_last_reviewed", return_value=None), \
         patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=False)

    assert reasoner_calls == []


def test_async_deep_partial_coverage_never_publishes_success(monkeypatch):
    """Reproduces the PR #127 shape (grug#813/#707): Tier-1 is clean, but
    the deep (reasoner) arm exceeds ITS OWN cohort budget and comes back
    `partial_review` with no novel findings (everything it did see was
    already covered by Tier-1). Before the fix, the merge that builds
    `combined` for the deep check-run only ever looked at Tier-1's
    `degraded_reason` - the deep arm's own partial coverage was silently
    dropped, and the deep check-run published `success` over ground the
    deep arm never finished walking. This is the exact mismatch reported
    live: a board comment saying "some ground not walked" next to a
    check-run conclusion of `success`.

    MUST fail on main (deep check conclusion == "success") and pass once
    `with_degradation` folds the deep arm's coverage into `combined`.
    """
    tier1 = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE, model_name="coder",
        backends_used=(Backend.CAVE,), models_used=("coder",),
    )
    deep = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE_REASONER, model_name="reasoner",
        backends_used=(Backend.CAVE_REASONER,), models_used=("reasoner",),
        # The deep arm's own cohort plan ran out of budget - #813's shape,
        # measured on byte-identical copies eating the budget before the
        # novel logic. `findings=()` means nothing NEW survives dedup
        # against Tier-1, which is exactly the case that used to erase the
        # degradation entirely.
        error="partial review: cohorts [2] failed",
        coverage=ReviewCoverage(
            total_cohorts=2, completed_cohorts=1, failed_cohorts=(2,),
            cohort_labels=("a", "b"),
        ),
    )
    posted_checks: list = []

    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: tier1)
    monkeypatch.setattr(cr_dispatch, "review_reasoner_diff", lambda *a, **kw: deep)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted_checks.append(
                {"external_id": external_id, "conclusion": result.conclusion}
            )
            or {"id": len(posted_checks)}
        ),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None), \
         patch("adapters.install_store.get_elder_last_reviewed", return_value=None), \
         patch("httpx.get", return_value=_diff_response()):
        # `blocking=True` -> mode="blocking": `_publish_shape` only forces
        # `neutral` from `evaluation.degraded_reason`, not from mode itself.
        # advisory mode (`blocking=False`) forces `neutral` unconditionally
        # and would pass this test even with the bug present - it has to be
        # blocking mode to actually exercise the coverage-honesty check.
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    deep_checks = [
        c for c in posted_checks
        if (c["external_id"] or "").startswith("grug-cr-deep:")
    ]
    assert deep_checks, "deep check-run was never posted"
    assert deep_checks[-1]["conclusion"] != "success", (
        "deep arm hit partial coverage but its check-run reported success - "
        "coverage honesty regression"
    )


def _clean_tier1() -> LlmReviewResponse:
    return LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE, model_name="coder",
        backends_used=(Backend.CAVE,), models_used=("coder",),
    )


def _run_escalated_review(
    monkeypatch, *, deep: LlmReviewResponse, blocking: bool = True,
    checks_on_head: list[dict] | None = None,
    posted_reviews: list | None = None,
    on_reasoner=None,
    tier1=None,
) -> list[dict]:
    """Drive one escalated tiered review (clean Tier-1, `deep` from the
    reasoner) in blocking mode (unless told otherwise) and return every
    posted check-run.

    `blocking=True` on purpose: advisory mode forces `neutral` unconditionally
    and would hide a Tier-1 `success` left standing by a failed deep arm."""
    posted_checks: list[dict] = []

    def _fake_post_check(token, owner, repo, result, external_id=None):
        posted_checks.append({
            "external_id": external_id,
            "conclusion": result.conclusion,
            "title": result.title,
            "summary": result.summary,
        })
        return {"id": len(posted_checks)}

    monkeypatch.setattr(
        cr_dispatch, "review_diff", tier1 or (lambda *a, **kw: _clean_tier1()),
    )
    def _fake_reasoner(*a, **kw):
        if on_reasoner is not None:
            on_reasoner()
        return deep

    monkeypatch.setattr(cr_dispatch, "review_reasoner_diff", _fake_reasoner)
    monkeypatch.setattr(cr_dispatch, "post_check_run", _fake_post_check)

    def _fake_post_review(*a, result, **k):
        if posted_reviews is not None:
            posted_reviews.append(result)
        return {"id": 1}

    monkeypatch.setattr(cr_dispatch, "post_review", _fake_post_review)
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    if checks_on_head is not None:
        monkeypatch.setattr(
            cr_dispatch, "list_check_runs_for_ref",
            lambda *a, **k: checks_on_head,
        )
    with patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None), \
         patch("adapters.install_store.get_elder_last_reviewed", return_value=None), \
         patch("httpx.get", return_value=_diff_response()):
        cr_dispatch.dispatch_code_review(_payload(), blocking=blocking)
    return posted_checks


def _deep_checks(posted_checks: list[dict]) -> list[dict]:
    return [
        c for c in posted_checks
        if (c["external_id"] or "").startswith("grug-cr-deep:")
    ]


@pytest.mark.parametrize(
    "failed_deep",
    [
        LlmReviewResponse(
            kind="all_failed", error="cave-reasoner: ConnectError",
            backend_used=Backend.CAVE_REASONER,
        ),
        LlmReviewResponse(
            kind="parse_failed", error="content was prose, not JSON",
            backend_used=Backend.CAVE_REASONER, model_name="reasoner",
        ),
    ],
    ids=["all_failed", "parse_failed"],
)
def test_async_deep_arm_failure_never_leaves_tier1_success_unqualified(
    monkeypatch, caplog, failed_deep,
):
    """grug#848: Tier-1 is clean and publishes `success` with the promise
    "reasoner may append later if escalated". Escalation fires, and the
    reasoner arm then fails outright (transport error, or a reply that
    never parsed). Before the fix `_async_deep_append_if_needed` logged
    `elder_async_deep_arm_empty` and returned - no check-run, no note - so
    the Tier-1 `success` stood unqualified and the author could not tell
    "the reasoner looked and agreed" from "the reasoner never looked".

    MUST fail on main (no `grug-cr-deep:` check-run is ever posted) and pass
    once the failed arm folds into Tier-1 through `with_degradation` /
    `_derive_conclusion` and completes the deep check-run degraded - in
    BLOCK mode that is `failure` (grug#1212): the escalated second look
    never happened, so the required check must not pass."""
    with caplog.at_level("INFO"):
        posted = _run_escalated_review(monkeypatch, deep=failed_deep)

    # The branch is exercised (issue acceptance: the log gains a test) ...
    empty = [r for r in caplog.records if r.message == "elder_async_deep_arm_empty"]
    assert len(empty) == 1
    assert empty[0].kind == failed_deep.kind  # type: ignore[attr-defined]
    # ... and it is no longer silent.
    deep = _deep_checks(posted)
    assert deep, (
        "deep arm failed outright but no deep check-run was posted - the "
        "Tier-1 success stands unqualified"
    )
    assert deep[-1]["conclusion"] == "failure"
    assert deep[-1]["summary"].split("\n")[0] == (
        f"Degraded review ({failed_deep.kind}): not a verdict. "
        "Comment `/grug improve` to re-run once the backend recovers."
    )
    assert "did not run" in deep[-1]["title"]
    assert failed_deep.kind in deep[-1]["title"]
    # The Tier-1 verdict is not disowned: this is "the second look never
    # happened", not "Grug could not see the diff".
    assert "could not see the diff" not in deep[-1]["summary"]
    assert "did not run" in deep[-1]["summary"]
    # Raw backend/parse error text never reaches the author-facing surface
    # (a parse failure's text can be model prose) - the log carries it.
    assert failed_deep.error not in deep[-1]["summary"]


def test_async_deep_arm_failure_stays_neutral_in_warn_mode(monkeypatch):
    """WARN mode is unchanged by grug#1212: the failed deep arm completes
    its check `neutral`, still led by the degraded line."""
    posted = _run_escalated_review(
        monkeypatch,
        deep=LlmReviewResponse(
            kind="all_failed", error="cave-reasoner: ConnectError",
            backend_used=Backend.CAVE_REASONER,
        ),
        blocking=False,
    )

    deep = _deep_checks(posted)
    assert deep, "deep check-run was never posted"
    assert deep[-1]["conclusion"] == "neutral"
    assert deep[-1]["summary"].startswith("Degraded review (all_failed):")


def test_async_deep_arm_ran_clean_still_publishes_success(monkeypatch):
    """The other half of the #848 distinction: a reasoner that RAN and found
    nothing is a legitimately clean deep pass. The failure handling must not
    turn every escalated-but-clean review into a neutral - `success`, with a
    summary that says the arm ran, not that it was skipped."""
    ran_clean = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE_REASONER, model_name="reasoner",
        backends_used=(Backend.CAVE_REASONER,), models_used=("reasoner",),
    )

    posted = _run_escalated_review(monkeypatch, deep=ran_clean)

    deep = _deep_checks(posted)
    assert deep, "deep check-run was never posted"
    assert deep[-1]["conclusion"] == "success"
    assert "did not run" not in deep[-1]["title"]
    assert "did not run" not in deep[-1]["summary"]
    assert "appended after Tier-1 completed" in deep[-1]["summary"]


def test_async_deep_clean_advisory_pass_says_why_it_is_neutral(monkeypatch):
    """grug#1753: the deep append publishes its own Elder check, so a clean
    advisory deep pass must name advisory mode just as Tier-1 does, or the
    latest check reads "Elder clear" with a bare neutral."""
    ran_clean = LlmReviewResponse(
        kind="reviewed", findings=(),
        backend_used=Backend.CAVE_REASONER, model_name="reasoner",
        backends_used=(Backend.CAVE_REASONER,), models_used=("reasoner",),
    )

    posted = _run_escalated_review(monkeypatch, deep=ran_clean, blocking=False)

    deep = _deep_checks(posted)
    assert deep, "deep check-run was never posted"
    assert deep[-1]["conclusion"] == "neutral"
    assert "Neutral because" in deep[-1]["title"]
    assert "advisory mode" in deep[-1]["title"]
    neutral_lines = [line for line in deep[-1]["summary"].splitlines()
                     if line.startswith("Neutral because ")]
    assert len(neutral_lines) == 1
    assert "advisory mode" in neutral_lines[0]


def test_async_deep_arm_no_diff_is_not_a_failure(monkeypatch, caplog):
    """`no_diff` from the reasoner means there was nothing for it to look at
    (BENIGN_DEGRADATIONS) - not a failure to look. It must not manufacture a
    degraded deep check-run over a clean Tier-1."""
    with caplog.at_level("INFO"):
        posted = _run_escalated_review(
            monkeypatch, deep=LlmReviewResponse(kind="no_diff"),
        )

    assert [r for r in caplog.records if r.message == "elder_async_deep_arm_empty"]
    assert _deep_checks(posted) == []


_HIGH_DEEP = LlmReviewResponse(
    kind="reviewed",
    findings=(LlmFinding(
        path="src/auth.py", line=3, rule="null-deref",
        severity="high", message="deep",  # type: ignore[arg-type]
    ),),
    backend_used=Backend.CAVE_REASONER, model_name="reasoner",
    backends_used=(Backend.CAVE_REASONER,), models_used=("reasoner",),
)


_APP_ID = "4242"


def _elder_run(run_id: int, *, app_id: str = _APP_ID, status: str = "completed",
               conclusion: str | None = "success", started_at: str | None = None) -> dict:
    return {"id": run_id, "name": "Grug - Elder", "app": {"id": int(app_id)},
            "status": status, "conclusion": conclusion, "started_at": started_at}


@pytest.fixture
def _own_app(monkeypatch):
    monkeypatch.setattr(cr_dispatch, "get_app_id", lambda: _APP_ID)


def test_superseded_review_deep_append_never_replaces_a_newer_verdict(
    monkeypatch, _own_app,
):
    """#1344: a newer review of the same head already posted the newest
    `Grug - Elder` verdict (check id 99, after this run's Tier-1 id 1, and
    an approval from a run that started after this one). This run's late
    deep append must not land on top of it: no deep check run and no
    REQUEST_CHANGES. Its high finding still posts as a COMMENT review, so
    the open thread carries into the next review's verdict."""
    reviews: list = []
    newer = _marked(98, state="APPROVED", started="2999-01-01T00:00:00+00:00")
    monkeypatch.setattr(cr_dispatch, "list_reviews", lambda *a, **k: [newer])
    posted = _run_escalated_review(
        monkeypatch, deep=_HIGH_DEEP,
        checks_on_head=[_elder_run(99)],
        posted_reviews=reviews,
    )

    assert _deep_checks(posted) == []
    assert not [r for r in reviews if r.event == "REQUEST_CHANGES"]
    deep_reviews = [
        r for r in reviews if any(c.line == 3 for c in r.comments)
    ]
    assert deep_reviews and deep_reviews[-1].event == "COMMENT"


@pytest.mark.parametrize(
    "newer",
    [
        _elder_run(99, app_id="777"),
        _elder_run(99, status="in_progress", conclusion=None),
        _elder_run(99, conclusion="neutral"),
    ],
    ids=["other_app", "in_progress", "neutral"],
)
def test_a_newer_run_that_is_not_our_verdict_does_not_supersede(
    monkeypatch, _own_app, newer,
):
    """#1344: only a completed success/failure run this app created counts.
    A run another app named `Grug - Elder`, an in-progress marker, and a
    neutral run decided nothing, so the deep append publishes as before."""
    reviews: list = []
    posted = _run_escalated_review(
        monkeypatch, deep=_HIGH_DEEP, checks_on_head=[newer],
        posted_reviews=reviews,
    )

    deep = _deep_checks(posted)
    assert deep, "deep check-run was never posted"
    assert deep[-1]["conclusion"] == "failure"
    assert [r for r in reviews if r.event == "REQUEST_CHANGES"]


def test_latest_review_deep_append_still_updates_the_check(monkeypatch, _own_app):
    """#1344: when this run's own Tier-1 check run is still the newest Elder
    run on the head, the deep append publishes as before."""
    posted = _run_escalated_review(
        monkeypatch, deep=_HIGH_DEEP,
        checks_on_head=[
            _elder_run(1),
            {"id": 50, "name": "Grug - Chief", "app": {"id": int(_APP_ID)},
             "status": "completed", "conclusion": "success"},
        ],
    )

    deep = _deep_checks(posted)
    assert deep, "deep check-run was never posted"
    assert deep[-1]["conclusion"] == "failure"


def test_deep_append_publishes_when_the_supersession_lookup_fails(
    monkeypatch, _own_app,
):
    """#1344: a failed check-run listing cannot prove a newer verdict, so the
    deep append keeps its old behavior rather than silently vanishing."""
    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(cr_dispatch, "list_check_runs_for_ref", _boom)
    posted = _run_escalated_review(monkeypatch, deep=_HIGH_DEEP)

    assert _deep_checks(posted)


# --- #1758: the deep append owns the verdict on an escalated head -----------

_RAN_CLEAN = LlmReviewResponse(
    kind="reviewed", findings=(),
    backend_used=Backend.CAVE_REASONER, model_name="reasoner",
    backends_used=(Backend.CAVE_REASONER,), models_used=("reasoner",),
)
_DEEP_DOWN = LlmReviewResponse(
    kind="all_failed", error="cave-reasoner: ConnectError",
    backend_used=Backend.CAVE_REASONER,
)


def _elder_review(rid, *, state, commit="abcd1234efgh", submitted_at=None):
    return {"id": rid, "state": state, "commit_id": commit,
            "body": "<!-- grug:review persona=elder -->",
            "user": {"login": "grug-tribe[bot]"}, "submitted_at": submitted_at}


def _run_with_verdicts(monkeypatch, *, deep, reviews_on_pr=(), blocking=True,
                       checks_on_head=None, posted=None, tier1=None):
    """An escalated review that records every posted review, every
    dismissal, and the reviews already posted when the reasoner started.

    By default the latest Elder check on the head is this run's own Tier-1
    check (id 1), so no newer run has started."""
    reviews: list = [] if posted is None else posted
    dismissed: list[int] = []
    before_deep: list = []
    monkeypatch.setattr(cr_dispatch, "get_app_id", lambda: _APP_ID)
    monkeypatch.setattr(cr_dispatch, "list_reviews", lambda *a, **k: list(reviews_on_pr))
    monkeypatch.setattr(
        cr_dispatch, "dismiss_review",
        lambda *a, **kw: dismissed.append(kw["review_id"]) or {},
    )
    _run_escalated_review(
        monkeypatch, deep=deep, posted_reviews=reviews, blocking=blocking,
        on_reasoner=lambda: before_deep.extend(r.event for r in reviews),
        tier1=tier1,
        checks_on_head=(
            [_elder_run(1, started_at="2999-01-01T00:00:00Z")]
            if checks_on_head is None else checks_on_head
        ),
    )
    return [r.event for r in reviews], dismissed, before_deep


def _marked(rid, *, state, started, submitted_at=None, commit="abcd1234efgh"):
    """An Elder review carrying the run marker of a run that started at
    `started` (#1764: recency is the run start, not post time)."""
    review = _elder_review(rid, state=state, commit=commit, submitted_at=submitted_at)
    review["body"] += "\n" + run_marker(datetime.fromisoformat(started))
    return review


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_escalated_head_is_approved_only_after_the_deep_pass(monkeypatch, blocking):
    """Tier-1 is clean but a deep pass is scheduled: no APPROVE may stand
    while the reasoner is still looking, or a gate could merge in between.
    The deep append posts the approval once it finishes clean."""
    events, _, before_deep = _run_with_verdicts(
        monkeypatch, deep=_RAN_CLEAN, blocking=blocking,
    )
    assert "APPROVE" not in before_deep
    assert events.count("APPROVE") == 1
    assert events[-1] == "APPROVE"


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_deep_arm_failure_leaves_the_head_unapproved(monkeypatch, blocking):
    """Fail closed: the reasoner never ran, so nobody approves the head."""
    events, _, _ = _run_with_verdicts(monkeypatch, deep=_DEEP_DOWN, blocking=blocking)
    assert "APPROVE" not in events


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_deep_blocking_finding_requests_changes_with_no_approval(monkeypatch, blocking):
    events, _, _ = _run_with_verdicts(monkeypatch, deep=_HIGH_DEEP, blocking=blocking)
    assert "APPROVE" not in events
    assert events[-1] == "REQUEST_CHANGES"


def test_deferred_approval_withdraws_elders_older_block(monkeypatch):
    """The clean deep pass is the approving run, so it withdraws Elder's own
    block on this head from an earlier-started run, as a clean Tier-1 would.
    A block on another commit stays (#1781): it may come from a newer push."""
    block = _marked(5, state="CHANGES_REQUESTED", started="2000-01-01T00:00:00+00:00")
    other = _elder_review(6, state="CHANGES_REQUESTED", commit="old0000")
    events, dismissed, _ = _run_with_verdicts(
        monkeypatch, deep=_RAN_CLEAN, reviews_on_pr=[block, other],
    )
    assert events[-1] == "APPROVE"
    assert dismissed == [5]


@pytest.mark.parametrize("deep", [_RAN_CLEAN, _HIGH_DEEP], ids=["clean", "high"])
def test_newer_verdict_review_supersedes_the_deep_append(monkeypatch, deep):
    """Advisory checks conclude neutral, so the check list cannot show a
    newer run. An Elder verdict review from a run that STARTED after this
    one does: the deep append neither approves nor blocks."""
    newer = _marked(9, state="APPROVED", started="2999-01-01T00:00:00+00:00",
                    submitted_at="2999-01-01T00:00:05Z")
    events, dismissed, _ = _run_with_verdicts(
        monkeypatch, deep=deep, reviews_on_pr=[newer], blocking=False,
    )
    assert "APPROVE" not in events and "REQUEST_CHANGES" not in events
    assert dismissed == []


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_earlier_run_approval_posted_late_never_silences_a_block(monkeypatch, blocking):
    """Runs A and B on one head; A started first. A's deferred APPROVE lands
    after B started, so by post time it looks newer. B's deep pass finds a
    blocking problem: B must request changes and dismiss A's stale approval,
    not stand down and leave the approval standing."""
    stale = _marked(7, state="APPROVED", started="2000-01-01T00:00:00+00:00",
                    submitted_at="2999-01-01T00:00:00Z")
    # A's own deep check also landed after B's Tier-1 check (higher id).
    a_check = _elder_run(99, started_at="2999-01-01T00:00:00Z")
    events, dismissed, _ = _run_with_verdicts(
        monkeypatch, deep=_HIGH_DEEP, reviews_on_pr=[stale], blocking=blocking,
        checks_on_head=[a_check],
    )
    assert events[-1] == "REQUEST_CHANGES"
    assert 7 in dismissed


def test_unmarked_approval_posted_after_the_run_started_is_dismissed_by_a_block(
    monkeypatch,
):
    """A same-head Elder approval with no run marker cannot prove it came
    from a newer run, so a blocking deep pass dismisses it."""
    legacy = _elder_review(7, state="APPROVED", submitted_at="2999-01-01T00:00:00Z")
    events, dismissed, _ = _run_with_verdicts(
        monkeypatch, deep=_HIGH_DEEP, reviews_on_pr=[legacy],
    )
    assert events[-1] == "REQUEST_CHANGES"
    assert dismissed == [7]


def test_deferred_approval_stands_down_when_a_newer_run_has_started(monkeypatch):
    """Right before the deferred APPROVE, the latest Elder check on the head
    belongs to another run that started after this one (still in progress,
    so it decided nothing yet): no approval."""
    newer_check = _elder_run(
        50, status="in_progress", conclusion=None, started_at="2999-01-01T00:00:00Z",
    )
    events, _, _ = _run_with_verdicts(
        monkeypatch, deep=_RAN_CLEAN, checks_on_head=[newer_check],
    )
    assert "APPROVE" not in events


def test_deferred_approval_stands_down_when_the_check_list_fails(monkeypatch):
    """Fail closed: no proof that no newer run started, no approval."""
    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    reviews: list = []
    monkeypatch.setattr(cr_dispatch, "get_app_id", lambda: _APP_ID)
    monkeypatch.setattr(cr_dispatch, "list_reviews", lambda *a, **k: [])
    monkeypatch.setattr(cr_dispatch, "dismiss_review", lambda *a, **kw: {})
    monkeypatch.setattr(cr_dispatch, "list_check_runs_for_ref", _boom)
    _run_escalated_review(monkeypatch, deep=_RAN_CLEAN, posted_reviews=reviews)
    assert "APPROVE" not in [r.event for r in reviews]


def test_every_verdict_review_carries_its_run_start(monkeypatch):
    """The run marker is what later runs order by, so each posted review
    carries one, and it parses back."""
    posted: list = []
    _run_with_verdicts(monkeypatch, deep=_RAN_CLEAN, posted=posted)
    assert posted and all(review_run_started(r.body) is not None for r in posted)


def _security_pass_down():
    """Tier-1 answers clean; the code-only security pass on the sensitive
    path (`src/auth.py`) does not finish."""
    calls: list = []

    def _review(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            return _clean_tier1()
        return LlmReviewResponse(kind="all_failed", error="down")

    return _review


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_unfinished_security_pass_leaves_an_escalated_head_unapproved(
    monkeypatch, blocking,
):
    """The deep pass finishes clean, but the security pass on the sensitive
    path did not: no approval, before or after the deep append."""
    monkeypatch.setenv("GRUG_QUARANTINED_SECURITY_PASS", "1")
    events, _, _ = _run_with_verdicts(
        monkeypatch, deep=_RAN_CLEAN, blocking=blocking, tier1=_security_pass_down(),
    )
    assert "APPROVE" not in events


def test_unfinished_security_pass_still_requests_changes_from_the_deep_pass(
    monkeypatch,
):
    monkeypatch.setenv("GRUG_QUARANTINED_SECURITY_PASS", "1")
    events, _, _ = _run_with_verdicts(
        monkeypatch, deep=_HIGH_DEEP, tier1=_security_pass_down(),
    )
    assert events[-1] == "REQUEST_CHANGES"


def test_deferred_approval_refuses_an_unfinished_security_pass(monkeypatch):
    """Defense in depth at the last seam: the deferred APPROVE is never
    posted for a run whose security pass did not finish."""
    posted: list = []
    monkeypatch.setattr(cr_dispatch, "get_app_id", lambda: _APP_ID)
    monkeypatch.setattr(cr_dispatch, "list_reviews", lambda *a, **k: [])
    monkeypatch.setattr(cr_dispatch, "list_check_runs_for_ref", lambda *a, **k: [])
    monkeypatch.setattr(
        cr_dispatch, "post_review", lambda *a, result, **k: posted.append(result) or {},
    )
    cr_dispatch._post_deferred_approval(
        installation_id=11, owner="myorg", repo_name="myrepo", pull_number=7,
        head_sha="abcd1234efgh",
        run_started_at=datetime.fromisoformat("2000-01-01T00:00:00+00:00"),
        own_check_ids=frozenset(), security_pass_complete=False,
    )
    assert posted == []


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_deferred_approval_stands_down_after_a_push(monkeypatch, blocking):
    """#1781: a push lands while the deep pass runs and a newer run blocks
    the new head. The older run's clean deep pass posts no approval on the
    old head and dismisses neither the newer block nor anything else."""
    monkeypatch.setattr(cr_dispatch, "_pr_head_state", lambda *a, **k: "moved")
    newer_block = _marked(8, state="CHANGES_REQUESTED", commit="new9999",
                          started="2999-01-01T00:00:00+00:00")
    events, dismissed, _ = _run_with_verdicts(
        monkeypatch, deep=_RAN_CLEAN, reviews_on_pr=[newer_block], blocking=blocking,
    )
    assert "APPROVE" not in events
    assert dismissed == []


def test_unconfirmed_head_never_gets_the_deferred_approval(monkeypatch):
    monkeypatch.setattr(cr_dispatch, "_pr_head_state", lambda *a, **k: "unknown")
    events, _, _ = _run_with_verdicts(monkeypatch, deep=_RAN_CLEAN)
    assert "APPROVE" not in events


@pytest.mark.parametrize("deep", [_DEEP_DOWN, _RAN_CLEAN], ids=["failed", "clean"])
def test_deep_append_end_withdraws_a_late_approval_on_an_earlier_commit(
    monkeypatch, deep,
):
    """#1781: an older run's approval on an earlier commit landed while this
    run's deep pass ran. However the deep append ends, it is withdrawn, and
    this run's own approval on its head (clean case) stays."""
    late = _marked(7, state="APPROVED", commit="old0000",
                   started="2000-01-01T00:00:00+00:00")
    events, dismissed, _ = _run_with_verdicts(
        monkeypatch, deep=deep, reviews_on_pr=[late],
    )
    assert dismissed == [7]
    assert ("APPROVE" in events) == (deep is _RAN_CLEAN)


