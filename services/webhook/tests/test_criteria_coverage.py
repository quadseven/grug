"""Tests for the deterministic criteria-coverage check (#1179).

Recorded shape: a lane PR titled "Free lane slots verify shipped tickets,
and the station applies the proof" added one helper, `verify_due`, that
nothing outside its own tests called, plus two unit tests. Its ticket had
three runtime criteria. Elder read the criteria, reported "clear", and the
PR merged having met none of them.

The check must:
- raise ONE high intent-mismatch naming every runtime criterion no hunk
  can satisfy, when every production change is an uncalled new function;
- raise a medium dead-code finding on each new function nothing calls,
  unless the PR body declares it an entry point;
- stay silent when the diff wires the new function into a caller;
- skip "Live:" criteria and criteria about tests or docs;
- fail open: a production file with no head text never yields an unmet
  criterion.
"""

from __future__ import annotations

from collections.abc import Sequence

from personas.code_reviewer.criteria_coverage import (
    LinkedCriterion,
    scan_criteria_coverage,
)
from personas.code_reviewer.diff_parser import DiffHunk

_TITLE = "Free lane slots verify shipped tickets, and the station applies the proof"
_BODY = "Closes #436\n\nAdds the verify-due check for shipped tickets."

_RUNTIME_CRITERIA = (
    LinkedCriterion(436, 1, "A free lane slot picks up a shipped ticket whose verify run is due"),
    LinkedCriterion(436, 2, "The station applies the proof a verify run returns to the ticket"),
    LinkedCriterion(436, 3, "A ticket whose verify run fails is reopened"),
)

_VERIFY_BASE = (
    '"""Verify runs for shipped tickets."""\n'
    "\n"
    "VERIFY_AFTER = 3600\n"
    "\n"
    "\n"
    "def shipped(ticket):\n"
    "    return ticket.shipped_at is not None\n"
)
_VERIFY_ADDED = [
    "",
    "",
    "def verify_due(ticket, now):",
    '    """True when a shipped ticket\'s verify run is due."""',
    "    return shipped(ticket) and now - ticket.shipped_at >= VERIFY_AFTER",
]
_VERIFY_HEAD = _VERIFY_BASE + "\n".join(_VERIFY_ADDED) + "\n"
# Line 10 of the head file is `def verify_due(`.
_VERIFY_DEF_LINE = 10

_TEST_ADDED = [
    "from station.verify import verify_due",
    "",
    "",
    "def test_due_after_an_hour():",
    "    assert verify_due(_ticket(shipped_at=0), now=3600)",
    "",
    "",
    "def test_not_due_before_an_hour():",
    "    assert not verify_due(_ticket(shipped_at=0), now=10)",
]

_LOOP_HEAD = (
    "from station.verify import verify_due\n"
    "\n"
    "\n"
    "def tick(slots, tickets, now):\n"
    "    for ticket in tickets:\n"
    "        if verify_due(ticket, now):\n"
    "            slots.dispatch_verify(ticket)\n"
)


def _hunk(
    path: str, start: int, added: Sequence[str], removed: Sequence[str] = (),
) -> DiffHunk:
    lines = [f"-{ln}" for ln in removed] + [f"+{ln}" for ln in added]
    return DiffHunk(
        file_path=path,
        new_start=start,
        new_lines=frozenset(range(start, start + len(added))),
        body=f"@@ -1 +{start},{len(added)} @@\n" + "\n".join(lines),
    )


def _helper_only_diff() -> tuple[tuple[DiffHunk, ...], dict[str, str]]:
    """The recorded shape: one uncalled helper plus its unit tests."""
    hunks = (
        _hunk("station/verify.py", 8, _VERIFY_ADDED),
        _hunk("tests/test_verify.py", 1, _TEST_ADDED),
    )
    contents = {
        "station/verify.py": _VERIFY_HEAD,
        "tests/test_verify.py": "\n".join(_TEST_ADDED) + "\n",
    }
    return hunks, contents


