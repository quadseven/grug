"""Tests for the lane-work seam (#1363): criteria scope, unreferenced
scripts and live-state criteria (#1364), and destructive edits (#1365).

Recorded shape (#1361, anonymized): a lane PR on a host-patching ticket
replaced the repository's 101-line root README with a write-up of the
ticket and added a 139-line standalone verification script. The README
named the script, nothing else did. Every criterion was a live host check:
a version reported on two hosts, ports closed from a VLAN, a tailnet ACL.
The PR linked its ticket with `Refs owner/name#N`. Elder passed it with
"No markings survived the judge".
"""

from __future__ import annotations

from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.destructive_edit import scan_destructive_edits
from personas.code_reviewer.diff_parser import FileChange, file_changes, parse_diff
from personas.code_reviewer.lane_work import (
    criteria_in_scope,
    is_lane_pr,
    lane_work_findings,
)

_FOOTER = (
    "\n---\nLoom ticket example.iac-83-r3, lane codex, model gpt-6-luna.\n"
    "Refs example/iac#83\n"
)
_TITLE = "Patch the PHP RCE on both storage hosts and close the GUI to the IoT VLAN"
_BODY = "## Why\nA critical PHP RCE is fixed in 8.4.27.\n" + _FOOTER
_HOST_BOXES = (
    (83, 1, "open", "`php -v` on build-host-1 and build-host-2 reports 8.4.27 or later."),
    (83, 2, "open", "From an IoT-VLAN address, ports 80, 443 and 8443 on 192.0.2.10 are closed."),
    (83, 3, "open", "The tailnet ACL lets only admin devices reach the GUI ports on both hosts."),
)

_SCRIPT = [
    "#!/usr/bin/env python3",
    '"""Verify the PHP fix on both hosts."""',
    "import subprocess",
    "",
    "",
    "def php_version(host):",
    "    out = subprocess.run(['ssh', host, 'php', '-v'], capture_output=True, text=True)",
    "    return out.stdout.split()[1]",
    "",
    "",
    "def main():",
    "    for host in ('build-host-1', 'build-host-2'):",
    "        print(host, php_version(host))",
    "",
    "",
    'if __name__ == "__main__":',
    "    main()",
]
_OLD_README = [f"Project overview line {n}" for n in range(1, 102)] + ["", "## License", "MIT"] * 9
_NEW_README = [
    "# PHP fix",
    "",
    "Run the verification:",
    "",
    "    python3 production/scripts/verify-php-fix.py",
] + [f"Ticket write-up line {n}" for n in range(1, 83)]


def _added_file(path: str, lines: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{ln}\n" for ln in lines)
    )


def _replaced_file(path: str, old: list[str], new: list[str], keep: int = 0) -> str:
    kept = old[:keep]
    body = "".join(f" {ln}\n" for ln in kept)
    body += "".join(f"-{ln}\n" for ln in old[keep:])
    body += "".join(f"+{ln}\n" for ln in new)
    return (
        f"diff --git a/{path} b/{path}\nindex 2222222..3333333 100644\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(old)} +1,{keep + len(new)} @@\n" + body
    )


def _findings(diff: str, *, boxes=_HOST_BOXES, body=_BODY, title=_TITLE, base=None):
    hunks = parse_diff(diff)
    contents = {}
    for change in file_changes(diff):
        if change.status == "added" and change.path.endswith("verify-php-fix.py"):
            contents[change.path] = "\n".join(_SCRIPT) + "\n"
    return lane_work_findings(
        diff_text=diff, hunks=hunks, file_contents=contents,
        base_line_counts=base if base is not None else {"README.md": len(_OLD_README)},
        boxes=boxes, pr_title=title, pr_body=body,
    )


def _recorded_diff() -> str:
    return _replaced_file("README.md", _OLD_README, _NEW_README) + _added_file(
        "production/scripts/verify-php-fix.py", _SCRIPT,
    )


def _rules(findings):
    return sorted(f.rule_name for f in findings)


# --- the recorded pull request ------------------------------------------------


def test_recorded_lane_pr_gets_a_high_mismatch_and_a_destructive_edit():
    findings = _findings(_recorded_diff())
    assert _rules(findings) == ["destructive-edit", "intent-mismatch"], findings
    assert all(f.severity == "high" for f in findings)
    mismatch = next(f for f in findings if f.rule_name == "intent-mismatch")
    assert mismatch.file == "production/scripts/verify-php-fix.py"
    for issue, number, _state, text in _HOST_BOXES:
        assert f"#{issue} criterion {number}" in mismatch.message
        assert text in mismatch.message
    assert "new script someone would still have to run by hand" in mismatch.message
    edit = next(f for f in findings if f.rule_name == "destructive-edit")
    assert edit.file == "README.md"
    assert "deletes 128 of the 128 lines in `README.md`" in edit.message


