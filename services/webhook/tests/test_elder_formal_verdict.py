"""#1758: Elder posts its verdict as a formal GitHub review.

Merge gates read GitHub review state, so on every head Elder reviews the
Tier-1 pass posts one formal review whose state is the verdict: APPROVE
when the head is clean, REQUEST_CHANGES when a blocking finding stands,
COMMENT only when the run is degraded or superseded. The states do not
depend on `code_reviewer_blocking`, which still picks only the check-run
conclusion. A degraded run never approves.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from github_reviews_client import ReviewResult
from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas import review_publish
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.carry_forward import CarriedFinding
from personas.code_reviewer.markings import run_marker
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


def _diff_response(diff: str = _DIFF):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = diff
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
         carried=(), reviews=(), author="alice", head_state="current",
         rerun_pending=False, diff=_DIFF, checks=None, head=_HEAD):
    """Drive one Tier-1 review; return (posted reviews, dismissed ids).
    `checks`, when given, collects every posted check-run.

    `head_state` is what GitHub says about the head when Elder checks it
    (#1781): "moved" models a push that landed after the run started."""
    posted: list[ReviewResult] = []
    dismissed: list[int] = []
    monkeypatch.setattr(cr_dispatch, "_pr_head_state", lambda *a, **k: head_state)
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **kw: (checks.append(a[3]) if checks is not None else None) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, result, **kw: posted.append(result) or {"id": 500 + len(posted)},
    )
    monkeypatch.setattr(cr_dispatch, "list_reviews", lambda *a, **kw: list(reviews))

    def _dismiss(*a, review_id, **kw):
        # As on GitHub: a dismissed review lists as DISMISSED afterward.
        dismissed.append(review_id)
        for review in reviews:
            if review["id"] == review_id:
                review["state"] = "DISMISSED"
        return {}

    monkeypatch.setattr(cr_dispatch, "dismiss_review", _dismiss)
    monkeypatch.setattr(
        cr_dispatch.carry_forward, "list_threads",
        lambda *a, **kw: None if threads is None else list(threads),
    )
    monkeypatch.setattr(
        cr_dispatch.carry_forward, "select_carried", lambda *a, **kw: tuple(carried),
    )
    payload = _payload(action)
    payload["pull_request"]["user"]["login"] = author
    payload["pull_request"]["head"]["sha"] = head
    with patch("httpx.get", return_value=_diff_response(diff)):
        cr_dispatch.dispatch_code_review(
            payload, blocking=blocking, rerun_pending=rerun_pending,
        )
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
    """A fully degraded run has nothing to post inline and no verdict, so it
    posts no review at all (a COMMENT with nothing in it is never sent)."""
    posted, _ = _run(monkeypatch, llm, blocking=blocking)
    assert [r.event for r in posted] == []
    assert "APPROVE" not in [r.event for r in posted]


@pytest.mark.parametrize(
    ("llm", "expected"),
    [(_clean(), []), (_with("medium"), ["COMMENT"])],
    ids=["clean", "medium_finding"],
)
def test_unknown_carry_forward_never_approves(monkeypatch, llm, expected):
    """No thread listing: the clean pass cannot vouch for earlier findings.
    It posts its inline findings, if any, as exactly one COMMENT review."""
    posted, _ = _run(
        monkeypatch, llm, blocking=False, action="synchronize", threads=None,
    )
    assert [r.event for r in posted] == expected
    assert "APPROVE" not in [r.event for r in posted]


@pytest.mark.parametrize(
    ("llm", "expected"),
    [(_clean(), []), (_with("medium"), ["COMMENT"]), (_with("high"), ["COMMENT"])],
    ids=["clean", "medium_finding", "high_finding"],
)
def test_own_pull_request_gets_no_verdict_state(monkeypatch, llm, expected):
    """GitHub refuses a verdict review from a pull request's author, so a PR
    the Grug App opened gets no APPROVE or REQUEST_CHANGES: its findings,
    if any, ride exactly one COMMENT review."""
    posted, _ = _run(monkeypatch, llm, blocking=True, author="grug-tribe[bot]")
    events = [r.event for r in posted]
    assert events == expected
    assert "APPROVE" not in events and "REQUEST_CHANGES" not in events


