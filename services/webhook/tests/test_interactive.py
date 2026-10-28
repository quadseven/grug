"""#528: interactive command actions - idempotency, routing, default-safe.

grug#1270: an Elder re-run is labelled `grug:reviewing` while it runs, and
is KEPT (deferred to the GitHub budget's reset) instead of dropped when the
budget is spent.

grug#1430: a command posted while a review is in flight is QUEUED behind it
(not refused), each command job carries its own comment identity, and a
failed enqueue is answered on the PR.
"""

from __future__ import annotations

import interactive
import review_label


def _tok(fn):
    return fn("tok")


_HEAD = "a" * 40


def _elder_state(monkeypatch, state: str = "absent") -> None:
    """Stub the Elder check state - the ground truth for "is a review
    running on this head?". "absent" is a head nobody reviewed yet."""
    monkeypatch.setattr(review_label, "elder_check_state", lambda **k: state)


def test_idempotent_per_comment(monkeypatch):
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=99, token_fn=_tok, enqueue=lambda **k: None,
        claim=lambda key: False,  # already handled
    )
    assert res["status"] == "no_op" and "already" in res["reason"]


class _FakeHttp:
    def __init__(self): self.calls = []
    def post(self, url, **kw): self.calls.append(("post", url, kw.get("json"))); return _R()
    def get(self, url, **kw): self.calls.append(("get", url)); return _R(text="diff --git ...")
    HTTPStatusError = Exception
    RequestError = Exception
    TimeoutException = Exception


class _R:
    def __init__(self, text=""): self.text = text
    def raise_for_status(self): pass


def test_improve_enqueues_code_reviewer_and_replies(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    _elder_state(monkeypatch)
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k), head_sha=_HEAD,
    )
    assert res["persona"] == "code_reviewer" and res["status"] == "dispatched"
    assert enq[0]["persona"] == "code_reviewer" and enq[0]["repo"] == "o/r"
    assert any(c[0] == "post" for c in fake.calls)  # posted a reply


def test_test_gaps_enqueues_smasher(monkeypatch):
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())
    enq = []
    res = interactive.run_command(
        "test-gaps", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k),
    )
    assert enq[0]["persona"] == "smasher" and res["status"] == "dispatched"


