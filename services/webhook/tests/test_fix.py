"""Tests for /grug fix (epic #1311, v1 autofix).

Covers:
- run_fix routes to the fix module and claims the comment
- no review session -> skip with a reply
- no open committable findings -> no_op with a reply
- open findings with suggestions that apply -> one commit, applied listed
- suggestions that no longer apply -> skipped, listed, never force-applied
- mixed applied/skipped -> both listed in the reply
- idempotency: a re-delivered comment is a no_op
- _open_committable_findings filters state and suggestion
- _fix_still_applies verifies against current file text
"""

from __future__ import annotations

from types import SimpleNamespace

import fix
import review_session as rs


class _FakeSession:
    """A stub ReviewSession with a controllable findings tuple."""

    def __init__(self, findings):
        self.findings = tuple(findings)


def _rec(
    path="a.py", line=1, state="open", suggestion="x = 2", rule="some-rule",
    message="do this", persona="elder",
):
    return SimpleNamespace(
        path=path, line=line, state=state, suggestion=suggestion,
        rule=rule, message=message, persona=persona,
        finding_id="fid1", category="c", severity="low",
        confidence=None, evidence="x = 1", source="llm",
        state_sha="s", first_seen_sha="s", last_seen_sha="s",
        history=(), superseded_by="", inline_comment_ids=(),
        check_run_ids=(),
    )


def _make_finding_record(**kw):
    base = dict(
        finding_id="fid1", persona="elder", path="a.py", line=1,
        rule="some-rule", category="c", severity="low",
        confidence=None, message="do this", evidence="x = 1",
        source="llm", state="open", state_sha="s", first_seen_sha="s",
        last_seen_sha="s", history=(), superseded_by="",
        inline_comment_ids=(), check_run_ids=(), suggestion=None,
    )
    base.update(kw)
    return rs.FindingRecord(**base)


def test_open_committable_findings_filters_state_and_suggestion():
    session = _FakeSession([
        _rec(state="open", suggestion="x = 2"),
        _rec(state="addressed", suggestion="x = 2"),  # not open
        _rec(state="open", suggestion=None),  # no suggestion
        _rec(state="open", suggestion="y = 3"),
    ])
    out = fix._open_committable_findings(session)
    assert len(out) == 2
    assert all(f.suggestion for f in out)
    assert all(f.state == "open" for f in out)


def test_fix_still_applies_returns_replacement():
    rec = _make_finding_record(
        path="a.py", line=2, suggestion="x = 2", message="set x to 2",
    )
    file_text = "x = 1\ny = 2\n"
    assert fix._fix_still_applies(rec, file_text) == "x = 2"


def test_fix_still_applies_none_when_line_out_of_range():
    rec = _make_finding_record(path="a.py", line=99, suggestion="x = 2")
    assert fix._fix_still_applies(rec, "x = 1\n") is None


def test_fix_still_applies_none_when_no_suggestion():
    rec = _make_finding_record(path="a.py", line=1, suggestion=None)
    assert fix._fix_still_applies(rec, "x = 1\n") is None


def _run_fix(**overrides):
    """Helper that always injects a claim so the store is not touched."""
    kwargs = dict(
        install_id=1, owner="o", repo="r", pr_number=2,
        comment_id=5, head_sha="a" * 40, token_fn=lambda fn: fn("tok"),
        claim=lambda k: True,
    )
    kwargs.update(overrides)
    return fix.run_fix(**kwargs)


def test_run_fix_claims_comment_then_no_ops_when_already_handled():
    claimed = []
    res = _run_fix(claim=lambda k: claimed.append(k) or False)
    assert res["status"] == "no_op" and "already handled" in res["reason"]
    assert claimed == ["grugcmd:5"]


def test_run_fix_no_session_skips_and_replies():
    fake_http = _FakeHttp()
    res = _run_fix(
        get_session=lambda *_: None,
        post_reply=lambda b: fake_http.calls.append(("post", b)) or True,
    )
    assert res["status"] == "skip" and res["reason"] == "no_review_session"
    assert any("no review session" in str(c[1]) for c in fake_http.calls)


def test_run_fix_no_committable_findings():
    session = _FakeSession([
        _rec(state="open", suggestion=None),  # no suggestion
        _rec(state="addressed", suggestion="x = 2"),  # not open
    ])
    fake_http = _FakeHttp()
    res = _run_fix(
        get_session=lambda *_: session,
        post_reply=lambda b: fake_http.calls.append(("post", b)) or True,
    )
    assert res["status"] == "no_op" and res["reason"] == "no_committable_findings"
    assert any("no unresolved committable suggestions" in str(c[1]) for c in fake_http.calls)


