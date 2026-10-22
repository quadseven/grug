"""Tests for the dead-on-arrival lane-work check (#1366).

Recorded shapes (anonymized): a lane PR added a pure decision module and a
Dockerfile `COPY` line for it, and nothing imported it; another added a
config table that a page loads with a `<script>` tag and nothing reads; a
third added a policy class that the rest of the diff only imports and
names in a type annotation. Others committed `test_scratch.txt`,
`test_write.tmp`, `.loom/test.txt` and `bin/test.txt`.
"""

from __future__ import annotations

import time

from personas.code_reviewer.dead_on_arrival import (
    drop_superseded,
    is_stray_path,
    scan_dead_on_arrival,
)
from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.lane_work import lane_work_findings
from personas.code_reviewer.persona import Finding

_LANE_BODY = (
    "## Summary\nDismiss the dialog when it persists.\n"
    "---\nLoom ticket example.app-125, lane spark, model example-model.\n"
    "Refs example/app#125\n"
)

_DECISION = [
    '"""Decide whether a persistent dialog should be dismissed."""',
    "",
    "",
    "def should_dismiss(seen_twice, is_combat_text):",
    "    return seen_twice and not is_combat_text",
    "",
    "",
    "def click_point(box):",
    "    return box[0] + box[2] // 2, box[1] + box[3] // 2",
]
_DECISION_TEST = [
    "from death_dismiss import click_point, should_dismiss",
    "",
    "",
    "def test_dismiss():",
    "    assert should_dismiss(True, False)",
    "    assert click_point((0, 0, 4, 4)) == (2, 2)",
]


def _added(path: str, lines: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{ln}\n" for ln in lines)
    )


def _modified(path: str, old: list[str], new: list[str], start: int = 10) -> str:
    return (
        f"diff --git a/{path} b/{path}\nindex 2222222..3333333 100644\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -{start},{len(old)} +{start},{len(new)} @@\n"
        + "".join(f"-{ln}\n" for ln in old)
        + "".join(f"+{ln}\n" for ln in new)
    )


def _contents(diff: str) -> dict[str, str]:
    """Head text of each added file: its added lines."""
    out: dict[str, str] = {}
    for hunk in parse_diff(diff):
        lines = [raw[1:] for raw in hunk.body.splitlines()[1:] if raw.startswith("+")]
        out[hunk.file_path] = out.get(hunk.file_path, "") + "\n".join(lines) + "\n"
    return out


def _scan(diff: str, *, body: str = "", prior: tuple[Finding, ...] = ()):
    return scan_dead_on_arrival(
        file_changes(diff), parse_diff(diff), pr_body=body, prior=prior,
        file_contents=_contents(diff),
    )


def _lane(diff: str, *, body: str = _LANE_BODY, boxes=()):
    return lane_work_findings(
        diff_text=diff, hunks=parse_diff(diff), file_contents=_contents(diff),
        base_line_counts={}, boxes=boxes, pr_title="Dismiss the dialog", pr_body=body,
    )


def _summary(findings):
    return sorted((f.rule_name, f.severity, f.file) for f in findings)


_DOCKER_COPY = _modified(
    "Dockerfile",
    ["COPY core.py bridge.py voice.py \\"],
    ["COPY core.py death_dismiss.py bridge.py voice.py \\"],
)


# --- a new file nothing loads ---------------------------------------------------


def test_module_copied_into_an_image_and_imported_by_nothing_is_high():
    diff = (
        _DOCKER_COPY + _added("death_dismiss.py", _DECISION)
        + _added("tests/test_death_dismiss.py", _DECISION_TEST)
    )
    found = _scan(diff)
    assert _summary(found) == [("dead-code", "high", "death_dismiss.py")]
    assert found[0].line == 1
    assert "only production change" in found[0].message


def test_the_same_module_beside_another_production_change_is_medium():
    diff = (
        _DOCKER_COPY + _added("death_dismiss.py", _DECISION)
        + _modified("poll.py", ["    pass"], ["    retry_once()"])
    )
    assert _summary(_scan(diff)) == [("dead-code", "medium", "death_dismiss.py")]


def test_a_module_production_code_imports_draws_nothing():
    diff = (
        _DOCKER_COPY + _added("death_dismiss.py", _DECISION)
        + _modified("poll.py", ["import os"], ["import os", "from death_dismiss import should_dismiss"])
    )
    assert _scan(diff) == ()


def test_a_dotted_import_names_the_module():
    path = "services/shared/review/intent_filter.py"
    diff = _added(path, _DECISION) + _modified(
        "services/shared/review/dispatch.py", ["import os"],
        ["import os", "from review.intent_filter import should_dismiss"],
    )
    assert _scan(diff) == ()