def test_ask_empty_question_posts_usage(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    asks = []
    res = interactive.run_command(
        "ask", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: None, enqueue_ask=lambda **k: asks.append(k),
    )
    assert res["status"] == "no_op" and "empty" in res["reason"]
    assert asks == []  # no enqueue for an empty question
    assert any("Usage" in str(c[2]) for c in fake.calls if c[0] == "post")


def test_ask_enqueues_async_not_inline(monkeypatch):
    """#528 LORE: the LLM Q&A must NOT run inline in the webhook path - ask
    ENQUEUES to the consumer with the question + comment_id for dedup."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    asks = []
    res = interactive.run_command(
        "ask", "how many secrets?", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=77, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: None, enqueue_ask=lambda **k: asks.append(k),
    )
    assert res["status"] == "dispatched"
    assert asks[0]["question"] == "how many secrets?" and asks[0]["comment_id"] == 77
    # no inline reply posted (the consumer posts the answer async)
    assert not any(c[0] == "post" for c in fake.calls)


def test_enqueue_failure_not_claimed(monkeypatch):
    """#528 LORE: claim happens AFTER enqueue, so an enqueue crash leaves the
    comment un-claimed (a retry can re-attempt) instead of silently lost."""
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())
    _elder_state(monkeypatch)
    claimed = []
    def _boom(**k): raise RuntimeError("queue down")
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: claimed.append(k) or True,
        enqueue=_boom, head_sha=_HEAD,
    )
    assert res["status"] == "skip" and res["reason"] == "enqueue_failed"
    assert claimed == []  # NOT claimed on enqueue failure


# --- grug#1188: /grug resume and /grug review --------------------------------


def _wake(monkeypatch, verb, *, paused, resume_raises=False, claim_ok=True, enqueue=None):
    enq = []
    state = {"paused": paused, "resumed": 0}
    _elder_state(monkeypatch)

    def resume(**_k):
        if resume_raises:
            raise RuntimeError("db down")
        state["resumed"] += 1
        was = state["paused"]
        state["paused"] = False
        return was

    res = interactive.run_command(
        verb, "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=5, token_fn=_tok, claim=lambda k: claim_ok,
        enqueue=enqueue or (lambda **k: enq.append(k)),
        resume=resume, is_paused=lambda **_k: state["paused"],
        head_sha=_HEAD,
    )
    return res, enq, state


def test_resume_lifts_the_pause_and_enqueues_elder(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    res, enq, state = _wake(monkeypatch, "resume", paused=True)
    assert res["status"] == "dispatched" and res["persona"] == "code_reviewer"
    assert res["paused"] == "false"
    assert enq == [{"install_id": 1, "repo": "o/r", "pr_number": 2, "persona": "code_reviewer", "comment_id": 5}]
    assert state == {"paused": False, "resumed": 1}
    replies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert len(replies) == 1 and "awake" in replies[0]


def test_resume_when_not_paused_still_reviews(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    res, enq, _ = _wake(monkeypatch, "resume", paused=False)
    assert res["status"] == "dispatched" and len(enq) == 1
    replies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert "was not resting" in replies[0]


def test_review_enqueues_once_and_keeps_the_pause(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    res, enq, state = _wake(monkeypatch, "review", paused=True)
    assert res["status"] == "dispatched" and res["paused"] == "true"
    assert len(enq) == 1 and enq[0]["persona"] == "code_reviewer"
    assert state["paused"] is True and state["resumed"] == 0
    replies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert "/grug resume" in replies[0]


def test_resume_store_failure_still_reviews_and_says_so(monkeypatch, caplog):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    res, enq, _ = _wake(monkeypatch, "resume", paused=True, resume_raises=True)
    assert res["status"] == "dispatched" and len(enq) == 1
    assert res["paused"] == "unknown"
    assert "interactive_resume_failed" in caplog.text
    replies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert "could not lift" in replies[0]


def test_wake_enqueue_failure_is_not_claimed_and_keeps_the_pause(monkeypatch):
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())

    def _boom(**_k):
        raise RuntimeError("queue down")

    res, _, state = _wake(monkeypatch, "resume", paused=True, enqueue=_boom)
    assert res["status"] == "skip" and res["reason"] == "enqueue_failed"
    assert state == {"paused": True, "resumed": 0}


def test_wake_already_handled_comment_is_a_no_op(monkeypatch):
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())
    res, _, state = _wake(monkeypatch, "resume", paused=True, claim_ok=False)
    assert res["status"] == "no_op" and "already" in res["reason"]
    assert state["resumed"] == 0


def test_learnings_list_posts_rows(monkeypatch):
    """`/grug learnings list` posts the remembered markings as a reply."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)

    def fake_list(repo):
        return [
            {"text": "no secrets in fixtures", "scope_path": "", "author": "a",
             "source_pr": 3, "usage_count": 2},
            {"text": "always add a test", "scope_path": "tests/*", "author": "b",
             "source_pr": 5, "usage_count": 0},
        ]

    calls = []
    monkeypatch.setattr(interactive, "_wake_learnings_list", lambda repo: fake_list(repo))

    res = interactive.run_command(
        "learnings", "list", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
    )
    assert res["status"] == "dispatched" and res["sub"] == "list"
    body = str([c[2] for c in fake.calls if c[0] == "post"][0])
    assert "no secrets in fixtures" in body and "tests/*" in body
    assert "used 2" in body and "used 0" in body


def test_learnings_delete_removes_and_replies(monkeypatch):
    """`/grug learnings delete <digest>` calls delete_learning and reports."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)

    deleted = []
    def fake_delete(repo, digest):
        deleted.append(digest); return True

    monkeypatch.setattr(interactive, "_wake_learnings_delete", lambda repo, d: fake_delete(repo, d))

    res = interactive.run_command(
        "learnings", "delete abc123", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
    )
    assert res["status"] == "dispatched" and res["sub"] == "delete"
    assert deleted == ["abc123"]
    body = str([c[2] for c in fake.calls if c[0] == "post"][0])
    assert "Forgot marking" in body


def test_learnings_delete_unknown_reports_none(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    monkeypatch.setattr(interactive, "_wake_learnings_delete", lambda repo, d: False)

    res = interactive.run_command(
        "learnings", "delete nope", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
    )
    assert res["status"] == "dispatched" and res["sub"] == "delete"
    body = str([c[2] for c in fake.calls if c[0] == "post"][0])
    assert "No such marking" in body


def test_learnings_empty_sub_posts_usage(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    res = interactive.run_command(
        "learnings", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
    )
    assert res["status"] == "no_op" and "unknown" in res["reason"]
    assert any("Usage" in str(c[2]) for c in fake.calls if c[0] == "post")


def test_learnings_delete_missing_arg_posts_usage(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    res = interactive.run_command(
        "learnings", "delete", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
    )
    assert res["status"] == "no_op" and "missing" in res["reason"]
    assert any("Usage" in str(c[2]) for c in fake.calls if c[0] == "post")


# --- grug#1270: label the PR, never overlap, never drop -------------------


def test_improve_labels_the_pr_while_elder_re_reviews(
    monkeypatch, _mock_review_label_http,
):
    """The visible sign that Elder is re-reviewing this PR."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    _elder_state(monkeypatch)
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k), head_sha="a" * 40,
    )
    assert res["status"] == "dispatched" and enq
    attaches = [
        c for c in _mock_review_label_http.calls
        if c[0] == "post" and c[1].endswith("/issues/2/labels")
    ]
    assert len(attaches) == 1
    assert attaches[0][2] == {"labels": ["grug:reviewing"]}




