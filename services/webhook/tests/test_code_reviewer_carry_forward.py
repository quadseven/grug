"""Elder's check must account for its own earlier findings that still stand.

A delta review of a push that touches other lines finds nothing, but the
earlier inline threads on untouched code are still open. Without carrying
them, the check went green over unaddressed blocking findings.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas.code_reviewer import carry_forward as cf
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.dedup import rule_marker
from personas.code_reviewer.persona import Finding

_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def _comment(*, severity="🔥 high", rule="null-deref", path="a.py", line=10,
             author="grug-tribe", minimized=False, url="https://example.test/t1"):
    return {
        "id": "C1", "databaseId": 1, "isMinimized": minimized, "url": url,
        "author": {"login": author}, "path": path, "line": line,
        "originalLine": line, "originalCommit": {"oid": "c0ffee"},
        "body": f"{severity} | _correctness_ | `{rule}`\n\n**What Elder sees**\n\nx\n\n"
                + rule_marker(rule),
    }


def _thread(*, outdated=False, resolved=False, comments=None, tid="T1", **kw):
    nodes = comments if comments is not None else [_comment(**kw)]
    return {
        "id": tid, "isResolved": resolved, "isOutdated": outdated,
        "comments": {"totalCount": len(nodes), "nodes": nodes},
    }


def _finding(file="a.py", line=10, rule="null-deref") -> Finding:
    return Finding(file=file, line=line, severity="high", rule_name=rule,
                   message="m", suggestion=None)


# --- pure selection -------------------------------------------------------

def test_open_unchanged_high_thread_is_carried_with_parsed_severity():
    out = cf.select_carried([_thread()], ())
    assert [(c.path, c.line, c.rule, c.severity, c.blocking) for c in out] == [
        ("a.py", 10, "null-deref", "high", True)]
    assert out[0].url == "https://example.test/t1"


@pytest.mark.parametrize("chip,sev", [
    ("💀 critical", "critical"), ("🟠 medium", "medium"), ("👁 low", "low"),
])
def test_severity_is_read_from_every_chip(chip, sev):
    assert cf.select_carried([_thread(severity=chip)], ())[0].severity == sev


@pytest.mark.parametrize("thread", [
    _thread(outdated=True),
    _thread(resolved=True),
    _thread(minimized=True),
    _thread(comments=[_comment(), _comment(author="alice")]),
    _thread(author="alice"),
    _thread(severity="mystery"),
])
def test_threads_that_no_longer_stand_are_not_carried(thread):
    assert cf.select_carried([thread], ()) == ()


def test_reraised_finding_is_not_double_counted():
    assert cf.select_carried([_thread()], (_finding(line=14),)) == ()
    # a different rule, a different file, or a far line is a separate finding
    assert len(cf.select_carried([_thread()], (_finding(rule="other"),))) == 1
    assert len(cf.select_carried([_thread()], (_finding(file="b.py"),))) == 1
    assert len(cf.select_carried([_thread()], (_finding(line=90),))) == 1


# --- verdict plumbing -----------------------------------------------------

def _carried(*sevs):
    return tuple(cf.CarriedFinding("a.py", i + 1, "r", s, "") for i, s in enumerate(sevs))


def test_high_carried_in_blocking_mode_fails_a_clean_title_and_lists_it():
    concl, title, summary = cf.apply_to_check(
        _carried("high", "low"), conclusion="success",
        title="Living Hunt aaa..bbb - Elder clear - no markings",
        summary="## Markings Board\n\nLiving Hunt: reviewing `aaa..bbb`",
        blocking_mode=True)
    assert concl == "failure"
    assert title == ("Living Hunt aaa..bbb - Elder: 2 earlier finding(s) "
                     "still open (1 high/critical)")
    assert "Living Hunt: reviewing `aaa..bbb`" in summary
    assert "`a.py:1` `r` high" in summary


def test_medium_and_low_carried_never_change_the_conclusion():
    concl, title, _ = cf.apply_to_check(
        _carried("medium", "low"), conclusion="success",
        title="Elder clear - no markings", summary="s", blocking_mode=True)
    assert concl == "success"
    assert title == "Elder: 2 earlier finding(s) still open (0 high/critical)"


def test_advisory_mode_keeps_its_conclusion():
    concl, _, _ = cf.apply_to_check(
        _carried("critical"), conclusion="neutral", title="t", summary="s",
        blocking_mode=False)
    assert concl == "neutral"


def test_fresh_findings_title_gets_a_suffix():
    _, title, _ = cf.apply_to_check(
        _carried("high"), conclusion="failure",
        title="Elder markings - 1 blocking, 1 total", summary="s",
        blocking_mode=True)
    assert title == ("Elder markings - 1 blocking, 1 total; 1 earlier "
                     "finding(s) still open (1 high/critical)")


def test_summary_list_is_bounded_to_ten_lines():
    _, _, summary = cf.apply_to_check(
        _carried(*["high"] * 14), conclusion="success", title="Elder clear - x",
        summary="s", blocking_mode=True)
    assert summary.count("\n- `a.py:") == 10
    assert "and 4 more" in summary


def test_nothing_carried_changes_nothing():
    assert cf.apply_to_check((), conclusion="success", title="t", summary="s",
                             blocking_mode=True) == ("success", "t", "s")


def test_listing_failure_fails_open_and_logs(monkeypatch, caplog):
    def boom(*a, **kw):
        raise httpx.ConnectError("x")
    monkeypatch.setattr(cf, "with_install_token_retry", boom)
    with caplog.at_level("WARNING"):
        assert cf.list_threads(1, "o", "r", 2) is None
    rec = [r for r in caplog.records if r.message == "elder_carry_forward_failed"]
    assert rec and rec[0].kind == "ConnectError"


# --- through dispatch_code_review -----------------------------------------

def _payload(action="synchronize"):
    return {
        "action": action, "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {
            "number": 7, "head": {"sha": "abcd1234efgh"},
            "base": {"sha": "base5678ijkl"}, "title": "t", "body": "b",
            "user": {"login": "alice"},
        },
    }


def _run(monkeypatch, *, blocking, threads, action="synchronize", llm=None):
    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda install_id, fn: fn("tok"))
    llm = llm or LlmReviewResponse(kind="reviewed", findings=(),
                                   backend_used=Backend.POOLSIDE)
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
    checks: list = []
    monkeypatch.setattr(cr_dispatch, "post_check_run",
                        lambda tok, o, r, res, **kw: checks.append(res) or {})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {})
    listings: list = []

    def list_threads(*a, **kw):
        listings.append(a)
        if isinstance(threads, Exception):
            raise threads
        return threads
    monkeypatch.setattr(cr_dispatch.carry_forward, "list_threads", list_threads)
    seen: list = []
    monkeypatch.setattr(cr_dispatch, "resolve_fixed_threads",
                        lambda *a, **kw: seen.append(kw) or 0)

    def get(url, **kw):
        if "/comments" in url:
            r = MagicMock(spec=httpx.Response)
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value=[])
            return r
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.text = _DIFF
        return r

    with patch("httpx.get", side_effect=get):
        cr_dispatch.dispatch_code_review(_payload(action), blocking=blocking)
    return checks[-1], listings, seen


def test_clean_delta_over_open_high_thread_fails_in_blocking_mode(monkeypatch):
    check, listings, _ = _run(monkeypatch, blocking=True, threads=[_thread()])
    assert check.conclusion == "failure"
    assert check.title == "Elder: 1 earlier finding(s) still open (1 high/critical)"
    assert "`a.py:10` `null-deref` high" in check.summary
    assert "https://example.test/t1" in check.summary
    assert len(listings) == 1


def test_advisory_mode_keeps_neutral_but_still_reports(monkeypatch):
    check, _, _ = _run(monkeypatch, blocking=False, threads=[_thread()])
    assert check.conclusion == "neutral"
    assert "1 earlier finding(s) still open" in check.title


def test_medium_carried_leaves_a_clean_check_green(monkeypatch):
    check, _, _ = _run(monkeypatch, blocking=True,
                       threads=[_thread(severity="🟠 medium")])
    assert check.conclusion == "success"


def test_outdated_thread_does_not_block(monkeypatch):
    check, _, _ = _run(monkeypatch, blocking=True, threads=[_thread(outdated=True)])
    assert check.conclusion == "success"
    assert "Elder clear" in check.title


def test_listing_failure_publishes_as_before(monkeypatch):
    # list_threads itself swallows errors; simulate its documented None.
    check, listings, seen = _run(monkeypatch, blocking=True, threads=None)
    assert check.conclusion == "success"
    assert "Elder clear" in check.title
    assert seen[0]["threads"] is None


def test_first_review_does_not_list(monkeypatch):
    check, listings, _ = _run(monkeypatch, blocking=True, threads=[_thread()],
                              action="opened")
    assert listings == []
    assert check.conclusion == "success"


def test_the_one_listing_is_shared_with_the_resolver(monkeypatch):
    threads = [_thread()]
    _, listings, seen = _run(monkeypatch, blocking=True, threads=threads)
    assert len(listings) == 1
    assert seen[0]["threads"] is threads


def test_reraised_finding_counts_once(monkeypatch):
    llm = LlmReviewResponse(
        kind="reviewed", backend_used=Backend.POOLSIDE,
        findings=(LlmFinding(path="src/x.py", line=2, rule="null-deref",
                             severity="high", message="m"),))  # type: ignore[arg-type]
    thread = _thread(path="src/x.py", line=2)
    check, _, _ = _run(monkeypatch, blocking=True, threads=[thread], llm=llm)
    assert check.conclusion == "failure"
    assert "earlier finding" not in check.title
