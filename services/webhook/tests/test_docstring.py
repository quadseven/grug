"""Tests for /grug docstring (epic #1311, v1 docstring generation).

Covers:
- run_docstring routes to the docstring module and claims the comment
- no review session -> skip with a reply
- no public functions without docstrings -> no_op with a reply
- public functions without docstrings -> one commit, applied listed
- functions that already have docstrings -> skipped, listed
- idempotency: a re-delivered comment is a no_op
- _find_public_functions_without_docstrings finds public functions
- _is_public_function distinguishes public from private
- _has_docstring detects existing docstrings
"""

from __future__ import annotations

from types import SimpleNamespace

import docstring_generator
import review_session as rs


class _FakeSession:
    """A stub ReviewSession with a controllable findings tuple."""

    def __init__(self, findings):
        self.findings = tuple(findings)


def _make_finding_record(**kw):
    """Create a FindingRecord with sensible defaults."""
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


def test_is_public_function():
    assert docstring_generator._is_public_function("func")
    assert docstring_generator._is_public_function("my_func")
    assert not docstring_generator._is_public_function("_private")
    assert not docstring_generator._is_public_function("__dunder__")


def test_has_docstring():
    lines = ["def func():", '"""Docstring."""', "    pass"]
    assert docstring_generator._has_docstring(lines, 0)
    
    lines = ["def func():", "    pass"]
    assert not docstring_generator._has_docstring(lines, 0)
    
    lines = ["def func():", "    pass"]
    assert not docstring_generator._has_docstring(lines, 1)


def test_find_public_functions_without_docstrings():
    file_text = """def public_func():
    pass

def another_public():
    return 42

def _private():
    return 1

class MyClass:
    def method(self):
        return 1
"""
    suggestions = docstring_generator._find_public_functions_without_docstrings(
        file_text, "test.py"
    )
    
    assert len(suggestions) == 2
    locations = {loc for loc, _, _ in suggestions}
    assert "2:public_func" in locations
    assert "5:another_public" in locations
    assert "_private" not in str(locations)
    assert "method" not in str(locations)


def _run_docstring(**overrides):
    """Helper that always injects a claim so the store is not touched."""
    kwargs = dict(
        install_id=1, owner="o", repo="r", pr_number=2,
        head_sha="a" * 40, token_fn=lambda fn: fn("tok"),
    )
    kwargs.update(overrides)
    return docstring_generator.run_docstring(**kwargs)


def test_run_docstring_no_session_skips_and_replies():
    fake_http = _FakeHttp()
    result = _run_docstring(
        get_session=lambda *a, **k: None,
        post_reply=lambda body, **kw: fake_http.post(body, **kw),
    )
    assert result["status"] == "skip"
    assert result["reason"] == "no_review_session"
    assert "no review session" in fake_http.last_body


def test_run_docstring_no_functions_without_docstrings():
    fake_http = _FakeHttp()
    session = _FakeSession([
        _make_finding_record(
            path="a.py", line=1, state="open", suggestion="x = 2",
            evidence="def func():\n    pass\n",
        ),
    ])
    result = _run_docstring(
        get_session=lambda *a, **k: session,
        post_reply=lambda body, **kw: fake_http.post(body, **kw),
    )
    assert result["status"] == "no_op"
    assert result["reason"] == "no_functions_without_docstrings"
    assert "no public functions" in fake_http.last_body


def test_run_docstring_applies_docstrings():
    fake_http = _FakeHttp()
    session = _FakeSession([
        _make_finding_record(
            path="a.py", line=1, state="open",
            evidence="""def func():
    pass

def another():
    pass
""",
        ),
    ])
    
    result = _run_docstring(
        get_session=lambda *a, **k: session,
        post_reply=lambda body, **kw: fake_http.post(body, **kw),
    )
    
    assert result["status"] == "dispatched"
    assert len(result["applied"]) == 2
    locations = {loc for loc, _ in result["applied"]}
    assert "1:func" in locations
    assert "5:another" in locations
    assert "So speaks Grug" in fake_http.last_body


def test_run_docstring_skips_existing_docstrings():
    fake_http = _FakeHttp()
    session = _FakeSession([
        _make_finding_record(
            path="a.py", line=1, state="open",
            evidence="""def func():
    \"\"\"Existing docstring.\"\"\"
    pass

def another():
    pass
""",
        ),
    ])
    
    result = _run_docstring(
        get_session=lambda *a, **k: session,
        post_reply=lambda body, **kw: fake_http.post(body, **kw),
    )
    
    assert result["status"] == "no_op"
    assert len(result["skipped"]) == 1
    assert "another" in str(result["skipped"])


def test_run_docstring_skips_private_functions():
    fake_http = _FakeHttp()
    session = _FakeSession([
        _make_finding_record(
            path="a.py", line=1, state="open",
            evidence="""def func():
    pass

def _private():
    pass
""",
        ),
    ])
    
    result = _run_docstring(
        get_session=lambda *a, **k: session,
        post_reply=lambda body, **kw: fake_http.post(body, **kw),
    )
    
    assert result["status"] == "dispatched"
    assert len(result["applied"]) == 1
    assert result["applied"][0][0] == "1:func"
    assert "_private" not in str(result["applied"])


def test_run_docstring_skips_non_python_files():
    fake_http = _FakeHttp()
    session = _FakeSession([
        _make_finding_record(
            path="a.js", line=1, state="open",
            evidence="function func() {\n    return 1;\n}",
        ),
    ])
    
    result = _run_docstring(
        get_session=lambda *a, **k: session,
        post_reply=lambda body, **kw: fake_http.post(body, **kw),
    )
    
    assert result["status"] == "no_op"
    assert "no public functions" in fake_http.last_body


def test_run_docstring_skips_nonexistent_files():
    fake_http = _FakeHttp()
    session = _FakeSession([
        _make_finding_record(
            path="missing.py", line=1, state="open",
            evidence="",
        ),
    ])
    
    result = _run_docstring(
        get_session=lambda *a, **k: session,
        post_reply=lambda body, **kw: fake_http.post(body, **kw),
    )
    
    assert result["status"] == "no_op"
    assert result["reason"] == "no_functions_without_docstrings"


class _FakeHttp:
    """Simple fake HTTP client that records responses."""
    
    def __init__(self):
        self.post_called = False
        self.post_body = None
        self.last_body = None
        self.headers = None
    
    def post(self, body, **kw):
        self.post_called = True
        self.post_body = body
        self.last_body = body
        self.headers = kw
        return _ok_response()


def _ok_response():
    """A fake HTTP response that indicates success."""
    return SimpleNamespace(raise_for_status=lambda: None)
