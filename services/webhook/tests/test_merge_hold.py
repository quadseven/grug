"""Merge holds the change ignores (grug#1766)."""
from __future__ import annotations

from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.merge_hold import RULE, Hold, find_holds, hold_findings

_DIFF = (
    "diff --git a/ca.py b/ca.py\n"
    "--- a/ca.py\n+++ b/ca.py\n"
    "@@ -1,2 +1,2 @@\n"
    " import os\n"
    "-DAYS = 90\n"
    "+DAYS = 21\n"
)

# Qualified references are joined at parse time: keeps a literal
# cross-repository reference out of the source.
_SAME = "example/repo" "#"
_FOREIGN = "other/repo" "#"

_BODY = f"""## Intent
- Certificates are now issued for 21 days instead of 90.
- Merge hold: issue #16 says this is safe only once renewal is proven on a handset (#22, still open).

## Why
This is blocked on proving renewal in issue #22, and blocked on #13 being proven on hardware.

## Out of scope
- Issue #40 remains blocked until that is completed with live evidence.

Refs {_SAME}160
"""


def _holds(body: str = _BODY) -> tuple[Hold, ...]:
    return find_holds(body, source="the PR body", owner="example", repo="repo")


def test_finds_hold_sentences_and_the_issues_they_wait_on():
    holds = _holds()
    assert [h.refs for h in holds] == [(16, 22), (22, 13)]
    assert holds[0].quote.startswith("Merge hold: issue #16 says")
    assert all(h.source == "the PR body" for h in holds)


def test_out_of_scope_checked_boxes_and_released_holds_are_not_holds():
    body = (
        "- [x] Do not merge until #5 closes\n"
        "No longer blocked by #6.\n"
        "## Non-goals\n"
        "Blocked by #7.\n"
        "```\nblocked by #8\n```\n"
    )
    assert _holds(body) == ()


def test_a_hold_with_no_issue_cited_or_a_foreign_repo_is_skipped():
    body = (
        "Do not merge until the hardware test passes.\n"
        f"Blocked by {_FOREIGN}9.\n"
    )
    assert _holds(body) == ()


def test_a_blocked_by_heading_holds_each_line_under_it():
    body = f"## Blocked by\n- #31\n- {_SAME}32\n## Summary\n- see #33\n"
    assert [h.refs for h in _holds(body)] == [(31,), (32,)]


def test_an_open_cited_issue_gives_one_blocking_finding_quoting_the_hold():
    hunks = parse_diff(_DIFF)
    found = hold_findings(_holds(), frozenset({22}), hunks)
    assert [(f.file, f.line, f.severity, f.rule_name) for f in found] == [
        ("ca.py", 2, "high", RULE), ("ca.py", 2, "high", RULE),
    ]
    assert '"Merge hold: issue #16 says' in found[0].message
    assert "#22 is still open" in found[0].message


def test_holds_whose_issues_are_all_closed_give_nothing():
    hunks = parse_diff(_DIFF)
    assert hold_findings(_holds(), frozenset(), hunks) == ()


def test_a_huge_run_of_name_characters_is_scanned_in_linear_time():
    """A 200 KB run of owner-like characters on a hold line must not make
    the reference scan backtrack from every start position."""
    import time

    body = "Blocked by " + "a-" * 100_000 + " #5"
    started = time.monotonic()
    find_holds(body, source="the PR body", owner="example", repo="repo")
    assert time.monotonic() - started < 0.5


def test_no_added_line_gives_nothing():
    assert hold_findings(_holds(), frozenset({22}), ()) == ()


def _review(body, issues, reads=None):
    """One pass through dispatch with GitHub faked at the issue read.
    `issues` maps number to (state, body); a missing number raises 404."""
    from unittest.mock import patch

    import httpx

    from personas.code_reviewer import dispatch
    from personas.code_reviewer.persona import CodeReviewEvaluation

    context = {
        "repo": "example/repo", "pr_number": 170, "installation_id": 7,
        "head_sha": "abc123", "title": "Shorten certificates", "body": body,
    }

    def read_issue(number):
        if reads is not None:
            reads.append(number)
        if number not in issues:
            request = httpx.Request("GET", "https://api.github.com/x")
            raise httpx.HTTPStatusError(
                "absent", request=request, response=httpx.Response(404, request=request),
            )
        state, text = issues[number]
        return {"title": "t", "body": text, "state": state}

    with patch.object(dispatch, "build_issue_details_fetcher", return_value=read_issue):
        dispatch._attach_linked_issue_context(
            context, installation_id=7, owner="example", repo_name="repo",
        )
    clean = CodeReviewEvaluation(findings=(), conclusion="success")
    return dispatch._merge_open_merge_holds(clean, parse_diff(_DIFF), context)


def test_dispatch_blocks_a_pr_whose_body_holds_on_an_open_issue():
    merged = _review(_BODY, {
        160: ("open", "Shorten certificate life."), 16: ("closed", ""),
        22: ("open", ""), 13: ("closed", ""),
    })
    assert [f.rule_name for f in merged.findings] == [RULE, RULE]
    assert merged.conclusion == "failure"
    assert all('", and #22 is still open.' in f.message for f in merged.findings)


def test_dispatch_reads_holds_from_the_linked_ticket():
    ticket = "Blocked by #22: renewal must be proven first."
    merged = _review("Refs #160", {160: ("open", ticket), 22: ("open", "")})
    assert len(merged.findings) == 1
    assert "ticket #160 says" in merged.findings[0].message


def test_dispatch_passes_when_every_held_issue_is_closed_or_unreadable():
    merged = _review(_BODY, {160: ("open", ""), 22: ("closed", ""), 16: ("closed", "")})
    assert merged.findings == ()
    assert merged.conclusion == "success"


def test_dispatch_skips_the_tickets_the_pr_links():
    """The linked ticket is the work itself, open until this PR lands."""
    body = "Refs #22\n\nDo not merge until #22 is fixed."
    merged = _review(body, {22: ("open", "")})
    assert merged.findings == ()