def test_improve_still_runs_when_the_elder_check_concluded(monkeypatch):
    """A FINISHED review is exactly what `/grug improve` is for - only an
    in-flight one blocks the command."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    monkeypatch.setattr(review_label, "elder_check_state", lambda **k: "concluded")
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k), head_sha="a" * 40,
    )
    assert res["status"] == "dispatched" and len(enq) == 1




def test_improve_is_kept_not_dropped_when_the_github_budget_is_spent(monkeypatch):
    """grug#1270: a command the exhausted hourly budget refused goes back on
    the queue with a `not_before`, and says so. Dropping it silently is what
    made the 2026-10-18 burst look like it worked."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k), defer_until=1791218759.0,
    )
    assert res["status"] == "deferred" and res["reason"] == "github_rate_limited"
    assert enq[0]["not_before"] == 1791218759.0
    assert enq[0]["persona"] == "code_reviewer"
    bodies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert any("budget" in str(b) for b in bodies)


def test_deferred_improve_is_answered_when_the_queue_refuses_it(monkeypatch):
    """A command that could not even be queued is ANSWERED, not dropped in
    silence: the reader is told to comment again. The claim was already
    taken (it comes first, so a redelivery cannot queue the same command
    twice), so the trace is the only way back to it."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    claimed = []

    def _boom(**_k):
        raise RuntimeError("queue down")

    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: claimed.append(k) or True,
        enqueue=_boom, defer_until=1791218759.0,
    )
    assert res["status"] == "skip" and res["reason"] == "defer_enqueue_failed"
    assert claimed == ["grugcmd:1"]
    bodies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert any("comment" in str(b).lower() for b in bodies)


def test_a_redelivered_deferred_command_is_queued_only_once(monkeypatch):
    """grug#1270: a deferred job that sits on the queue WILL run later, so a
    redelivered comment must not queue the same command a second time - two
    Elder reviews of one head is the overlap this ticket exists to stop."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: False,
        enqueue=lambda **k: enq.append(k), defer_until=1791218759.0,
    )
    assert res["status"] == "no_op" and res["reason"] == "comment already handled"
    assert enq == []