def test_clean_advisory_rereview_withdraws_elders_stale_block(monkeypatch):
    """The verdict, not the check conclusion, withdraws an older block: an
    advisory-mode clean review approves and dismisses Elder's own block."""
    reviews = [_review(1, state="CHANGES_REQUESTED", commit=_HEAD, submitted_at=_BEFORE_RUN)]
    posted, dismissed = _run(
        monkeypatch, _clean(), blocking=False, action="synchronize", reviews=reviews,
    )
    assert [r.event for r in posted] == ["APPROVE"]
    assert dismissed == [1]


def test_clean_review_withdraws_a_same_head_block_from_an_earlier_started_run(monkeypatch):
    """Recency is the run start, not the post time: a block an earlier run
    posted after this run started is still that earlier run's verdict."""
    block = _review(1, state="CHANGES_REQUESTED", commit=_HEAD,
                    submitted_at="2999-01-01T00:00:00Z")
    block["body"] += "\n" + run_marker(datetime(2000, 1, 1, tzinfo=timezone.utc))
    _, dismissed = _run(
        monkeypatch, _clean(), blocking=True, action="synchronize", reviews=[block],
    )
    assert dismissed == [1]


def test_clean_review_keeps_a_same_head_block_from_a_later_started_run(monkeypatch):
    block = _review(1, state="CHANGES_REQUESTED", commit=_HEAD, submitted_at=_BEFORE_RUN)
    block["body"] += "\n" + run_marker(datetime(2999, 1, 1, tzinfo=timezone.utc))
    _, dismissed = _run(
        monkeypatch, _clean(), blocking=True, action="synchronize", reviews=[block],
    )
    assert dismissed == []


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


def test_rereview_withdraws_elders_approval_on_an_earlier_commit(monkeypatch):
    """#1781: the approval vouched for an older commit. A clean finish
    approves the new head again."""
    reviews = [_review(1, state="APPROVED", commit="old0000")]
    posted, dismissed = _run(
        monkeypatch, _clean(), blocking=True, action="synchronize", reviews=reviews,
    )
    assert dismissed == [1]
    assert [r.event for r in posted] == ["APPROVE"]


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_degraded_rerun_pending_withdraws_elders_older_approval(monkeypatch, blocking):
    """#1781: a run that exits early to wait for its re-run cannot vouch
    for the head either, so the earlier approval is withdrawn first."""
    reviews = [_review(1, state="APPROVED", commit="old0000")]
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **kw: {})
    _, dismissed = _run(
        monkeypatch, LlmReviewResponse(kind="all_failed", error="poolside: timeout"),
        blocking=blocking, action="synchronize", reviews=reviews, rerun_pending=True,
    )
    assert dismissed == [1]


def test_stale_rereview_withdraws_elders_older_approval(monkeypatch):
    """A queued re-review whose input went stale exits before any model
    call, after withdrawing the approval it can no longer stand behind."""
    reviews = [_review(1, state="APPROVED", commit="old0000")]
    monkeypatch.setattr(
        cr_dispatch, "_review_snapshot_freshness_failure",
        lambda **kw: {"persona": "code_reviewer", "result": "stale"},
    )
    posted, dismissed = _run(
        monkeypatch, _clean(), blocking=True, action="review", reviews=reviews,
    )
    assert posted == []
    assert dismissed == [1]


@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_push_after_the_run_started_leaves_the_newer_block_and_no_approval(
    monkeypatch, blocking,
):
    """#1781: run A reviews the old head; a push lands and run B blocks the
    new head. A finishes clean: it must not approve the old head, and must
    not dismiss B's block or any approval on the new head."""
    newer_block = _review(1, state="CHANGES_REQUESTED", commit="new9999")
    newer_block["body"] += "\n" + run_marker(datetime(2999, 1, 1, tzinfo=timezone.utc))
    posted, dismissed = _run(
        monkeypatch, _clean(), blocking=blocking, action="synchronize",
        reviews=[newer_block, _review(2, state="APPROVED", commit="new9999")],
        head_state="moved",
    )
    assert "APPROVE" not in [r.event for r in posted]
    assert dismissed == []


def test_unconfirmed_head_never_approves(monkeypatch):
    """Fail closed: the head lookup failed, so the approval is not posted."""
    posted, _ = _run(monkeypatch, _clean(), blocking=True, head_state="unknown")
    assert "APPROVE" not in [r.event for r in posted]


