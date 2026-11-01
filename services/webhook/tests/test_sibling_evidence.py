"""grug#1530: what earlier merged PRs delivered counts for the ticket's last PR."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.intent_filter import scope_intent_mismatch_to_claimed_boxes
from personas.code_reviewer.lane_work import lane_work_findings
from personas.code_reviewer.linked_issue import (
    build_linked_issue_context, rendered_criteria_with_state,
)
from personas.code_reviewer.persona import Finding
from personas.tpm import issue_fetcher, ticket_compliance_run
from personas.tpm.dor_checks import check_linked_issue_completeness
from personas.tpm.sibling_evidence import SiblingPr, delivered_criteria

_MEASURED = "Count and document the private names reached through `ctx.server` in apiv2"
_ALL = "Repeat the move for all remaining GET endpoints"
_UNRELATED = "Publish the nightly wheelbarrow inventory report to the harbor archive"
_TICKET = (
    "## Why\nThe private names reached through `ctx.server` in apiv2 need counting.\n\n"
    f"## Acceptance criteria\n- [ ] {_MEASURED}\n- [ ] {_ALL}\n"
)
_SIBLING = SiblingPr(
    5,
    "## Why\napiv2's private names through `ctx.server`: 16 names, reached in 17 places.\n",
)
_CLOSES = "## Why\nLast slice.\n\nCloses #9\n"
_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,1 +1,2 @@
 context
+new_line = 1
"""


def _chief(ticket: str, siblings=(_SIBLING,)):
    return check_linked_issue_completeness(
        _CLOSES, fetch_issue=lambda _n: ticket, fetch_siblings=lambda _n: tuple(siblings),
    )


# --- Chief ----------------------------------------------------------------


def test_chief_passes_a_box_a_merged_pr_delivered() -> None:
    result = _chief(f"## Acceptance criteria\n- [ ] {_MEASURED}\n- [x] Done already\n")
    assert result.passed
    assert "criteria met by merged PRs (#9: 1 by #5)" in result.detail


def test_chief_still_fails_a_box_with_no_evidence() -> None:
    result = _chief(f"## Acceptance criteria\n- [ ] {_MEASURED}\n- [ ] {_UNRELATED}\n")
    assert not result.passed
    assert _UNRELATED[:60] in result.detail
    assert _MEASURED[:60] not in result.detail


def test_chief_is_unchanged_for_a_first_or_only_pr() -> None:
    ticket = f"## Acceptance criteria\n- [ ] {_MEASURED}\n"
    assert not _chief(ticket, siblings=()).passed
    assert not check_linked_issue_completeness(
        _CLOSES, fetch_issue=lambda _n: ticket,
    ).passed


def test_chief_read_failure_is_no_evidence() -> None:
    def boom(_n: int):
        raise httpx.ConnectError("down")

    result = check_linked_issue_completeness(
        _CLOSES,
        fetch_issue=lambda _n: f"## Acceptance criteria\n- [ ] {_MEASURED}\n",
        fetch_siblings=boom,
    )
    assert not result.passed


def test_chief_does_not_read_siblings_when_every_box_is_ticked() -> None:
    def never(_n: int):
        raise AssertionError("a ticked ticket needs no read")

    result = check_linked_issue_completeness(
        _CLOSES, fetch_issue=lambda _n: "## Acceptance criteria\n- [x] Done\n",
        fetch_siblings=never,
    )
    assert result.passed


# --- what counts as evidence ----------------------------------------------


@pytest.mark.parametrize("body", [
    f"## Out of scope\n- {_MEASURED}\n",
    f"## Follow-ups\n{_MEASURED}\n",
    f"## Acceptance criteria\n- [ ] {_MEASURED}\n",
    f"> {_MEASURED}\n",
    f"- {_MEASURED}; not built: already in tree\n",
])
def test_a_report_that_does_not_deliver_is_no_evidence(body: str) -> None:
    assert delivered_criteria([_MEASURED], [SiblingPr(5, body)]) == {}


