"""Chief's issue-time DoR advisory (grug's first non-PR review surface).

Two things these tests exist to pin, beyond the happy path:

  1. The PR-shaped checks (`issue_link`, `linked_issue_completeness`) must
     NEVER run at issue time. Including them would nag about something
     structurally impossible on an issue, which trains people to ignore
     Chief entirely.
  2. A ticket that is ready from the start gets NO comment. A
     congratulation is a notification with zero information, and the
     surface only earns its keep if silence is the common case.
"""
from __future__ import annotations

import httpx

from personas.tpm import issue_dor as idor

_GOOD = "\n".join([
    "## Why",
    "The poller drops every third delivery and nobody notices for days.",
    "## Acceptance criteria",
    "- Deliveries are counted",
    "- A drop emits a warning",
    "- A test proves the counter moves",
    "## Out of scope",
    "- Rewriting the poller",
    "Size: M",
])

_BAD = "please fix the poller"


# --- pure evaluation -----------------------------------------------------

def test_good_issue_passes_every_issue_check():
    assert [r.passed for r in idor.evaluate_issue(_GOOD)] == [True] * 4


def test_bare_issue_fails_every_issue_check():
    assert [r.passed for r in idor.evaluate_issue(_BAD)] == [False] * 4


def test_pr_shaped_checks_are_not_run_at_issue_time():
    """`issue_link` wants `closes #N` and `linked_issue_completeness` walks
    a PR's linked issues - neither is satisfiable on an issue. A ticket with
    no `closes #N` must still be able to pass."""
    names = {r.name for r in idor.evaluate_issue(_GOOD)}
    assert names == {"why", "acceptance", "estimate", "scope-fence"}
    assert "issue-link" not in names
    assert "linked-issue-completeness" not in names
    assert "closes #" not in _GOOD.lower()  # and it still passed above


def test_empty_body_does_not_crash():
    assert all(not r.passed for r in idor.evaluate_issue(""))


# --- rendering -----------------------------------------------------------

def test_advisory_is_none_when_ready():
    assert idor.advisory_markdown(idor.evaluate_issue(_GOOD)) is None


def test_advisory_names_each_gap_and_a_remedy():
    md = idor.advisory_markdown(idor.evaluate_issue(_BAD))
    assert md is not None
    for name in ("why", "acceptance", "estimate", "scope-fence"):
        assert f"`{name}`" in md
    # every failing row carries an actionable remedy, not just a complaint
    assert "Size: XS|S|M|L" in md
    assert "## Out of scope" in md
    assert idor._MARKER in md


def test_advisory_credits_what_already_passes():
    """Partial credit matters: a ticket with a good Why should be told so,
    or the advisory reads as 'start over' and gets ignored."""
    partial = "## Why\nThe poller drops deliveries and nobody notices.\n"
    md = idor.advisory_markdown(idor.evaluate_issue(partial))
    assert md is not None and "Already good: `why`" in md


def test_ready_markdown_carries_the_marker():
    assert idor._MARKER in idor.ready_markdown()


# --- run loop ------------------------------------------------------------

def _wire(monkeypatch, *, existing=None):
    calls = []
    monkeypatch.setattr(idor, "_existing_comment", lambda t, o, r, n: existing)
    monkeypatch.setattr(
        idor.httpx, "post",
        lambda url, **kw: calls.append(("post", url, kw.get("json", {}))) or httpx.Response(
            201, json={"id": 1}, request=httpx.Request("POST", url)),
    )
    monkeypatch.setattr(
        idor.httpx, "patch",
        lambda url, **kw: calls.append(("patch", url, kw.get("json", {}))) or httpx.Response(
            200, json={}, request=httpx.Request("PATCH", url)),
    )
    return calls


def test_ready_issue_with_no_prior_comment_says_nothing(monkeypatch):
    calls = _wire(monkeypatch)
    out = idor.run_issue_dor("tok", "o", "r", 7, _GOOD)
    assert calls == []
    assert out["status"] == "no_op"


def test_gappy_issue_posts_one_advisory(monkeypatch):
    calls = _wire(monkeypatch)
    out = idor.run_issue_dor("tok", "o", "r", 7, _BAD)
    assert out["status"] == "ok"
    assert len(calls) == 1
    verb, url, body = calls[0]
    assert verb == "post" and url.endswith("/issues/7/comments")
    assert idor._MARKER in body["body"]


