"""`/grug walkthrough` adds the opt-in label AND enqueues the walkthrough.

The label gate alone would leave a person who typed the command waiting for
the next push: no `labeled` pull_request action is dispatched, so the command
itself must start the run (grug#1356).
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

import dispatcher as d


def _comment_payload(*, body: str = "/grug walkthrough"):
    return {
        "action": "created",
        "issue": {
            "number": 42,
            "user": {"login": "alice"},
            "pull_request": {"url": "..."},
        },
        "comment": {"id": 7, "body": body},
        "repository": {
            "id": 100, "name": "myrepo",
            "owner": {"login": "myorg"}, "full_name": "myorg/myrepo",
        },
        "installation": {"id": 1},
        "sender": {"login": "alice"},
    }


_FAKE_PR = {
    "number": 42,
    "head": {"sha": "abc123"},
    "base": {
        "sha": "def456",
        "repo": {"name": "myrepo", "owner": {"login": "myorg"}, "private": False},
    },
    "title": "a change",
    "body": "body",
    "user": {"login": "alice"},
}


@pytest.fixture
def _no_install_lookups(monkeypatch):
    monkeypatch.setattr(d, "is_install_allowlisted", lambda _id: True)
    monkeypatch.setattr(d, "is_persona_enabled", lambda *_: True)


def _run(label_ok: bool = True, enqueued: bool = True):
    with patch("github_app_auth.with_install_token_retry", side_effect=lambda _i, fn: fn("tok")), \
         patch("httpx.get") as get_mock, \
         patch("httpx.post") as post_mock, \
         patch("async_dispatch.enqueue_walkthrough_review", return_value=enqueued) as enq:
        get_mock.return_value.raise_for_status = lambda: None
        get_mock.return_value.json.return_value = _FAKE_PR
        if label_ok:
            post_mock.return_value.raise_for_status = lambda: None
            post_mock.return_value.json.return_value = []
        else:
            post_mock.side_effect = httpx.ConnectError("down")
        out = d.dispatch("issue_comment", _comment_payload())
    return out, post_mock, enq


def test_command_alone_enqueues_one_walkthrough(_no_install_lookups):
    out, post_mock, enq = _run()
    post_mock.assert_called_once()
    assert post_mock.call_args.kwargs["json"] == {"labels": ["grug:walkthrough"]}
    enq.assert_called_once()
    payload = enq.call_args.kwargs["payload"]
    assert payload["pull_request"]["number"] == 42
    assert payload["pull_request"]["head"]["sha"] == "abc123"
    assert payload["repository"]["owner"]["login"] == "myorg"
    assert payload["repository"]["name"] == "myrepo"
    assert payload["installation"]["id"] == 1
    assert out == {"status": "ok", "trigger": "walkthrough", "result": "queued"}


def test_enqueue_failure_is_reported_not_hidden(_no_install_lookups):
    with patch("async_dispatch.self_recover_review") as recover:
        out, _post, enq = _run(enqueued=False)
    enq.assert_called_once()
    recover.assert_called_once()
    assert out["result"] == "enqueue_failed"


def test_label_failure_does_not_enqueue(_no_install_lookups):
    out, _post, enq = _run(label_ok=False)
    enq.assert_not_called()
    assert out == {"status": "skip", "reason": "label_add_transport_failed"}
