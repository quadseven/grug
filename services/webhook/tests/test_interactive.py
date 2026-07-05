"""#528: interactive command actions - idempotency, routing, default-safe."""

from __future__ import annotations

import interactive


def _tok(fn):
    return fn("tok")


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
    enq = []
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True,
        enqueue=lambda **k: enq.append(k),
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
    """#528 Qodo: the LLM Q&A must NOT run inline in the webhook path - ask
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
    """#528 Qodo: claim happens AFTER enqueue, so an enqueue crash leaves the
    comment un-claimed (a retry can re-attempt) instead of silently lost."""
    monkeypatch.setattr(interactive, "httpx", _FakeHttp())
    claimed = []
    def _boom(**k): raise RuntimeError("queue down")
    res = interactive.run_command(
        "improve", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: claimed.append(k) or True,
        enqueue=_boom,
    )
    assert res["status"] == "skip" and res["reason"] == "enqueue_failed"
    assert claimed == []  # NOT claimed on enqueue failure