def test_the_reviewing_label_is_not_marked_for_an_already_handled_comment(
    monkeypatch, _mock_review_label_http,
):
    """A comment that was already handled must not leave a
    `grug:reviewing` label behind for a review this delivery did not
    start - the sign would outlive its review (grug#1270)."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    _elder_state(monkeypatch)
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: False,
        enqueue=lambda **k: enq.append(k), head_sha="a" * 40,
    )
    assert res["status"] == "no_op" and res["reason"] == "comment already handled"
    assert enq
    assert not [
        c for c in _mock_review_label_http.calls
        if c[0] == "post" and c[1].endswith("/issues/2/labels")
    ]






def test_improve_without_a_head_is_not_queued(monkeypatch):
    """An empty head is not "Elder is not reviewing": it is a head nobody
    knows, so a review must not be queued on nothing (grug#1270)."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k), head_sha="",
    )
    assert res["status"] == "no_op" and res["reason"] == "already_reviewing"
    assert enq == []


# --- grug#1430: comment-triggered runs are queued, never dropped -------------


def test_improve_while_elder_runs_is_queued_behind_the_review(monkeypatch):
    """A review in flight no longer refuses the command: the job is queued
    with its own identity and the reply says it waits behind the review."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    _elder_state(monkeypatch, "running")
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=41, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k), head_sha="abc12345" + "0" * 32,
    )
    assert res["status"] == "queued" and res["reason"] == "queued_behind_review"
    assert enq == [{"install_id": 1, "repo": "o/r", "pr_number": 2,
                    "persona": "code_reviewer", "comment_id": 41}]
    bodies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert len(bodies) == 1
    assert "already reviewing abc12345" in bodies[0]
    assert "queued" in bodies[0]


def test_resume_while_elder_runs_is_queued_and_lifts_the_pause(monkeypatch):
    """/grug resume queues behind a review in flight too, and the pause is
    still lifted once the job is queued."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    _elder_state(monkeypatch, "running")
    enq, resumed = [], []
    res = interactive.run_command(
        "resume", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=42, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k), head_sha="abc12345" + "0" * 32,
        resume=lambda **k: resumed.append(k) or True,
        is_paused=lambda **k: False,
    )
    assert res["status"] == "queued" and res["paused"] == "false"
    assert enq and enq[0]["comment_id"] == 42
    assert resumed
    bodies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert "queued" in bodies[0]


def test_an_unreadable_check_state_still_queues_the_command(monkeypatch):
    """An unreadable check-run listing is not a reason to drop the command:
    it is queued, and the consumer decides at run time whether a review is
    in flight. The reply does not claim a review that may not be running."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    _elder_state(monkeypatch, "unknown")
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=43, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k), head_sha="a" * 40,
    )
    assert res["status"] == "queued" and len(enq) == 1
    bodies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert "queued" in bodies[0] and "already reviewing" not in bodies[0]


def test_every_command_job_carries_its_own_comment_identity(monkeypatch):
    """Two different comments enqueue two jobs that differ by comment id, so
    the queue's dedup id differs and neither is collapsed."""
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())
    _elder_state(monkeypatch, "concluded")
    enq = []
    for cid in (7, 8):
        interactive.run_command(
            "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
            comment_id=cid, token_fn=_tok, claim=lambda k: True,
            enqueue=lambda **k: enq.append(k), head_sha="a" * 40,
        )
    assert [e["comment_id"] for e in enq] == [7, 8]