def test_run_fix_applies_one_commit_and_reports():
    rec = _make_finding_record(path="a.py", line=1, suggestion="x = 2", rule="some-rule")
    session = _FakeSession([rec])

    fetched = {}
    def fetch(token_fn, owner, repo, path, ref):
        fetched["path"] = path
        return "x = 1\ny = 2\n"

    committed = {}
    def apply(token_fn, owner, repo, head_sha, changes, message, pr_number):
        committed["changes"] = changes
        committed["message"] = message
        committed["head_sha"] = head_sha
        return "newhead"

    posted = []
    res = _run_fix(
        get_session=lambda *_: session,
        fetch_file=fetch,
        apply_commit=apply,
        post_reply=lambda b: posted.append(b) or True,
    )
    assert res["status"] == "dispatched"
    assert len(res["applied"]) == 1
    assert res["applied"][0]["path"] == "a.py"
    assert res["applied"][0]["line"] == 1
    assert res["skipped"] == []
    assert committed["changes"] == {"a.py": "x = 2\ny = 2"}
    assert "Apply grug autofix suggestions" in committed["message"]
    assert committed["head_sha"] == "a" * 40
    body = posted[0]
    assert "Applied:" in body and "`a.py:1` [some-rule]" in body
    assert "Skipped: (none)" in body


def test_run_fix_skips_non_applicable_suggestion():
    rec = _make_finding_record(path="a.py", line=1, suggestion="x = 2")
    session = _FakeSession([rec])

    def fetch(token_fn, owner, repo, path, ref):
        return "x = 2\n"  # suggestion equals original -> checked_suggestion returns None

    committed = {}
    def apply(token_fn, owner, repo, head_sha, changes, message, pr_number):
        committed["changes"] = changes
        return "newhead"

    posted = []
    res = _run_fix(
        get_session=lambda *_: session,
        fetch_file=fetch,
        apply_commit=apply,
        post_reply=lambda b: posted.append(b) or True,
    )
    assert res["status"] == "no_op" and res["reason"] == "nothing_applies"
    assert committed == {}  # no commit created
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["path"] == "a.py"
    body = posted[0]
    assert "Applied: (none)" in body
    assert "Skipped:" in body


def test_run_fix_mixed_applied_and_skipped():
    applied_rec = _make_finding_record(path="a.py", line=1, suggestion="x = 2", rule="rule-a")
    skipped_rec = _make_finding_record(path="b.py", line=3, suggestion="y = 2", rule="rule-b")
    session = _FakeSession([applied_rec, skipped_rec])

    def fetch(token_fn, owner, repo, path, ref):
        if path == "a.py":
            return "x = 1\n"  # suggestion differs -> applies
        return "y = 2\n"  # suggestion equals original -> does not apply

    committed = {}
    def apply(token_fn, owner, repo, head_sha, changes, message, pr_number):
        committed["changes"] = changes
        return "newhead"

    posted = []
    res = _run_fix(
        get_session=lambda *_: session,
        fetch_file=fetch,
        apply_commit=apply,
        post_reply=lambda b: posted.append(b) or True,
    )
    assert res["status"] == "dispatched"
    assert len(res["applied"]) == 1 and res["applied"][0]["path"] == "a.py"
    assert len(res["skipped"]) == 1 and res["skipped"][0]["path"] == "b.py"
    assert committed["changes"] == {"a.py": "x = 2"}
    body = posted[0]
    assert "`a.py:1` [rule-a]" in body
    assert "`b.py:3` [rule-b]" in body


def test_run_fix_file_fetch_failure_is_skipped():
    rec = _make_finding_record(path="a.py", line=1, suggestion="x = 2")
    session = _FakeSession([rec])

    def fetch(token_fn, owner, repo, path, ref):
        return None  # cannot read the file

    committed = {}
    def apply(token_fn, owner, repo, head_sha, changes, message, pr_number):
        committed["changes"] = changes
        return "newhead"

    posted = []
    res = _run_fix(
        get_session=lambda *_: session,
        fetch_file=fetch,
        apply_commit=apply,
        post_reply=lambda b: posted.append(b) or True,
    )
    assert res["status"] == "no_op" and res["reason"] == "nothing_applies"
    assert committed == {}  # no commit
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["reason"] == "could not read the file at this head"


def test_run_fix_commit_failure_reports_all_skipped():
    rec = _make_finding_record(path="a.py", line=1, suggestion="x = 2")
    session = _FakeSession([rec])

    def fetch(token_fn, owner, repo, path, ref):
        return "x = 1\n"

    def apply(token_fn, owner, repo, head_sha, changes, message, pr_number):
        return None  # commit failed

    posted = []
    res = _run_fix(
        get_session=lambda *_: session,
        fetch_file=fetch,
        apply_commit=apply,
        post_reply=lambda b: posted.append(b) or True,
    )
    assert res["status"] == "skip" and res["reason"] == "commit_failed"
    assert len(res["skipped"]) == 1  # the candidate is reported as skipped
    body = posted[0]
    assert "Applied: (none)" in body


class _FakeHttp:
    def __init__(self):
        self.calls = []