def test_approving_run_never_dismisses_a_block_on_another_commit(monkeypatch):
    """Only a same-head block from an earlier-started run is withdrawn; a
    block on another commit may belong to a newer push (#1781)."""
    reviews = [_review(1, state="CHANGES_REQUESTED", commit="other00")]
    posted, dismissed = _run(
        monkeypatch, _clean(), blocking=True, action="synchronize", reviews=reviews,
    )
    assert [r.event for r in posted] == ["APPROVE"]
    assert dismissed == []


# --- #1781: every exit withdraws approvals on other commits -----------------


def _old_approval() -> list[dict]:
    return [_review(1, state="APPROVED", commit="old0000")]


def test_config_skip_exit_withdraws_an_approval_on_another_commit(monkeypatch):
    monkeypatch.setattr(cr_dispatch, "pr_skip_rule", lambda *a, **k: "skip_prs: bots")
    reviews = _old_approval()
    posted, dismissed = _run(monkeypatch, _clean(), blocking=True, reviews=reviews)
    assert posted == []
    assert dismissed == [1]


def test_carried_verdict_exit_withdraws_an_approval_on_another_commit(monkeypatch):
    monkeypatch.setattr(
        cr_dispatch.carry_verdict, "carry_if_unchanged",
        lambda *a, **k: {"persona": "code_reviewer", "result": "pass"},
    )
    posted, dismissed = _run(
        monkeypatch, _clean(), blocking=True, reviews=_old_approval(),
    )
    assert posted == []
    assert dismissed == [1]


def test_degraded_fetch_exit_withdraws_an_approval_on_another_commit(monkeypatch):
    def _down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(cr_dispatch, "fetch_pr_diff", _down)
    posted, dismissed = _run(
        monkeypatch, _clean(), blocking=True, reviews=_old_approval(),
    )
    assert "APPROVE" not in [r.event for r in posted]
    assert dismissed == [1]