def test_a_line_copied_from_the_ticket_is_no_evidence() -> None:
    line = "The private names reached through `ctx.server` in apiv2 need counting."
    sibling = SiblingPr(5, f"## Why\n{line}\n")
    assert delivered_criteria([_MEASURED], [sibling], _TICKET) == {}
    assert delivered_criteria([_MEASURED], [sibling]) == {_MEASURED: 5}


@pytest.mark.parametrize("criterion", [
    _ALL,
    "No handler reads a private name through `ctx.server` in apiv2",
    "Live: private names reached through `ctx.server` in apiv2",
    "A test pins the line",
])
def test_a_criterion_a_report_cannot_show_stays_open(criterion: str) -> None:
    sibling = SiblingPr(5, f"## Why\n{criterion}\n")
    assert delivered_criteria([criterion], [sibling]) == {}


def test_the_evidence_is_one_pr_not_the_sum_of_several() -> None:
    criterion = "Reader families presence context handler"
    parts = [SiblingPr(5, "Reader and families"), SiblingPr(6, "Presence context")]
    assert delivered_criteria([criterion], parts) == {}


# --- the read -------------------------------------------------------------


def _event(number: int, *, merged: bool = True, repo: str = "repos/owner/name",
           kind: str = "cross-referenced") -> dict:
    return {
        "event": kind,
        "source": {"issue": {
            "number": number,
            "repository_url": f"https://api.github.com/{repo}",
            "pull_request": {"merged_at": "2026-01-01T00:00:00Z" if merged else None},
        }},
    }


def _github(monkeypatch, events: list[dict], prs: dict[int, dict]) -> list[str]:
    """Fake timeline and GraphQL reads; returns the GraphQL queries sent."""
    queries: list[str] = []

    def fake_get(_url, **_kwargs):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: events)

    def fake_post(_url, **kwargs):
        queries.append(kwargs["json"]["query"])
        data = {f"pr{n}": {"number": n, **pr} for n, pr in prs.items()}
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"repository": data}},
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    return queries


_MERGED = "2026-01-01T00:00:00Z"


def _pr(body: str = "Refs #9", edited: str | None = None) -> dict:
    return {"mergedAt": _MERGED, "lastEditedAt": edited, "body": body}


def test_merged_siblings_keeps_only_merged_prs_that_link_the_ticket(monkeypatch) -> None:
    events = [
        _event(1), _event(2, merged=False), _event(3),
        _event(4, repo="repos/owner/other"), _event(7), _event(8, kind="labeled"),
        _event(11),
    ]
    queries = _github(monkeypatch, events, {
        1: _pr(), 3: _pr("Saw #9 once"), 11: _pr("Closes #9"),
    })
    found = issue_fetcher.merged_siblings("t", "owner", "name", 9, pr_number=7)
    assert [pr.number for pr in found] == [1, 11]
    assert len(queries) == 1
    assert "pr2:" not in queries[0] and "pr4:" not in queries[0] and "pr7:" not in queries[0]


def test_a_body_edited_after_the_merge_is_no_evidence(monkeypatch) -> None:
    _github(monkeypatch, [_event(1), _event(2), _event(3)], {
        1: _pr(edited="2026-01-01T00:00:01Z"),
        2: _pr(edited="2025-12-31T23:59:59Z"),
        3: _pr(edited=_MERGED),
    })
    found = issue_fetcher.merged_siblings("t", "owner", "name", 9)
    assert [pr.number for pr in found] == [2, 3]


def test_an_outside_contributors_unedited_pr_still_counts(monkeypatch) -> None:
    """Authorship is not read: the merged body is what must not have moved."""
    _github(monkeypatch, [_event(1)], {1: _pr(f"Refs #9\n\n{_SIBLING.body}")})
    found = issue_fetcher.merged_siblings("t", "owner", "name", 9)
    assert delivered_criteria([_MEASURED], found) == {_MEASURED: 1}


def test_a_graphql_error_is_a_failed_read_not_evidence(monkeypatch) -> None:
    _github(monkeypatch, [_event(1)], {})
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"errors": [{"message": "no"}]},
    ))
    with pytest.raises(RuntimeError):
        issue_fetcher.merged_siblings("t", "owner", "name", 9)