def test_edit_refreshes_the_same_comment_never_a_second(monkeypatch):
    """The `edited` event is what makes this self-healing - and what would
    make it a spam machine if it appended. Five saves must yield one
    comment."""
    calls = _wire(monkeypatch, existing=42)
    idor.run_issue_dor("tok", "o", "r", 7, _BAD)
    assert len(calls) == 1
    verb, url, _ = calls[0]
    assert verb == "patch" and url.endswith("/issues/7/comments/42")


def test_fixed_issue_clears_the_advisory_in_place(monkeypatch):
    calls = _wire(monkeypatch, existing=42)
    out = idor.run_issue_dor("tok", "o", "r", 7, _GOOD)
    assert out["status"] == "ok" and "cleared" in out["reason"]
    verb, url, body = calls[0]
    assert verb == "patch" and url.endswith("/issues/7/comments/42")
    assert "ready for hunt" in body["body"]


def test_existing_comment_is_found_by_marker_not_author(monkeypatch):
    """Author-matching breaks the moment the App is renamed or a different
    installation identity posts; the body marker is the identity."""
    payload = [
        {"id": 1, "body": "unrelated human comment"},
        {"id": 2, "body": f"some text\n{idor._MARKER}"},
    ]
    monkeypatch.setattr(
        idor.httpx, "get",
        lambda url, **kw: httpx.Response(
            200, json=payload, request=httpx.Request("GET", url)),
    )
    assert idor._existing_comment("tok", "o", "r", 7) == 2


def test_no_marker_means_no_existing_comment(monkeypatch):
    monkeypatch.setattr(
        idor.httpx, "get",
        lambda url, **kw: httpx.Response(
            200, json=[{"id": 1, "body": "just a human"}],
            request=httpx.Request("GET", url)),
    )
    assert idor._existing_comment("tok", "o", "r", 7) is None


# --- the epic line (grug#1035) ------------------------------------------------

from personas.tpm.dor_checks import IssueFacts  # noqa: E402


def _facts(**over):
    base = dict(number=7, title="a ticket", body="", labels=(), sub_issue_count=0,
                parent_number=None, is_pull_request=False)
    base.update(over)
    return IssueFacts(**base)


def test_a_ready_ticket_with_no_epic_gets_an_advisory_naming_it(monkeypatch):
    calls = _wire(monkeypatch)
    out = idor.run_issue_dor("tok", "o", "r", 7, _GOOD, fetch_facts=lambda n: _facts())
    assert out["status"] == "ok" and len(calls) == 1
    body = calls[0][2]["body"]
    assert "`epic`" in body and "sub-issue of an epic" in body


def test_a_ticket_in_an_epic_stays_silent(monkeypatch):
    calls = _wire(monkeypatch)
    for facts in (_facts(parent_number=3), _facts(body="Part of #3"),
                  _facts(title="Epic: a root"), _facts(labels=("epic",)), _facts(sub_issue_count=2)):
        out = idor.run_issue_dor("tok", "o", "r", 7, _GOOD, fetch_facts=lambda n, f=facts: f)
        assert out["status"] == "no_op", facts
    assert calls == []


def test_linking_the_epic_clears_the_advisory_in_place(monkeypatch):
    calls = _wire(monkeypatch, existing=42)
    out = idor.run_issue_dor("tok", "o", "r", 7, _GOOD, fetch_facts=lambda n: _facts(parent_number=3))
    assert "cleared" in out["reason"]
    assert calls[0][0] == "patch" and calls[0][1].endswith("/comments/42")


def test_a_fetch_failure_adds_no_epic_line(monkeypatch):
    """Advisory surface: a GitHub blip must not become a nag."""
    calls = _wire(monkeypatch)

    def boom(n):
        raise httpx.ConnectError("down")

    out = idor.run_issue_dor("tok", "o", "r", 7, _GOOD, fetch_facts=boom)
    assert out["status"] == "no_op" and calls == []


def test_refresh_only_never_starts_a_comment(monkeypatch):
    """A link event on an old, gappy issue must not post a fresh lecture."""
    calls = _wire(monkeypatch)
    out = idor.run_issue_dor("tok", "o", "r", 7, _BAD,
                             fetch_facts=lambda n: _facts(), refresh_only=True)
    assert out["status"] == "no_op" and calls == []


def test_refresh_only_still_updates_an_existing_advisory(monkeypatch):
    calls = _wire(monkeypatch, existing=42)
    idor.run_issue_dor("tok", "o", "r", 7, _GOOD,
                       fetch_facts=lambda n: _facts(), refresh_only=True)
    assert calls and calls[0][0] == "patch"
