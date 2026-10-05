"""Tests for personas/code_reviewer/thread_resolver.py (#1102).

On a new Elder review, Elder resolves ITS OWN inline threads whose code a
later push changed and which the new review does not re-raise, replying
"fixed in <sha>" first. Anyone else's thread, and any thread a person
replied to, is never touched.
"""
from __future__ import annotations

import httpx
import pytest

from personas.code_reviewer import thread_resolver as tr
from personas.code_reviewer.dedup import rule_marker
from personas.code_reviewer.persona import Finding

HEAD = "abcdef1234567890abcdef1234567890abcdef12"


def _finding(file="a.py", line=10, rule="null-deref") -> Finding:
    return Finding(
        file=file, line=line, severity="high", rule_name=rule,
        message="m", suggestion=None,
    )


def _comment(author="grug-tribe", rule="null-deref", oid="c0ffee", db_id=1):
    body = "finding\n\n" + (rule_marker(rule) if rule else "")
    return {
        "databaseId": db_id, "author": {"login": author}, "body": body,
        "path": "a.py", "line": None, "originalLine": 10,
        "originalCommit": {"oid": oid} if oid else None,
    }


def _thread(tid="T1", *, outdated=True, resolved=False, comments=None,
            total=None):
    nodes = comments if comments is not None else [_comment()]
    return {
        "id": tid, "isResolved": resolved, "isOutdated": outdated,
        "comments": {"totalCount": total if total is not None else len(nodes),
                     "nodes": nodes},
    }


class _Gh:
    """Fake GraphQL endpoint recording replies/resolves."""

    def __init__(self, threads, *, resolve_error=None):
        self.threads = threads
        self.replies: list[tuple[str, str]] = []
        self.resolved: list[str] = []
        self.resolve_error = resolve_error

    def post(self, url, json=None, headers=None, timeout=None):
        q, v = json["query"], json["variables"]
        if "reviewThreads" in q:
            data = {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": self.threads}}}}
        elif "addPullRequestReviewThreadReply" in q:
            self.replies.append((v["thread"], v["body"]))
            data = {"addPullRequestReviewThreadReply": {"comment": {"id": "x"}}}
        elif "resolveReviewThread" in q:
            if self.resolve_error:
                return _Resp({"errors": [{"message": self.resolve_error}]})
            self.resolved.append(v["thread"])
            data = {"resolveReviewThread": {"thread": {"isResolved": True}}}
        else:  # pragma: no cover
            raise AssertionError(q)
        return _Resp({"data": data})


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


@pytest.fixture
def run(monkeypatch):
    def _run(gh, findings=()):
        monkeypatch.setattr(tr.httpx, "post", gh.post)
        monkeypatch.setattr(
            tr, "with_install_token_retry", lambda iid, fn: fn("tok"),
        )
        return tr.resolve_fixed_threads(
            1, "o", "r", 5, head_sha=HEAD, findings=tuple(findings),
        )
    return _run


def test_fixed_and_not_reraised_replies_then_resolves(run):
    gh = _Gh([_thread()])
    assert run(gh) == 1
    assert gh.resolved == ["T1"]
    assert len(gh.replies) == 1
    assert gh.replies[0][0] == "T1" and HEAD[:7] in gh.replies[0][1]


def test_reply_is_posted_before_resolve(run, monkeypatch):
    gh = _Gh([_thread()])
    order = []
    orig = gh.post

    def spy(url, json=None, **kw):
        order.append("reply" if "addPullRequestReviewThreadReply" in json["query"]
                     else "resolve" if "resolveReviewThread" in json["query"]
                     else "list")
        return orig(url, json=json, **kw)
    gh.post = spy
    run(gh)
    assert order == ["list", "reply", "resolve"]


def test_changed_but_reraised_nearby_stays_open(run):
    gh = _Gh([_thread()])
    assert run(gh, [_finding(line=12)]) == 0
    assert gh.resolved == [] and gh.replies == []


def test_same_file_other_rule_does_not_count_as_reraise(run):
    gh = _Gh([_thread()])
    assert run(gh, [_finding(rule="other-rule")]) == 1


def test_same_rule_far_away_does_not_count_as_reraise(run):
    gh = _Gh([_thread()])
    assert run(gh, [_finding(line=500)]) == 1


def test_untouched_code_stays_open(run):
    gh = _Gh([_thread(outdated=False)])
    assert run(gh) == 0
    assert gh.resolved == [] and gh.replies == []


def test_already_resolved_is_skipped(run):
    gh = _Gh([_thread(resolved=True)])
    assert run(gh) == 0 and gh.replies == []


def test_human_thread_never_resolved(run):
    gh = _Gh([_thread(comments=[_comment(author="alice")])])
    assert run(gh) == 0
    assert gh.resolved == [] and gh.replies == []


def test_thread_without_grug_marker_never_resolved(run):
    gh = _Gh([_thread(comments=[_comment(rule=None)])])
    assert run(gh) == 0 and gh.resolved == []


def test_human_reply_in_grug_thread_blocks_resolution(run):
    gh = _Gh([_thread(comments=[_comment(), _comment(author="alice", db_id=2)])])
    assert run(gh) == 0
    assert gh.resolved == [] and gh.replies == []


def test_grug_own_followup_reply_does_not_block(run):
    gh = _Gh([_thread(comments=[_comment(), _comment(db_id=2)])])
    assert run(gh) == 1


def test_truncated_comment_list_is_treated_as_touched(run):
    gh = _Gh([_thread(total=40)])
    assert run(gh) == 0 and gh.resolved == []


def test_gone_original_commit_is_skipped(run):
    gh = _Gh([_thread(comments=[_comment(oid=None)])])
    assert run(gh) == 0 and gh.resolved == []


def test_bot_suffix_login_also_matches(run):
    gh = _Gh([_thread(comments=[_comment(author="grug-tribe[bot]")])])
    assert run(gh) == 1


def test_resolve_failure_is_logged_with_kind_and_never_raises(run, caplog):
    gh = _Gh([_thread(), _thread(tid="T2")], resolve_error="boom")
    with caplog.at_level("WARNING"):
        assert run(gh) == 0
    kinds = [getattr(r, "kind", None) for r in caplog.records
             if r.getMessage() == "elder_thread_resolve_failed"]
    assert kinds == ["GraphQLError", "GraphQLError"]  # second thread still tried


def test_list_failure_never_raises(monkeypatch, caplog):
    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(tr.httpx, "post", boom)
    monkeypatch.setattr(tr, "with_install_token_retry", lambda iid, fn: fn("t"))
    with caplog.at_level("WARNING"):
        assert tr.resolve_fixed_threads(
            1, "o", "r", 5, head_sha=HEAD, findings=(),
        ) == 0
    assert any(getattr(r, "kind", None) == "ConnectError" for r in caplog.records)


def test_a_thread_already_answered_fixed_is_resolved_without_a_second_reply(run):
    """If an earlier pass posted "Fixed in" but its resolve call failed, the
    thread is still wholly Elder's and outdated. The next pass must resolve
    it without stacking a second "Fixed in" reply."""
    earlier = _comment(rule=None, db_id=2)
    earlier["body"] = "Fixed in 1234567: the flagged code changed and this review does not raise it again."
    gh = _Gh([_thread(comments=[_comment(), earlier])])
    assert run(gh) == 1
    assert gh.replies == []
    assert gh.resolved == ["T1"]