def test_exception_exit_withdraws_an_approval_on_another_commit(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(cr_dispatch, "parse_diff", _boom)
    reviews = _old_approval()
    with pytest.raises(RuntimeError):
        _run(monkeypatch, _clean(), blocking=True, reviews=reviews)
    assert reviews[0]["state"] == "DISMISSED"


def test_first_review_exit_keeps_this_heads_approval(monkeypatch):
    """The final withdrawal touches other commits only: the approval this
    run just posted on its head stands."""
    reviews = _old_approval() + [_review(2, state="APPROVED", commit=_HEAD)]
    posted, dismissed = _run(monkeypatch, _clean(), blocking=True, reviews=reviews)
    assert [r.event for r in posted] == ["APPROVE"]
    assert dismissed == [1]


# --- #1781: no approval over unreviewed code ---------------------------------


def _with_file(path: str, body: str = "+x = 1\n") -> str:
    lines = body.count("\n")
    return _DIFF + (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,1 +1,{lines + 1} @@\n context\n{body}"
    )


def _ignore_docs(monkeypatch):
    from personas.code_reviewer.repo_config import parse_repo_config

    config = parse_repo_config("ignore:\n  - 'docs/**'\n")
    monkeypatch.setattr(cr_dispatch, "_load_pr_repo_config", lambda *a, **k: config)


@pytest.mark.parametrize("kind", ["excluded", "oversized", "ignored"])
@pytest.mark.parametrize("blocking", [False, True], ids=["advisory", "blocking"])
def test_unreviewed_path_never_approves_and_is_named(monkeypatch, kind, blocking):
    """Elder approves only code it read. A changed path dropped before the
    model saw it (data or generated, too big for any cohort, or ignored by
    `.grug.yaml`) leaves the head unvouched, and the summary names it."""
    if kind == "excluded":
        path, body = "data/rows.csv", "+a,b\n"
    elif kind == "oversized":
        path, body = "src/huge.py", "".join(f"+v{i} = {i}\n" for i in range(2000))
        monkeypatch.setattr(cr_dispatch, "review_cohort_char_budget", lambda: 2000)
    else:
        path, body = "docs/guide.md", "+words\n"
        _ignore_docs(monkeypatch)
    checks: list = []
    posted, _ = _run(
        monkeypatch, _clean(), blocking=blocking, diff=_with_file(path, body),
        checks=checks,
    )
    assert "APPROVE" not in [r.event for r in posted]
    summary = checks[-1].summary
    assert "Elder does not approve this head" in summary
    assert f"`{path}`" in summary.split("Elder does not approve this head", 1)[1]


def test_hunk_that_splits_into_a_few_pieces_is_read_and_the_note_says_so(monkeypatch):
    """#1747: a hunk over one cohort that fits a few pieces is read in full
    (so it is not an unreviewed path) and the summary records the
    compression. A hunk that needs far more pieces is still dropped, named
    and never approved (the oversized case above)."""
    body = "".join(f"+v{i} = {i}\n" for i in range(400))
    monkeypatch.setattr(cr_dispatch, "review_cohort_char_budget", lambda: 2000)
    checks: list = []
    _run(
        monkeypatch, _clean(), blocking=True,
        diff=_with_file("src/huge.py", body), checks=checks,
    )
    summary = checks[-1].summary
    assert "Compressed: the change to `src/huge.py`" in summary
    assert "Elder does not approve this head" not in summary


def test_unreviewed_path_still_requests_changes_on_a_block(monkeypatch):
    _ignore_docs(monkeypatch)
    posted, _ = _run(
        monkeypatch, _with("high"), blocking=True,
        diff=_with_file("docs/guide.md", "+words\n"),
    )
    assert [r.event for r in posted][-1] == "REQUEST_CHANGES"


_HEAD2 = "feed5678beef"
_HUGE = "".join(f"+v{i} = {i}\n" for i in range(2000))
_OTHER = """diff --git a/src/y.py b/src/y.py
--- a/src/y.py
+++ b/src/y.py
@@ -1,1 +1,2 @@
 context
+y = 2
"""


def test_unreviewed_path_is_not_skipped_by_the_next_delta_review(monkeypatch):
    """#1781: push 1 carries a file too big to review, so it gets no
    approval. Push 2 touches only another file. The Living Hunt baseline
    must not have moved past push 1, or push 2's delta would skip the big
    file and approve code nobody read."""
    import adapters.install_store as store

    baseline: dict = {}
    monkeypatch.setattr(store, "get_elder_last_reviewed", lambda **k: baseline.get("head"))
    monkeypatch.setattr(
        store, "put_elder_last_reviewed", lambda **k: baseline.update(head=k["head_sha"]),
    )
    monkeypatch.setattr(cr_dispatch, "review_cohort_char_budget", lambda: 2000)
    full_pr = _with_file("src/huge.py", _HUGE) + _OTHER
    heads = iter((_HEAD, _HEAD2))
    current = {}

    def _snapshot(*a, **k):
        return ("id", current["head"], "open", False)

    monkeypatch.setattr(cr_dispatch, "_fetch_current_review_snapshot", _snapshot)
    monkeypatch.setattr(
        cr_dispatch, "fetch_pr_diff_with_scope", lambda *a, **k: (_OTHER, True, False),
    )

    current["head"] = next(heads)
    first, _ = _run(
        monkeypatch, _clean(), blocking=True, diff=_with_file("src/huge.py", _HUGE),
    )
    current["head"] = next(heads)
    second, _ = _run(
        monkeypatch, _clean(), blocking=True, action="synchronize", diff=full_pr,
        head=_HEAD2,
    )

    assert "APPROVE" not in [r.event for r in first]
    assert "APPROVE" not in [r.event for r in second]


def test_lock_files_are_left_to_the_lock_check():
    """A lock file the model skipped is the lock check's to vouch for
    (`lock_check.unreviewed`), so it is not counted twice; every other
    dropped path is named once, in diff order."""
    assert cr_dispatch._unreviewed_paths(
        excluded=("uv.lock", "data/rows.csv"),
        oversized=("src/huge.py", "data/rows.csv"),
        ignored=("docs/guide.md",),
    ) == ("data/rows.csv", "src/huge.py", "docs/guide.md")


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


def test_a_40000_line_file_is_named_and_never_approved_live(monkeypatch):
    """#1747: at the default cohort budget a 40,000 line added file needs
    about 30 pieces, over the bound, so live Elder drops it, names it and
    does not approve. Same bound as the replay harness."""
    body = "".join(f"+v{i} = {i}\n" for i in range(40_000))
    checks: list = []
    posted, _ = _run(
        monkeypatch, _clean(), blocking=True,
        diff=_with_file("src/huge.py", body), checks=checks,
    )
    assert "APPROVE" not in [r.event for r in posted]
    summary = checks[-1].summary
    assert "Compressed: the change to `src/huge.py`" not in summary
    assert "`src/huge.py`" in summary.split("Elder does not approve this head", 1)[1]
