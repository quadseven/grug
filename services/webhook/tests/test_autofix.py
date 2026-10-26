"""Tests for /grug fix (epic #1311, v1 autofix).

Covers:
- run_fix routes to the autofix module and claims the comment
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

import autofix
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
    out = autofix._open_committable_findings(session)
    assert len(out) == 2
    assert all(f.suggestion for f in out)
    assert all(f.state == "open" for f in out)


def test_fix_still_applies_returns_replacement():
    rec = _make_finding_record(
        path="a.py", line=2, suggestion="x = 2", message="set x to 2",
    )
    file_text = "x = 1\ny = 2\n"
    assert autofix._fix_still_applies(rec, file_text) == "x = 2"


def test_fix_still_applies_none_when_line_out_of_range():
    rec = _make_finding_record(path="a.py", line=99, suggestion="x = 2")
    assert autofix._fix_still_applies(rec, "x = 1\n") is None


def test_fix_still_applies_none_when_no_suggestion():
    rec = _make_finding_record(path="a.py", line=1, suggestion=None)
    assert autofix._fix_still_applies(rec, "x = 1\n") is None


def _run_fix(**overrides):
    """Helper that always injects a claim so the store is not touched."""
    kwargs = dict(
        install_id=1, owner="o", repo="r", pr_number=2,
        head_sha="a" * 40, token_fn=lambda fn: fn("tok"),
    )
    kwargs.update(overrides)
    return autofix.run_fix(**kwargs)


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
    assert committed["changes"] == {"a.py": "x = 2\ny = 2\n"}
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
    assert committed["changes"] == {"a.py": "x = 2\n"}
    body = posted[0]
    assert "`a.py:1` [rule-a]" in body
    assert "`b.py:3` [rule-b]" in body


def test_run_fix_preserves_multiple_fixes_in_same_file():
    first = _make_finding_record(path="a.py", line=1, suggestion="x = 2", rule="r1")
    second = _make_finding_record(path="a.py", line=2, suggestion="y = 3", rule="r2")
    committed = {}
    res = _run_fix(
        get_session=lambda *_: _FakeSession([first, second]),
        fetch_file=lambda *args: "x = 1\ny = 2\nz = 4\n",
        apply_commit=lambda *args: committed.setdefault("changes", args[4]) or "newhead",
        post_reply=lambda body: True,
    )
    assert res["status"] == "dispatched"
    assert committed["changes"] == {"a.py": "x = 2\ny = 3\nz = 4\n"}


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
    rejected = _make_finding_record(path="a.py", line=1, suggestion="x = 2")
    valid = _make_finding_record(path="b.py", line=1, suggestion="y = 2")
    session = _FakeSession([rejected, valid])

    def fetch(token_fn, owner, repo, path, ref):
        return "x = 2\n" if path == "a.py" else "y = 1\n"

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
    assert len(res["applied"]) == 0
    assert len(res["skipped"]) == 2
    assert res["skipped"][0]["reason"] == "suggestion no longer applies cleanly"
    assert res["skipped"][1]["reason"] == "commit could not be created"
    body = posted[0]
    assert "Applied: (none)" in body
    assert "suggestion no longer applies cleanly" in body
    assert "commit could not be created" in body


class _FakeHttp:
    def __init__(self):
        self.calls = []


# --- the Git Data API commit sequence ---------------------------------------


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_req(pr, tree_entries, calls, truncated=False):
    """A `_req` seam answering the Git Data API calls `_do_commit` makes."""
    def _req(token, method, url, json=None):
        calls.append((method, url, json))
        if method == "GET" and "/pulls/" in url:
            return _Resp(pr)
        if method == "GET" and "/git/commits/" in url:
            return _Resp({"tree": {"sha": "basetree"}})
        if method == "GET" and "/git/trees/" in url:
            return _Resp({"tree": tree_entries, "truncated": truncated})
        if url.endswith("/git/blobs"):
            return _Resp({"sha": "blob1"})
        if url.endswith("/git/trees"):
            return _Resp({"sha": "newtree"})
        if url.endswith("/git/commits"):
            return _Resp({"sha": "newcommit"})
        return _Resp({})
    return _req


def _pr(full_name="o/r", sha="a" * 40, ref="feature"):
    return {"head": {"ref": ref, "sha": sha, "repo": {"full_name": full_name}}}


def test_do_commit_refuses_a_fork_pr_and_never_moves_a_base_branch():
    """A fork PR's head ref names a branch in the FORK. Updating
    `heads/<ref>` in the base repo would move the base repo's own branch of
    that name (a fork PR from `main` would fast-forward the base `main`)."""
    calls = []
    try:
        autofix._do_commit(
            "t", "o", "r", "a" * 40, {"a.py": "x = 2\n"}, "msg", 9,
            _fake_req(_pr(full_name="stranger/r", ref="main"), [], calls),
        )
    except autofix.CommitRefused as e:
        assert "fork" in str(e)
    else:
        raise AssertionError("fork PR was not refused")
    assert all(method == "GET" for method, _, _ in calls)


def test_do_commit_refuses_when_the_head_moved():
    calls = []
    try:
        autofix._do_commit(
            "t", "o", "r", "a" * 40, {"a.py": "x = 2\n"}, "msg", 9,
            _fake_req(_pr(sha="b" * 40), [], calls),
        )
    except autofix.CommitRefused as e:
        assert "moved" in str(e)
    else:
        raise AssertionError("stale head was not refused")
    assert all(method == "GET" for method, _, _ in calls)


def test_do_commit_keeps_the_file_mode_and_attributes_the_bot():
    calls = []
    new = autofix._do_commit(
        "t", "o", "r", "a" * 40, {"bin/run.sh": "echo 2\n"}, "msg", 9,
        _fake_req(_pr(), [{"path": "bin/run.sh", "mode": "100755",
                           "type": "blob"}], calls),
    )
    assert new == "newcommit"
    tree_post = next(j for m, u, j in calls if m == "POST" and u.endswith("/git/trees"))
    assert tree_post["tree"][0]["mode"] == "100755"
    commit_post = next(j for m, u, j in calls if m == "POST" and u.endswith("/git/commits"))
    assert commit_post["author"]["name"] == "grug-tribe[bot]"
    assert commit_post["parents"] == ["a" * 40]
    ref_patch = next((u, j) for m, u, j in calls if m == "PATCH")
    assert ref_patch[0].endswith("/git/refs/heads/feature")
    assert ref_patch[1] == {"sha": "newcommit", "force": False}


def test_run_fix_reports_a_refused_commit_reason():
    rec = _make_finding_record(path="a.py", line=1, suggestion="x = 2")

    def apply(*_args):
        raise autofix.CommitRefused("this PR comes from a fork")

    posted = []
    res = _run_fix(
        get_session=lambda *_: _FakeSession([rec]),
        fetch_file=lambda *_a: "x = 1\n",
        apply_commit=apply,
        post_reply=lambda b: posted.append(b) or True,
    )
    assert res["status"] == "skip" and res["reason"] == "commit_failed"
    assert res["skipped"][0]["reason"] == "this PR comes from a fork"
    assert "this PR comes from a fork" in posted[0]


def test_run_fix_reads_each_file_once():
    first = _make_finding_record(path="a.py", line=1, suggestion="x = 2", rule="r1")
    second = _make_finding_record(path="a.py", line=2, suggestion="y = 3", rule="r2")
    reads = []

    def fetch(token_fn, owner, repo, path, ref):
        reads.append(path)
        return "x = 1\ny = 2\n"

    _run_fix(
        get_session=lambda *_: _FakeSession([first, second]),
        fetch_file=fetch, apply_commit=lambda *a: "newhead",
        post_reply=lambda b: True,
    )
    assert reads == ["a.py"]