def test_the_same_criteria_met_by_config_draw_no_mismatch():
    # The fix lands where the state is managed: a firewall rule file.
    diff = _recorded_diff() + _replaced_file(
        "production/firewall/storage-hosts.rules", ["allow 80", "allow 443"],
        ["deny from vlan150 port 80", "deny from vlan150 port 443"],
    )
    assert "intent-mismatch" not in _rules(_findings(diff))


def test_a_tested_script_with_criteria_about_what_it_does_is_not_a_mismatch():
    boxes = ((83, 1, "open", "The verifier prints each host's PHP version"),)
    test = ["from production.scripts import verify_php_fix", "", "def test_parse():", "    pass"]
    diff = _added_file("production/scripts/verify_php_fix.py", _SCRIPT) + _added_file(
        "tests/test_verify_php_fix.py", test,
    )
    assert _findings(diff, boxes=boxes) == ()


def test_a_human_pr_that_only_refs_the_ticket_is_not_held_to_it():
    body = "## Why\nFirst slice of the patch work.\n\nRefs #83\n"
    diff = _added_file("production/scripts/verify-php-fix.py", _SCRIPT)
    assert _findings(diff, body=body) == ()


def test_a_script_nothing_names_is_inert_against_runtime_criteria():
    boxes = ((83, 1, "open", "The nightly sync retries a failed upload three times"),)
    script = ["#!/bin/sh", "set -eu", "for i in 1 2 3; do upload && break; done"]
    findings = _findings(_added_file("ops/retry-upload.sh", script), boxes=boxes)
    assert _rules(findings) == ["intent-mismatch"]
    assert "`ops/retry-upload.sh`, which no other file in this diff imports" in findings[0].message


def test_a_script_a_workflow_runs_is_not_inert():
    boxes = ((83, 1, "open", "The nightly sync retries a failed upload three times"),)
    script = ["#!/bin/sh", "set -eu", "for i in 1 2 3; do upload && break; done"]
    workflow = ["on: schedule", "jobs:", "  sync:", "    steps:", "      - run: ops/retry-upload.sh"]
    diff = _added_file("ops/retry-upload.sh", script) + _added_file(
        ".github/workflows/nightly.yml", workflow,
    )
    assert _findings(diff, boxes=boxes) == ()


# --- criteria scope ------------------------------------------------------------


def test_scope_takes_every_box_of_a_lane_pr_and_only_claimed_boxes_otherwise():
    boxes = ((7, 1, "open", "The cache expires entries after an hour"),
             (7, 2, "open", "The cache logs each eviction with its key"))
    assert is_lane_pr(_FOOTER)
    assert len(criteria_in_scope(boxes, "Refs #7" + _FOOTER)) == 2
    claimed = criteria_in_scope(boxes, "Refs #7\nThe cache expires entries after an hour.")
    assert [c.number for c in claimed] == [1]
    assert criteria_in_scope(boxes, "Refs #7") == ()
    assert len(criteria_in_scope(boxes, "Closes #7")) == 2


# --- destructive edits ---------------------------------------------------------


def _edit(path, removed, added=0, status="modified", previous=None, similarity=None):
    return FileChange(path, status, previous, similarity, added, removed)


def _hunks_for(path, old, new):
    return parse_diff(_replaced_file(path, old, new))


def _scan(changes, base, *, title="Tidy the notes", criteria=(), hunks=()):
    return scan_destructive_edits(
        changes, base, criteria=criteria, pr_title=title, hunks=hunks,
    )


_OLD_DOC = [f"Step {n}: do the thing number {n}" for n in range(1, 61)]
_NEW_DOC = ["Short summary of the change."]


def test_a_doc_mostly_deleted_and_never_named_is_a_high_finding():
    hunks = _hunks_for("docs/deploy.md", _OLD_DOC, _NEW_DOC)
    found = _scan((_edit("docs/deploy.md", 60, 1),), {"docs/deploy.md": 60}, hunks=hunks)
    assert len(found) == 1 and found[0].severity == "high"
    assert found[0].rule_name == "destructive-edit"


def test_a_file_the_title_or_a_criterion_names_is_fine():
    hunks = _hunks_for("docs/deploy.md", _OLD_DOC, _NEW_DOC)
    change, base = (_edit("docs/deploy.md", 60, 1),), {"docs/deploy.md": 60}
    assert _scan(change, base, title="Rewrite the deploy doc", hunks=hunks) == ()
    criteria = (LinkedCriterion(1, 1, "docs/deploy.md describes the new flow"),)
    assert _scan(change, base, criteria=criteria, hunks=hunks) == ()


def test_moved_content_lock_files_tests_and_small_trims_are_fine():
    moved = parse_diff(
        _replaced_file("app/handlers.py", _OLD_DOC, _NEW_DOC)
        + _added_file("app/steps.py", _OLD_DOC[:30]),
    )
    assert _scan((_edit("app/handlers.py", 60, 1),), {"app/handlers.py": 60}, hunks=moved) == ()
    lock = _hunks_for("uv.lock", _OLD_DOC, _NEW_DOC)
    assert _scan((_edit("uv.lock", 60, 1),), {"uv.lock": 60}, hunks=lock) == ()
    test = _hunks_for("tests/test_x.py", _OLD_DOC, _NEW_DOC)
    assert _scan((_edit("tests/test_x.py", 60, 1),), {"tests/test_x.py": 60}, hunks=test) == ()
    small = _hunks_for("docs/deploy.md", _OLD_DOC[:10], _NEW_DOC)
    assert _scan((_edit("docs/deploy.md", 10, 1),), {"docs/deploy.md": 12}, hunks=small) == ()