def _wired_diff() -> tuple[tuple[DiffHunk, ...], dict[str, str]]:
    """The same helper, plus a call from the existing `tick` loop."""
    hunks, contents = _helper_only_diff()
    loop_lines = _LOOP_HEAD.rstrip("\n").split("\n")
    hunks = hunks + (
        _hunk("station/loop.py", 1, loop_lines[:1]),
        _hunk("station/loop.py", 6, loop_lines[5:]),
    )
    return hunks, {**contents, "station/loop.py": _LOOP_HEAD}


def _by_rule(findings, rule):
    return [f for f in findings if f.rule_name == rule]


# --- the recorded shape -----------------------------------------------------


def test_uncalled_helper_with_runtime_criteria_is_a_high_intent_mismatch():
    hunks, contents = _helper_only_diff()
    findings = scan_criteria_coverage(
        hunks, contents, criteria=_RUNTIME_CRITERIA, pr_title=_TITLE, pr_body=_BODY,
    )
    mismatches = _by_rule(findings, "intent-mismatch")
    assert len(mismatches) == 1, findings
    finding = mismatches[0]
    assert finding.severity == "high"
    assert (finding.file, finding.line) == ("station/verify.py", _VERIFY_DEF_LINE)
    for criterion in _RUNTIME_CRITERIA:
        assert f"#436 criterion {criterion.number}" in finding.message
        assert criterion.text in finding.message
    assert "verify_due" in finding.message
    assert _TITLE in finding.message


def test_uncalled_helper_is_a_medium_dead_code_finding():
    hunks, contents = _helper_only_diff()
    findings = scan_criteria_coverage(
        hunks, contents, criteria=(), pr_title=_TITLE, pr_body=_BODY,
    )
    dead = _by_rule(findings, "dead-code")
    assert len(dead) == 1, findings
    assert dead[0].severity == "medium"
    assert (dead[0].file, dead[0].line) == ("station/verify.py", _VERIFY_DEF_LINE)
    assert "verify_due" in dead[0].message
    # No criteria were linked, so nothing can be unmet.
    assert not _by_rule(findings, "intent-mismatch")


# --- a diff that meets its criteria ------------------------------------------


def test_wired_function_covering_each_non_live_criterion_is_clear():
    hunks, contents = _wired_diff()
    criteria = (
        LinkedCriterion(436, 1, "A free lane slot picks up a shipped ticket whose verify run is due"),
        LinkedCriterion(436, 2, "A test shows a due ticket is dispatched to a free slot"),
        LinkedCriterion(436, 3, "Live: the next shipped ticket gets a verify run within an hour"),
    )
    findings = scan_criteria_coverage(
        hunks, contents, criteria=criteria, pr_title=_TITLE, pr_body=_BODY,
    )
    assert not _by_rule(findings, "intent-mismatch"), findings
    assert not _by_rule(findings, "dead-code"), findings


def test_helper_called_only_by_another_uncalled_helper_is_still_dead():
    hunks, contents = _helper_only_diff()
    added = ["", "", "def verify_batch(tickets, now):", "    return [t for t in tickets if verify_due(t, now)]"]
    head = _VERIFY_HEAD + "\n".join(added) + "\n"
    hunks = (_hunk("station/verify.py", 8, _VERIFY_ADDED + added),) + hunks[1:]
    findings = scan_criteria_coverage(
        hunks, {**contents, "station/verify.py": head},
        criteria=_RUNTIME_CRITERIA, pr_title=_TITLE, pr_body=_BODY,
    )
    assert len(_by_rule(findings, "intent-mismatch")) == 1
    dead = _by_rule(findings, "dead-code")
    # One marking for the call tree, on its root, naming the stranded helper.
    assert len(dead) == 1, findings
    assert dead[0].line == 15  # `def verify_batch(`
    assert dead[0].message.split("`")[1] == "verify_batch"
    assert "`verify_due`" in dead[0].message