def test_a_dockerfile_command_that_runs_the_script_is_a_reference():
    diff = _added("verify_dialog.py", _DECISION) + _modified(
        "Dockerfile", ['CMD ["python", "core.py"]'], ["CMD python verify_dialog.py"],
    )
    assert _scan(diff) == ()


def test_a_doc_that_names_the_script_is_a_reference():
    diff = _added("scripts/emit_report.py", _DECISION) + _added(
        "docs/runbook.md", ["Run `python3 scripts/emit_report.py` weekly."],
    )
    assert _scan(diff) == ()


def test_an_entry_point_the_body_declares_draws_nothing():
    diff = _added("scripts/emit_report.py", _DECISION)
    body = "Adds the `scripts/emit_report.py` CLI command for operators."
    assert _scan(diff, body=body) == ()
    assert _summary(_scan(diff)) == [("dead-code", "high", "scripts/emit_report.py")]


def test_a_file_a_runner_discovers_by_location_draws_nothing():
    diff = _added("db/migrations/versions/0042_add_column.py", _DECISION)
    assert _scan(diff) == ()


def test_a_configmap_listing_does_not_load_the_module():
    diff = _modified(
        "deploy/app/kustomization.yaml", ["      - pkg/auth.py"],
        ["      - pkg/auth.py", "      - pkg/egress_rules.py"],
    ) + _added("deploy/app/pkg/egress_rules.py", _DECISION)
    found = _scan(diff)
    assert _summary(found) == [("dead-code", "high", "deploy/app/pkg/egress_rules.py")]


def test_an_intent_mismatch_that_names_the_file_is_not_repeated():
    diff = _added("scripts/emit_report.py", _DECISION)
    prior = (Finding(
        file="scripts/emit_report.py", line=1, severity="high", rule_name="intent-mismatch",
        message="No hunk can satisfy it: adds `scripts/emit_report.py`, which nothing runs.",
        suggestion=None,
    ),)
    assert _scan(diff, prior=prior) == ()


def test_per_function_findings_in_a_dead_file_are_superseded_by_one():
    diff = _added("death_dismiss.py", _DECISION) + _added(
        "tests/test_death_dismiss.py", _DECISION_TEST,
    )
    found = _lane(diff)
    # The #1179 check marks `should_dismiss` and `click_point`; the file
    # finding replaces both.
    assert _summary(found) == [("dead-code", "high", "death_dismiss.py")]
    assert "New file" in found[0].message


def test_a_dead_file_outside_a_lane_pr_stays_medium():
    diff = _added("death_dismiss.py", _DECISION)
    found = _lane(diff, body="## Why\nA helper for the next change.\n")
    assert _summary(found) == [("dead-code", "medium", "death_dismiss.py")]


def test_drop_superseded_keeps_other_files_and_rules():
    whole = Finding("a.py", 1, "medium", "dead-code", "New file `a.py` is not imported.", None)
    per_def = Finding("a.py", 4, "medium", "dead-code", "New function `f` has no caller.", None)
    other = Finding("b.py", 4, "medium", "dead-code", "New function `g` has no caller.", None)
    mismatch = Finding("a.py", 4, "high", "intent-mismatch", "No hunk can satisfy.", None)
    assert drop_superseded((per_def, other, mismatch), (whole,)) == (other, mismatch)


# --- a script a page loads but nothing reads ------------------------------------

_CONFIG_TABLE = [
    "// Encounter balancing tables.",
    "(function (CD) {",
    "  'use strict';",
    "  const BADGE_TIERS = {",
    "    TIER_1: { minLevel: 2, maxLevel: 5 },",
    "  };",
    "  CD.encounterConfig = { BADGE_TIERS };",
    "})(window.GameData);",
]


def _page_loads(path: str) -> str:
    return _modified(
        "index.html", ['<script src="src/data/moves.js"></script>'],
        ['<script src="src/data/moves.js"></script>', f'<script src="{path}"></script>'],
    )


def test_a_config_table_a_page_loads_and_nothing_reads_is_medium():
    path = "src/data/encounter-config.js"
    diff = _page_loads(path) + _added(path, _CONFIG_TABLE) + _added(
        "tests/encounter-config.test.js", ["const c = CD.encounterConfig;"],
    )
    found = _scan(diff)
    assert _summary(found) == [("dead-code", "medium", path)]
    assert "<script>" in found[0].message


def test_a_config_table_another_file_reads_draws_nothing():
    path = "src/data/encounter-config.js"
    diff = _page_loads(path) + _added(path, _CONFIG_TABLE) + _modified(
        "src/game/wild.js", ["  return roll();"],
        ["  return pick(CD.encounterConfig, roll());"],
    )
    assert _scan(diff) == ()


