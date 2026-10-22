"""Tests for partial delivery (#1389): every in-scope criterion needs evidence.

Recorded shape (anonymized): a lane PR on a ticket with three criteria
flipped one socket option for the first and had nothing for the other two;
the station closed the ticket on merge.
"""

from __future__ import annotations

import httpx
from personas.code_reviewer import criteria_judge
from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.lane_work import lane_work_findings
from personas.code_reviewer.partial_delivery import (
    RULE,
    criteria_evidence,
    scan_partial_delivery,
)

_FOOTER = "\n---\nLoom ticket example.link-8-r1, lane codex, model coder.\nRefs #8\n"


def _changed(path: str, old: list[str], new: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\nindex 2222222..3333333 100644\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1,{len(old)} +1,{len(old) + len(new)} @@\n"
        + "".join(f" {ln}\n" for ln in old)
        + "".join(f"+{ln}\n" for ln in new)
    )


_SOCKET = _changed(
    "bonder/transport.py", ["def open_socket(sock):"], ["    sock.setsockopt(SO_REUSEPORT, 1)"],
)
_CRITERIA = (
    LinkedCriterion(8, 1, "A second instance cannot bind: `open_socket` sets `SO_REUSEPORT` off."),
    LinkedCriterion(8, 2, "An idle leg sleeps after `LEG_IDLE_SECONDS` and `wake_leg()` resumes it."),
    LinkedCriterion(8, 3, "A unit test covers the leg sleep and wake."),
)


def _scan(diff: str, criteria=_CRITERIA, judge=None):
    return scan_partial_delivery(criteria, file_changes(diff), parse_diff(diff), judge=judge)


def test_most_criteria_without_evidence_is_named_and_asked_not_blocked():
    found = _scan(_SOCKET)
    # Medium even when most lack evidence: a high blocked good held-out PRs.
    assert [(f.rule_name, f.severity) for f in found] == [(RULE, "medium")]
    message = found[0].message
    assert "2 of the 3" in message and "most of the ticket" in message
    assert "#8 criterion 2" in message and "#8 criterion 3" in message
    assert "#8 criterion 1" not in message
    assert found[0].file == "bonder/transport.py"


def test_one_criterion_without_evidence_is_medium():
    diff = _SOCKET + _changed(
        "bonder/legs.py", ["class Leg:"], ["    def wake_leg(self):", "        self.asleep = False"],
    )
    found = _scan(diff)
    assert [(f.rule_name, f.severity) for f in found] == [(RULE, "medium")]
    assert "#8 criterion 3" in found[0].message and "most of the ticket" not in found[0].message


def test_every_criterion_with_evidence_draws_nothing():
    diff = _SOCKET + _changed(
        "bonder/legs.py", ["class Leg:"], ["    LEG_IDLE_SECONDS = 30", "    def wake_leg(self):"],
    ) + _changed("tests/test_legs.py", ["import legs"], ["def test_sleep_and_wake():"])
    assert _scan(diff) == ()


def test_live_operator_and_process_criteria_are_never_reported():
    criteria = (
        LinkedCriterion(8, 1, "Live: throughput across both legs exceeds the best single leg."),
        LinkedCriterion(8, 2, "Measured on the router, the failover takes under two seconds."),
        LinkedCriterion(8, 3, "The operator enrolls the device by hand in the vendor console."),
        LinkedCriterion(8, 4, "The pull request's checks pass and every review thread is answered."),
        LinkedCriterion(8, 5, "Confirm the `plan-check-failed` invariant no longer triggers."),
        LinkedCriterion(8, 6, "Existing `LegPool` assertions and coverage are preserved."),
        LinkedCriterion(8, 7, "The focused `test_legs` suite passes 200 runs in a row."),
        LinkedCriterion(8, 8, "If deleted: the `relay` manifest is removed and the reason written down."),
    )
    assert criteria_evidence(criteria, file_changes(_SOCKET), parse_diff(_SOCKET)) == ()
    assert _scan(_SOCKET, criteria) == ()


def test_a_test_that_fails_without_the_change_is_still_asked_for():
    criteria = (LinkedCriterion(8, 1, "A test fails without the fix and passes with it."),)
    found = _scan(_SOCKET, criteria)
    assert [(f.rule_name, f.severity) for f in found] == [(RULE, "medium")]


def test_a_ticked_box_is_not_held_to_the_diff():
    boxes = [(c.issue, c.number, "ticked" if c.number > 1 else "open", c.text) for c in _CRITERIA]
    found = lane_work_findings(
        diff_text=_SOCKET, hunks=parse_diff(_SOCKET), file_contents={}, base_line_counts={},
        boxes=boxes, pr_title="Single instance", pr_body="## Why\nOne instance.\n" + _FOOTER,
    )
    assert RULE not in {f.rule_name for f in found}


def test_a_lane_pr_refs_linked_ticket_is_held_to_every_open_criterion():
    boxes = [(c.issue, c.number, "open", c.text) for c in _CRITERIA]
    found = lane_work_findings(
        diff_text=_SOCKET, hunks=parse_diff(_SOCKET), file_contents={}, base_line_counts={},
        boxes=boxes, pr_title="Single instance", pr_body="## Why\nOne instance.\n" + _FOOTER,
    )
    assert [(f.rule_name, f.severity) for f in found if f.rule_name == RULE] == [(RULE, "medium")]


_PLAIN = (
    LinkedCriterion(9, 1, "An unanswered ask goes to the family after a set number of tries."),
    LinkedCriterion(9, 2, "Nobody waits longer than two hours for help."),
)
_ASK = _changed("orchestrator/asks.py", ["def route(ask):"], ["    if ask.tries > MAX_TRIES:", "        return family"])


