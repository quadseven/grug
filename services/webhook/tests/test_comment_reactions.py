"""#695: the command-comment reaction helpers are best-effort signs."""

from __future__ import annotations

import comment_reactions
import httpx


class _Http:
    HTTPError = httpx.HTTPError

    def __init__(self, post_error=None):
        self.post_error = post_error
        self.calls = []

    def post(self, url, **kw):
        if self.post_error:
            raise self.post_error
        self.calls.append(("post", kw["json"]["content"]))
        r = httpx.Response(201, json={"id": 31}, request=httpx.Request("POST", url))
        return r

    def delete(self, url, **kw):
        self.calls.append(("delete", url.rsplit("/", 1)[1]))
        return httpx.Response(204, request=httpx.Request("DELETE", url))


def test_swap_adds_the_new_sign_then_removes_the_old_one(monkeypatch):
    http = _Http()
    monkeypatch.setattr(comment_reactions, "httpx", http)
    assert comment_reactions.swap_reaction("t", "o", "r", 5, 12, "+1") == 31
    assert http.calls == [("post", "+1"), ("delete", "12")]


def test_a_refused_reaction_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(comment_reactions, "httpx", _Http(httpx.ConnectError("down")))
    with caplog.at_level("WARNING"):
        assert comment_reactions.add_reaction("t", "o", "r", 5, "eyes") is None
    assert "comment_reaction_add_failed" in caplog.text


def test_failed_swap_keeps_the_old_sign(monkeypatch):
    http = _Http(httpx.ConnectError("down"))
    monkeypatch.setattr(comment_reactions, "httpx", http)
    assert comment_reactions.swap_reaction("t", "o", "r", 5, 12, "-1") is None
    assert http.calls == []