def test_uncalled_root_and_its_private_helpers_are_one_marking():
    added = [
        "",
        "",
        "def publish_report(rows):",
        "    return _render(_group(_clean(rows)))",
        "",
        "",
        "def _clean(rows):",
        "    return [r for r in rows if r]",
        "",
        "",
        "def _group(rows):",
        "    return sorted(rows)",
        "",
        "",
        "def _render(rows):",
        "    return ', '.join(rows)",
    ]
    head = _VERIFY_BASE + "\n".join(added) + "\n"
    findings = scan_criteria_coverage(
        (_hunk("station/verify.py", 8, added),), {"station/verify.py": head},
        criteria=(), pr_title=_TITLE, pr_body=_BODY,
    )
    dead = _by_rule(findings, "dead-code")
    assert len(dead) == 1, findings
    assert (dead[0].file, dead[0].line) == ("station/verify.py", 10)
    assert dead[0].message.split("`")[1] == "publish_report"
    assert "neither do the 3 new helpers it calls" in dead[0].message
    for helper in ("_clean", "_group", "_render"):
        assert f"`{helper}`" in dead[0].message


def test_mutually_recursive_uncalled_defs_still_get_a_marking():
    added = [
        "",
        "",
        "def ping(n):",
        "    return pong(n - 1) if n else 0",
        "",
        "",
        "def pong(n):",
        "    return ping(n - 1) if n else 1",
    ]
    head = _VERIFY_BASE + "\n".join(added) + "\n"
    findings = scan_criteria_coverage(
        (_hunk("station/verify.py", 8, added),), {"station/verify.py": head},
        criteria=(), pr_title=_TITLE, pr_body=_BODY,
    )
    dead = _by_rule(findings, "dead-code")
    assert len(dead) == 1, findings
    assert dead[0].message.split("`")[1] == "ping"
    assert "`pong`" in dead[0].message


# --- exemptions --------------------------------------------------------------


def test_live_criteria_are_exempt_when_the_diff_changes_runtime():
    hunks, contents = _wired_diff()
    criteria = (LinkedCriterion(436, 1, "Live: a shipped ticket gets a verify run within an hour"),)
    findings = scan_criteria_coverage(
        hunks, contents, criteria=criteria, pr_title=_TITLE, pr_body=_BODY,
    )
    assert not _by_rule(findings, "intent-mismatch")


def test_live_criteria_are_unmet_when_nothing_in_the_diff_runs():
    # #1364: a deploy can make a Live: criterion true only if the diff
    # changes something that runs. An uncalled helper deploys nothing.
    hunks, contents = _helper_only_diff()
    criteria = (LinkedCriterion(436, 1, "Live: a shipped ticket gets a verify run within an hour"),)
    findings = scan_criteria_coverage(
        hunks, contents, criteria=criteria, pr_title=_TITLE, pr_body=_BODY,
    )
    mismatch = _by_rule(findings, "intent-mismatch")
    assert len(mismatch) == 1 and mismatch[0].severity == "high"
    assert "Live: a shipped ticket" in mismatch[0].message


def test_test_and_doc_criteria_can_be_met_without_runtime_hunks():
    hunks, contents = _helper_only_diff()
    criteria = (
        LinkedCriterion(436, 1, "A test shows verify_due is true after an hour"),
        LinkedCriterion(436, 2, "The README documents the verify window"),
    )
    findings = scan_criteria_coverage(
        hunks, contents, criteria=criteria, pr_title=_TITLE, pr_body=_BODY,
    )
    assert not _by_rule(findings, "intent-mismatch")


def test_declared_entry_point_is_not_dead_and_counts_as_runtime():
    hunks, contents = _helper_only_diff()
    body = _BODY + "\n\n`verify_due` is an exported API for the station plugin."
    findings = scan_criteria_coverage(
        hunks, contents, criteria=_RUNTIME_CRITERIA, pr_title=_TITLE, pr_body=body,
    )
    assert findings == ()