def test_the_judge_alone_never_raises_a_finding():
    assert {e.status for e in criteria_evidence(_PLAIN, file_changes(_ASK), parse_diff(_ASK))} == {"unsure"}
    asked = []

    def judge(criteria, hunks):
        asked.append(criteria)
        return {(9, 1): False, (9, 2): False}

    assert _scan(_ASK, _PLAIN) == ()
    assert _scan(_ASK, _PLAIN, judge=judge) == ()
    assert asked == []


def test_the_judge_decides_unsure_criteria_once_one_is_missing():
    # Criterion 3 names a target the diff never touches; 1 and 2 need a reader.
    criteria = (*_PLAIN, LinkedCriterion(9, 3, "`escalate_after()` is called from `AskQueue.tick`."))
    calls = []

    def judge(criteria, hunks):
        calls.append(tuple(c.number for c in criteria))
        return {(9, 1): True, (9, 2): False}

    found = _scan(_ASK, criteria, judge=judge)
    assert calls == [(1, 2)]
    assert [(f.rule_name, f.severity) for f in found] == [(RULE, "medium")]
    assert "#9 criterion 2" in found[0].message and "#9 criterion 3" in found[0].message
    assert "#9 criterion 1" not in found[0].message
    statuses = criteria_evidence(criteria, file_changes(_ASK), parse_diff(_ASK), judge=judge)
    assert [(e.criterion.number, e.status, e.judged) for e in statuses] == [
        (1, "met", True), (2, "missing", True), (3, "missing", False),
    ]
    met = _scan(_ASK, criteria, judge=lambda c, h: {(9, 1): True, (9, 2): True})
    assert ["1 of the 3" in f.message and "#9 criterion 3" in f.message for f in met] == [True]


def test_the_judge_is_not_asked_about_criteria_the_mapping_decides():
    asked = []
    _scan(_SOCKET, judge=lambda criteria, hunks: asked.append(criteria) or {})
    assert asked == []


# --- the model judgment ----------------------------------------------------------


def test_ground_accepts_only_a_cited_line_the_diff_changes():
    hunks = parse_diff(_ASK)
    answer = {"criteria": [
        {"id": 1, "met": True, "path": "orchestrator/asks.py", "line": "if ask.tries > MAX_TRIES:"},
        {"id": 2, "met": False, "path": "", "line": ""},
    ]}
    assert criteria_judge.ground(answer, _PLAIN, hunks) == {(9, 1): True, (9, 2): False}
    invented = {"criteria": [
        {"id": 1, "met": True, "path": "orchestrator/asks.py", "line": "escalate_after(hours=2)"},
        {"id": 2, "met": True, "path": "orchestrator/other.py", "line": "if ask.tries > MAX_TRIES:"},
    ]}
    assert criteria_judge.ground(invented, _PLAIN, hunks) == {}
    for junk in (None, [], {"criteria": "yes"}, {"criteria": [{"id": "1", "met": True}, {"id": 7, "met": False}]}):
        assert criteria_judge.ground(junk, _PLAIN, hunks) == {}


def _reply(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]},
        request=httpx.Request("POST", "https://gateway.invalid/v1/chat/completions"),
    )


def test_judge_redacts_for_cloud_backends_and_falls_back_to_the_raw_cave(monkeypatch):
    from llm_client import Backend, BackendConfig

    secret = "AKIAIOSFODNN7EXAMPLE"
    diff = _changed("orchestrator/asks.py", ["def route(ask):"], [f"    KEY = '{secret}'", "    return family"])
    cloud = BackendConfig(Backend.POOLSIDE, "https://cloud.invalid/v1", "m", lambda: "k")
    cave = BackendConfig(Backend.CAVE, "https://gateway.invalid/v1", "spark:warm-any", lambda: "in-cluster")
    monkeypatch.setattr(criteria_judge, "cloud_judge_configs", lambda: [cloud])
    monkeypatch.setattr(criteria_judge, "_cave_judge_config", lambda: cave)
    sent = []

    def call(config, messages, cancel_event=None):
        sent.append((config.backend, messages[1]["content"]))
        if config.backend is Backend.POOLSIDE:
            return _reply('{"criteria": [{"id": 1, "met": false}]}')  # partial: falls back
        return _reply('{"criteria": [{"id": 1, "met": true, "path": "orchestrator/asks.py",'
                      ' "line": "return family"}, {"id": 2, "met": false}]}')

    monkeypatch.setattr(criteria_judge.llm_client, "_call_backend", call)
    verdicts = criteria_judge.judge_criteria(_PLAIN, parse_diff(diff))
    assert verdicts == {(9, 1): True, (9, 2): False}
    assert [b for b, _ in sent] == [Backend.POOLSIDE, Backend.CAVE]
    assert secret not in sent[0][1]
    assert secret in sent[1][1]


def test_judge_fails_open(monkeypatch):
    from llm_client import Backend, BackendConfig

    cave = BackendConfig(Backend.CAVE, "https://gateway.invalid/v1", "m", lambda: "in-cluster")
    monkeypatch.setattr(criteria_judge, "cloud_judge_configs", list)
    monkeypatch.setattr(criteria_judge, "_cave_judge_config", lambda: cave)

    def boom(*_a, **_k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(criteria_judge.llm_client, "_call_backend", boom)
    assert criteria_judge.judge_criteria(_PLAIN, parse_diff(_ASK)) == {}
    monkeypatch.setattr(criteria_judge.llm_client, "_call_backend", lambda *a, **k: _reply("not json"))
    assert criteria_judge.judge_criteria(_PLAIN, parse_diff(_ASK)) == {}


def test_the_rule_renders_under_requirements():
    from personas.code_reviewer import linked_issue

    assert linked_issue.RULE_CATEGORIES[RULE] == "requirements"