def test_a_script_that_runs_at_load_draws_nothing():
    path = "src/ui/banner.js"
    script = ["const BANNER_TEXT = 'hi';", "document.addEventListener('load', show);"]
    assert _scan(_page_loads(path) + _added(path, script)) == ()


# --- a class nothing constructs -------------------------------------------------

_POLICY = [
    '"""Per-client egress policy."""',
    "",
    "",
    "class EgressPolicy:",
    "    def __init__(self, clients):",
    "        self.clients = clients",
    "",
    "    def check(self, identity):",
    "        return identity in self.clients",
]


def _transport(lines: list[str]) -> str:
    return _modified(
        "bond/home_transport.py", ["import logging"], ["import logging", *lines],
    )


def test_a_policy_class_only_imported_and_annotated_is_medium():
    diff = _added("bond/egress.py", _POLICY) + _transport([
        "from bond.egress import EgressPolicy",
        "egress_policy: EgressPolicy | None = None",
        "if egress_policy is not None and not egress_policy.check(name):",
        "    drop()",
    ])
    found = _scan(diff)
    assert _summary(found) == [("dead-code", "medium", "bond/egress.py")]
    assert found[0].line == 4
    assert "`EgressPolicy` is never constructed" in found[0].message


def test_a_policy_class_production_code_constructs_draws_nothing():
    diff = _added("bond/egress.py", _POLICY) + _transport([
        "from bond.egress import EgressPolicy",
        "policy = EgressPolicy(load_clients())",
    ])
    assert _scan(diff) == ()


def test_a_class_with_a_base_draws_nothing():
    lines = [line.replace("class EgressPolicy:", "class EgressPolicy(Policy):") for line in _POLICY]
    diff = _added("bond/egress.py", lines) + _transport(["from bond.egress import EgressPolicy"])
    assert _scan(diff) == ()


# --- stray scratch files --------------------------------------------------------


def test_recorded_scratch_files_are_stray():
    for path in (
        "test_scratch.txt", "test_write.tmp", ".loom/test.txt", "bin/test.txt",
        "temp_git_objects/ab/cdef", "notes.bak", "scratch.py",
    ):
        assert is_stray_path(path), path


def test_config_fixtures_and_ordinary_files_are_not_stray():
    for path in (
        ".loom/validation.conf", "tests/fixtures/test_input.txt", "templates/page.html",
        "docs/testing.md", "src/temperature.py", "requirements.txt",
    ):
        assert not is_stray_path(path), path


def test_stray_files_get_one_medium_finding_each():
    diff = (
        _added("test_scratch.txt", ["scratch"]) + _added(".loom/test.txt", ["x"])
        + _added(".loom/validation.conf", ["make test"])
        + _modified("src/game.cpp", ["  return 0;"], ["  return 1;"])
    )
    assert _summary(_scan(diff)) == [
        ("stray-file", "medium", ".loom/test.txt"),
        ("stray-file", "medium", "test_scratch.txt"),
    ]


def test_stray_file_has_a_board_category():
    from personas.code_reviewer.linked_issue import RULE_CATEGORIES

    assert RULE_CATEGORIES["stray-file"] == "maintainability"


# --- cost -------------------------------------------------------------------------


def test_scan_is_linear_on_a_large_diff():
    # 90 new scripts and 90 new modules of about 300 lines each, plus one
    # module importing a class from each: about 54,000 added lines, under
    # the candidate bound.
    parts = []
    for i in range(90):
        parts.append(_added(f"ops/tool_{i:03d}.sh", ["#!/bin/sh"] + [f"echo {n}" for n in range(300)]))
        module = [f"class Policy{i:03d}:", "    pass"] + [f"VALUE_{n} = {n}" for n in range(300)]
        parts.append(_added(f"pkg/mod_{i:03d}.py", module))
    parts.append(_added("pkg/wire.py", [f"from pkg.mod_{i:03d} import Policy{i:03d}" for i in range(90)]))
    diff = "".join(parts)
    changes, hunks, contents = file_changes(diff), parse_diff(diff), _contents(diff)
    start = time.perf_counter()
    found = scan_dead_on_arrival(changes, hunks, pr_body="", file_contents=contents)
    elapsed = time.perf_counter() - start
    rules = {(f.rule_name, f.file.split("/")[0]) for f in found}
    assert rules == {("dead-code", "ops"), ("dead-code", "pkg")}
    assert elapsed < 2.0, elapsed


def test_past_the_scan_bounds_only_stray_files_are_reported():
    from personas.code_reviewer import criteria_coverage as cc

    many = "".join(
        _added(f"ops/tool_{i:03d}.sh", ["#!/bin/sh"])
        for i in range(cc.MAX_REFERENCE_CANDIDATES + 1)
    ) + _added("test_write.tmp", ["x"])
    assert _summary(_scan(many)) == [("stray-file", "medium", "test_write.tmp")]