def test_decorated_and_dunder_defs_are_not_dead():
    added = [
        "",
        "",
        "@app.route('/verify')",
        "def verify_route():",
        "    return 'ok'",
        "",
        "",
        "class Proof:",
        "    def __init__(self, data):",
        "        self.data = data",
    ]
    head = _VERIFY_BASE + "\n".join(added) + "\n"
    findings = scan_criteria_coverage(
        (_hunk("station/verify.py", 8, added),), {"station/verify.py": head},
        criteria=(), pr_title=_TITLE, pr_body=_BODY,
    )
    assert not _by_rule(findings, "dead-code"), findings


def test_alembic_revision_hooks_are_not_dead():
    head = (
        '"""add verify table"""\n'
        "from alembic import op\n"
        "\n"
        "revision = '0042'\n"
        "down_revision = '0041'\n"
        "\n"
        "\n"
        "def upgrade():\n"
        "    op.create_table('verify')\n"
        "\n"
        "\n"
        "def downgrade():\n"
        "    op.drop_table('verify')\n"
    )
    path = "migrations/versions/0042_verify.py"
    findings = scan_criteria_coverage(
        (_hunk(path, 1, head.rstrip("\n").split("\n")),), {path: head},
        criteria=_RUNTIME_CRITERIA, pr_title=_TITLE, pr_body=_BODY,
    )
    assert findings == ()


def test_upgrade_outside_an_alembic_revision_is_still_dead():
    added = ["", "", "def upgrade(ticket):", "    return ticket"]
    head = _VERIFY_BASE + "\n".join(added) + "\n"
    findings = scan_criteria_coverage(
        (_hunk("station/verify.py", 8, added),), {"station/verify.py": head},
        criteria=(), pr_title=_TITLE, pr_body=_BODY,
    )
    assert len(_by_rule(findings, "dead-code")) == 1, findings


def test_override_on_a_subclass_is_not_dead():
    # urllib calls `redirect_request` by dispatch through the base class,
    # so no caller names it.
    head = (
        "import urllib.request\n"
        "\n"
        "\n"
        "class _NoRedirects(urllib.request.HTTPRedirectHandler):\n"
        "    def redirect_request(self, req, fp, code, msg, headers, newurl):\n"
        "        return None\n"
        "\n"
        "\n"
        "OPENER = urllib.request.build_opener(_NoRedirects)\n"
    )
    path = "station/http.py"
    findings = scan_criteria_coverage(
        (_hunk(path, 1, head.rstrip("\n").split("\n")),), {path: head},
        criteria=(), pr_title=_TITLE, pr_body=_BODY,
    )
    assert not _by_rule(findings, "dead-code"), findings


def test_method_of_a_baseless_class_is_still_dead():
    added = ["", "", "class Proof(object):", "    def check(self):", "        return True"]
    head = _VERIFY_BASE + "\n".join(added) + "\n"
    findings = scan_criteria_coverage(
        (_hunk("station/verify.py", 8, added),), {"station/verify.py": head},
        criteria=(), pr_title=_TITLE, pr_body=_BODY,
    )
    assert [f.message.split("`")[1] for f in _by_rule(findings, "dead-code")] == ["check"]


def test_defs_in_test_files_are_ignored():
    _, contents = _helper_only_diff()
    findings = scan_criteria_coverage(
        (_hunk("tests/test_verify.py", 1, _TEST_ADDED),), contents,
        criteria=(), pr_title=_TITLE, pr_body=_BODY,
    )
    assert findings == ()


# --- fail-open ---------------------------------------------------------------


def test_missing_head_text_never_yields_an_unmet_criterion():
    hunks, contents = _helper_only_diff()
    del contents["station/verify.py"]
    findings = scan_criteria_coverage(
        hunks, contents, criteria=_RUNTIME_CRITERIA, pr_title=_TITLE, pr_body=_BODY,
    )
    assert findings == ()


