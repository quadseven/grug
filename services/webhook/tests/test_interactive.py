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
    res = interactive.run_command(
        "ask", "", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True, enqueue=lambda **k: None,
    )
    assert res["status"] == "no_op" and "empty" in res["reason"]
    assert any("Usage" in str(c[2]) for c in fake.calls if c[0] == "post")


def test_ask_answers_from_diff(monkeypatch):
    fake = _FakeHttp(); monkeypatch.setattr(interactive, "httpx", fake)
    monkeypatch.setattr("llm_client.answer_pr_question", lambda q, d, i: "The rollback restores 3 secrets.")
    res = interactive.run_command(
        "ask", "how many secrets?", install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=1, token_fn=_tok, claim=lambda k: True, enqueue=lambda **k: None,
    )
    assert res["status"] == "dispatched"
    posted = [c for c in fake.calls if c[0] == "post"][-1][2]["body"]
    assert "restores 3 secrets" in posted