def test_no_merged_candidate_costs_no_graphql_read(monkeypatch) -> None:
    queries = _github(monkeypatch, [_event(2, merged=False)], {})
    assert issue_fetcher.merged_siblings("t", "owner", "name", 9) == ()
    assert queries == []


# --- Elder ----------------------------------------------------------------


def _block(ticket: str, siblings=(_SIBLING,)) -> str:
    return build_linked_issue_context(
        _CLOSES, fetch_issue=lambda _n: ticket, owner="owner", repo="name",
        fetch_siblings=lambda _n: tuple(siblings),
    ) or ""


def _partial(block: str) -> tuple[Finding, ...]:
    findings = lane_work_findings(
        diff_text=_DIFF, hunks=parse_diff(_DIFF), file_contents={},
        base_line_counts={}, boxes=rendered_criteria_with_state(block),
        pr_title="Last slice", pr_body=_CLOSES,
    )
    return tuple(f for f in findings if f.rule_name == "partial-delivery")


def test_elder_marks_a_delivered_criterion_and_tells_the_model() -> None:
    block = _block(_TICKET)
    assert f"#9 criterion 1 [delivered by merged #5]: {_MEASURED}" in block
    assert f"#9 criterion 2 [open]: {_ALL}" in block
    assert "delivered by merged #N" in block
    assert rendered_criteria_with_state(block)[0] == (9, 1, "delivered", _MEASURED)


def test_elder_does_not_report_a_delivered_criterion_as_partial_delivery() -> None:
    ticket = f"## Acceptance criteria\n- [ ] {_MEASURED}\n"
    assert _partial(_block(ticket, siblings=()))
    assert not _partial(_block(ticket))


def test_elder_does_not_report_a_ticked_criterion_as_partial_delivery() -> None:
    assert not _partial(_block(f"## Acceptance criteria\n- [x] {_MEASURED}\n"))


def test_elder_still_reports_a_criterion_with_no_evidence() -> None:
    ticket = f"## Acceptance criteria\n- [ ] {_MEASURED}\n- [ ] {_UNRELATED.replace('report', '`inventory_report`')}\n"
    found = _partial(_block(ticket))
    assert found
    assert "wheelbarrow" in found[0].message
    assert "ctx.server" not in found[0].message


def test_a_first_pr_block_carries_no_note_and_no_read_when_nothing_is_open() -> None:
    def never(_n: int):
        raise AssertionError("no open criterion needs no read")

    block = build_linked_issue_context(
        _CLOSES, fetch_issue=lambda _n: "## Acceptance criteria\n- [x] Done\n",
        fetch_siblings=never,
    ) or ""
    assert "[ticked]" in block
    assert "delivered by merged" not in block


def test_an_intent_mismatch_on_a_delivered_box_stays_advisory_on_the_closing_pr() -> None:
    finding = Finding(
        file="src/x.py", line=2, severity="high", rule_name="intent-mismatch", suggestion=None,
        message=f"#9 criterion 1 is unmet: {_MEASURED}",
    )
    kept, dropped, lowered = scope_intent_mismatch_to_claimed_boxes(
        (finding,), boxes=[(9, 1, "delivered", _MEASURED)],
        closed_issues=[9], pr_body=_CLOSES,
    )
    assert [f.severity for f in kept] == ["medium"]
    assert (dropped, lowered) == (0, 1)


# --- the advisory ---------------------------------------------------------


def test_the_advisory_leaves_out_what_a_merged_pr_delivered(monkeypatch) -> None:
    monkeypatch.setattr(
        issue_fetcher, "merged_siblings", lambda *_a, **_k: (_SIBLING,),
    )
    monkeypatch.setattr(ticket_compliance_run, "_issue_body", lambda *_a: _TICKET)
    found = ticket_compliance_run._collect_unaddressed(
        "t", "owner", "name", [9], set(), 7,
    )
    assert found == {9: [_ALL]}