def test_unparseable_head_text_never_yields_an_unmet_criterion():
    hunks, contents = _helper_only_diff()
    contents["station/verify.py"] = "def verify_due(:\n"
    findings = scan_criteria_coverage(
        hunks, contents, criteria=_RUNTIME_CRITERIA, pr_title=_TITLE, pr_body=_BODY,
    )
    assert findings == ()


def test_removed_production_lines_count_as_runtime_change():
    hunks, contents = _helper_only_diff()
    removal = _hunk("station/loop.py", 3, [], removed=["    legacy_verify(ticket)"])
    findings = scan_criteria_coverage(
        hunks + (removal,), contents,
        criteria=_RUNTIME_CRITERIA, pr_title=_TITLE, pr_body=_BODY,
    )
    assert not _by_rule(findings, "intent-mismatch")


# --- dispatch merge ------------------------------------------------------------


def _evaluation(findings=()):
    from personas.code_reviewer.persona import CodeReviewEvaluation

    return CodeReviewEvaluation(
        findings=tuple(findings), conclusion="success",
        dropped_hallucinations=0, degraded_reason=None,
    )


def _pr_context(body: str) -> dict:
    from personas.code_reviewer.linked_issue import render_linked_issue_block

    block = render_linked_issue_block(
        {436: [("open", c.text) for c in _RUNTIME_CRITERIA]},
    )
    return {"title": _TITLE, "body": body, "linked_issue_context": block}


def _merge(evaluation, hunks, contents, body, *, correctness_only=False):
    from personas.code_reviewer import dispatch

    return dispatch._merge_lane_work(
        evaluation, hunks, "", contents, {}, _pr_context(body),
        correctness_only=correctness_only,
    )


def test_dispatch_merges_a_high_mismatch_for_a_closed_issue():
    hunks, contents = _helper_only_diff()
    merged = _merge(_evaluation(), hunks, contents, _BODY)
    assert [f.rule_name for f in merged.findings] == ["dead-code", "intent-mismatch"]
    assert merged.conclusion == "failure"


def test_dispatch_ignores_criteria_of_issues_the_pr_only_refs():
    hunks, contents = _helper_only_diff()
    merged = _merge(_evaluation(), hunks, contents, "Refs #436")
    assert [f.rule_name for f in merged.findings] == ["dead-code"]


def test_dispatch_checks_a_lane_pr_against_the_ticket_it_refs():
    # #1364: a lane PR links its ticket with Refs, and the station closes
    # that ticket when the PR merges, so its criteria are this PR's to meet.
    hunks, contents = _helper_only_diff()
    body = "Refs #436\n\n---\nLoom ticket example.repo-436, lane codex, model m.\nRefs owner/repo#436"  # leak-guard-allow: placeholder repo
    merged = _merge(_evaluation(), hunks, contents, body)
    assert [f.rule_name for f in merged.findings] == ["dead-code", "intent-mismatch"]
    assert merged.conclusion == "failure"


def test_dispatch_correctness_scope_drops_dead_code_and_llm_rows_win():
    from personas.code_reviewer.persona import Finding

    hunks, contents = _helper_only_diff()
    llm = Finding(
        file="station/verify.py", line=_VERIFY_DEF_LINE, severity="high",
        rule_name="intent-mismatch", message="model row", suggestion=None,
    )
    merged = _merge(_evaluation([llm]), hunks, contents, _BODY, correctness_only=True)
    assert merged.findings == (llm,)


def test_criteria_about_the_pull_request_itself_need_no_hunk():
    # A ticket to land or supersede an older pull request: the review
    # process meets these, a docs-only diff is fine.
    criteria = (
        LinkedCriterion(436, 1, "The change reaches the default branch through a pull request"),
        LinkedCriterion(436, 2, "The new pull request's checks pass and every review thread is answered"),
    )
    hunks = (_hunk("skills/verify/SKILL.md", 1, ["# Verify", "Check before you claim."]),)
    findings = scan_criteria_coverage(
        hunks, {}, criteria=criteria, pr_title=_TITLE, pr_body=_BODY,
    )
    assert not _by_rule(findings, "intent-mismatch")