def test_front_door_files_use_the_lower_floor_and_unknown_base_is_skipped():
    old = [f"Overview {n}" for n in range(10)]
    hunks = _hunks_for("AGENTS.md", old, ["Ticket notes"])
    assert len(_scan((_edit("AGENTS.md", 10, 1),), {"AGENTS.md": 12}, hunks=hunks)) == 1
    assert _scan((_edit("AGENTS.md", 10, 1),), {}, hunks=hunks) == ()


def test_a_rename_is_measured_from_its_old_path():
    hunks = _hunks_for("docs/new-name.md", _OLD_DOC, _NEW_DOC)
    change = _edit("docs/new-name.md", 60, 1, status="renamed", previous="docs/deploy.md")
    found = _scan((change,), {"docs/deploy.md": 60}, hunks=hunks)
    assert len(found) == 1
    assert "`docs/deploy.md` (renamed to `docs/new-name.md`)" in found[0].message


# --- file_changes ----------------------------------------------------------------


def test_file_changes_reads_status_rename_and_counts():
    diff = (
        _added_file("a.py", ["x = 1"])
        + "diff --git a/old.md b/new.md\nsimilarity index 40%\nrename from old.md\n"
        "rename to new.md\nindex 1..2 100644\n--- a/old.md\n+++ b/new.md\n"
        "@@ -1,2 +1,1 @@\n-one\n-two\n+three\n"
        + "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\nindex 3..0\n"
        "--- a/gone.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    )
    assert file_changes(diff) == (
        FileChange("a.py", "added", None, None, 1, 0),
        FileChange("new.md", "renamed", "old.md", 40, 1, 2),
        FileChange("gone.txt", "removed", None, None, 0, 2),
    )


# --- reference scan cost (#1364 review) -------------------------------------------


def test_reference_chain_resolves_through_a_referenced_new_file():
    from personas.code_reviewer.criteria_coverage import _unreferenced_new_files

    diff = (
        _added_file("ops/a_tool.sh", ["#!/bin/sh", "ops/b_tool.sh"])
        + _added_file("ops/b_tool.sh", ["#!/bin/sh", "echo b"])
        + _added_file("ops/c_tool.sh", ["#!/bin/sh", "ops/d_tool.sh"])
        + _added_file("ops/d_tool.sh", ["#!/bin/sh", "ops/c_tool.sh"])
        + _added_file(".github/workflows/run.yml", ["steps:", "  - run: ops/a_tool.sh"])
    )
    new = frozenset(c.path for c in file_changes(diff) if c.status == "added")
    # a is run by the workflow and runs b; c and d only name each other.
    assert _unreferenced_new_files(new, parse_diff(diff), "") == {"ops/c_tool.sh", "ops/d_tool.sh"}


def test_reference_scan_is_linear_on_a_large_diff():
    import time

    from personas.code_reviewer.criteria_coverage import _unreferenced_new_files

    # 150 new scripts, each naming the next, 300 lines apiece: 45,000 added
    # lines. The old rescan was one regex per token per file per pass.
    parts = []
    for i in range(150):
        body = ["#!/bin/sh", f"ops/tool_{i + 1:03d}.sh"] + [f"echo line {n} of tool {i}" for n in range(300)]
        parts.append(_added_file(f"ops/tool_{i:03d}.sh", body))
    diff = "".join(parts)
    hunks = parse_diff(diff)
    new = frozenset(c.path for c in file_changes(diff) if c.status == "added")
    start = time.perf_counter()
    inert = _unreferenced_new_files(new, hunks, "")
    elapsed = time.perf_counter() - start
    # Only tool_000 has no caller; every later tool is reached from it, but
    # tool_000 is itself a candidate, so the whole chain stays unreferenced.
    assert inert == new
    assert elapsed < 1.0, elapsed


def test_reference_scan_past_its_bounds_is_not_evaluated_and_calls_nothing_inert():
    from personas.code_reviewer import criteria_coverage as cc

    many = "".join(_added_file(f"ops/tool_{i:03d}.sh", ["#!/bin/sh"]) for i in range(cc.MAX_REFERENCE_CANDIDATES + 1))
    new = frozenset(c.path for c in file_changes(many) if c.status == "added")
    assert cc._unreferenced_new_files(new, parse_diff(many), "") is None
    # Not evaluated never becomes a finding: no file counts as inert.
    boxes = ((83, 1, "open", "The nightly sync retries a failed upload three times"),)
    assert _findings(many, boxes=boxes, base={}) == ()