def test_a_queued_command_is_not_claimed_twice(monkeypatch):
    """A redelivered comment that was already queued answers nothing more."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    _elder_state(monkeypatch, "running")
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=44, token_fn=_tok, claim=lambda k: False,
        enqueue=lambda **k: None, head_sha="a" * 40,
    )
    assert res["status"] == "no_op" and res["reason"] == "comment already handled"
    assert not [c for c in fake.calls if c[0] == "post"]


def test_improve_enqueue_failure_is_answered(monkeypatch):
    """An enqueue that fails is answered with "comment again" instead of
    returning silently, and the comment stays unclaimed."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    _elder_state(monkeypatch)
    claimed = []

    def _boom(**_k):
        raise RuntimeError("queue down")

    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=45, token_fn=_tok, claim=lambda k: claimed.append(k) or True,
        enqueue=_boom, head_sha=_HEAD,
    )
    assert res["status"] == "skip" and res["reason"] == "enqueue_failed"
    assert claimed == []
    bodies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert bodies == ["Grug could not queue this command. Comment `/grug improve` again."]


def test_review_and_test_gaps_enqueue_failures_are_answered(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    _elder_state(monkeypatch)

    def _boom(**_k):
        raise RuntimeError("queue down")

    for verb in ("review", "test-gaps"):
        res = interactive.run_command(
            verb, "", install_id=1, owner="o", repo="r", pr_number=2,
            comment_id=46, token_fn=_tok, claim=lambda k: True,
            enqueue=_boom, head_sha=_HEAD,
            resume=lambda **k: False, is_paused=lambda **k: False,
        )
        assert res["reason"] == "enqueue_failed"
    bodies = [c[2]["body"] for c in fake.calls if c[0] == "post"]
    assert any("/grug review` again" in b for b in bodies)
    assert any("/grug test-gaps` again" in b for b in bodies)


def test_a_headless_command_answer_failure_leaves_it_unclaimed(monkeypatch):
    """With no head there is nothing to queue against; the command is
    answered, and a reply that did not land leaves it unclaimed."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    claimed = []

    def _boom(*_a, **_k):
        raise fake.HTTPStatusError("github is down")

    monkeypatch.setattr(interactive, "_post_reply", _boom)
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: claimed.append(k) or True,
        enqueue=lambda **k: None, head_sha="",
    )
    assert res["status"] == "skip" and res["reason"] == "reply_failed"
    assert claimed == []


# --- /grug fix (epic #1311, v1 autofix) -------------------------------------


def test_fix_routes_to_fix_module(monkeypatch):
    """`/grug fix` is routed to autofix.run_fix with the right arguments."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    called = {}
    import autofix as fix_module

    def fake_run_fix(*, install_id, owner, repo, pr_number,
                     head_sha, token_fn, **_k):
        called.update(install_id=install_id, owner=owner, repo=repo,
                      pr_number=pr_number, head_sha=head_sha)
        return {"status": "dispatched", "verb": "fix"}

    monkeypatch.setattr(fix_module, "run_fix", fake_run_fix)
    res = interactive.run_command(
        "fix", "", install_id=3, owner="o", repo="r", pr_number=9,
        comment_id=11, token_fn=_tok, claim=lambda k: True, head_sha="a" * 40,
    )
    assert res == {"status": "dispatched", "verb": "fix"}
    assert called == {
        "install_id": 3, "owner": "o", "repo": "r",
        "pr_number": 9, "head_sha": "a" * 40,
    }


def test_fix_claims_before_running(monkeypatch):
    """A redelivered /grug fix comment is not run twice (claim first)."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    claimed = []
    import autofix as fix_module

    def fake_run_fix(*, install_id, owner, repo, pr_number, comment_id,
                     head_sha, token_fn, **_k):
        return {"status": "dispatched", "verb": "fix"}

    monkeypatch.setattr(fix_module, "run_fix", fake_run_fix)
    res = interactive.run_command(
        "fix", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: claimed.append(k) or False,
        head_sha="a" * 40,
    )
    assert res["status"] == "no_op" and "already handled" in res["reason"]
    assert claimed == ["grugcmd:1"]


def test_fix_is_not_an_elder_rerun_verb():
    """fix must not be in ELDER_RERUN_VERBS - it is not budget-deferred."""
    from grug_commands import ELDER_RERUN_VERBS
    assert "fix" not in ELDER_RERUN_VERBS


def test_fix_crash_is_answered_not_a_webhook_500(monkeypatch):
    """The comment is claimed before run_fix runs, so GitHub never redelivers
    it. A raise inside run_fix must be answered on the PR and returned as a
    skip, never propagated into a webhook 500 that drops the command."""
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    import autofix as fix_module

    def boom(**_k):
        raise RuntimeError("git data api exploded")

    monkeypatch.setattr(fix_module, "run_fix", boom)
    res = interactive.run_command(
        "fix", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=5, token_fn=_tok, claim=lambda k: True, head_sha="a" * 40,
    )
    assert res == {"status": "skip", "reason": "fix_failed", "verb": "fix"}
    replies = [c for c in fake.calls if c[0] == "post"]
    assert replies and "/grug fix" in replies[0][2]["body"]


# --- #695: eyes on receipt, final sign on resolve -----------------------------


class _ReactionHttp:
    """Records the reaction calls `comment_reactions` makes."""

    HTTPError = Exception

    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("post", url.rsplit("/reactions", 1)[0][-12:], kw["json"]["content"]))
        r = _R()
        r.json = lambda: {"id": 500 + len(self.calls)}
        return r

    def delete(self, url, **kw):
        self.calls.append(("delete", url.rsplit("/", 1)[1]))
        return _R()


def test_slash_command_gets_eyes_then_thumbs_up(monkeypatch):
    import comment_reactions

    http = _ReactionHttp()
    monkeypatch.setattr(comment_reactions, "httpx", http)
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())
    _elder_state(monkeypatch)
    interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=9, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: None, head_sha=_HEAD,
    )
    assert [c[:1] + c[2:] for c in http.calls if c[0] == "post"] == [
        ("post", "eyes"), ("post", "+1"),
    ]
    assert ("delete", "501") in http.calls  # the eyes reaction came off


def test_failed_command_ends_on_thumbs_down(monkeypatch):
    import comment_reactions

    http = _ReactionHttp()
    monkeypatch.setattr(comment_reactions, "httpx", http)
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())

    def boom(**k):
        raise RuntimeError("queue down")

    res = interactive.run_command(
        "test-gaps", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=9, token_fn=_tok, claim=lambda k: True, enqueue=boom,
    )
    assert res["status"] == "skip"
    assert [c[2] for c in http.calls if c[0] == "post"] == ["eyes", "-1"]


def test_ask_keeps_eyes_and_hands_the_reaction_to_the_consumer(monkeypatch):
    import comment_reactions

    http = _ReactionHttp()
    monkeypatch.setattr(comment_reactions, "httpx", http)
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())
    asks = []
    interactive.run_command(
        "ask", "why?", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=9, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: None, enqueue_ask=lambda **k: asks.append(k),
    )
    assert [c[2] for c in http.calls if c[0] == "post"] == ["eyes"]
    assert asks[0]["reaction_id"] == 501


def test_command_crash_is_logged_signed_failed_and_reraised(monkeypatch, caplog):
    """A fault in the command is logged by kind, the sign flips to
    thumbs-down, and the fault still propagates (#695)."""
    swaps = []

    def boom(*a, **k):
        raise ValueError("kaboom")

    monkeypatch.setattr(interactive, "_execute_command", boom)
    monkeypatch.setattr(interactive, "_react", lambda tf, fn, *a: swaps.append(a) or 7)
    with caplog.at_level("ERROR"):
        try:
            interactive.run_command(
                "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
                comment_id=5, token_fn=_tok,
            )
            raised = False
        except ValueError:
            raised = True
    assert raised
    assert "interactive_command_failed" in caplog.text
    kinds = [r.kind for r in caplog.records if r.getMessage() == "interactive_command_failed"]
    assert kinds == ["ValueError"]
    assert swaps[-1][-1] == interactive.comment_reactions.FAILED
