"""PR-added inline suppressions do not hide analyzer findings (#1579).

The engines run with in-file suppression off; suppression_scope puts back the
ones already on the base. Each seam is checked: the scope helper itself, the
ruff mapping, both opengrep mappings (Elder analyzers and SAST), the SAST
judge's advisory downgrade, and the engine flags that turn in-file
suppression off.
"""
from __future__ import annotations

import json
import shutil
from unittest.mock import MagicMock, patch

import pytest

import personas.code_reviewer.analyzers as an
import personas.code_reviewer.lint as lint_mod
from llm_client import FindingJudgement
from personas.code_reviewer import sast
from personas.code_reviewer.diff_parser import DiffHunk, parse_diff
from personas.code_reviewer.suppression_scope import (
    PR_ADDED_NOTE, opengrep_scope, ruff_scope,
)


def _hunk(path: str, body: str, new_start: int = 1) -> DiffHunk:
    return DiffHunk(file_path=path, new_start=new_start,
                    new_lines=frozenset(), body=body)


# --- the scope helper ------------------------------------------------------

def test_nosemgrep_on_added_line_is_pr_added():
    src = "a\neval(x) // nosemgrep\n"
    assert opengrep_scope(src, 2, "elder-js-dynamic-eval", {2}) == "pr_added"


def test_nosemgrep_on_base_line_above_is_honored():
    src = "// nosemgrep: elder-js-dynamic-eval\neval(x)\n"
    assert opengrep_scope(src, 2, "elder-js-dynamic-eval", {2}) == "base"


def test_nosemgrep_for_another_rule_does_not_match():
    src = "eval(x) // nosemgrep: elder-js-other\n"
    assert opengrep_scope(src, 1, "elder-js-dynamic-eval", set()) == "none"


def test_nosemgrep_inside_a_string_is_not_a_directive():
    src = 'const note = "nosemgrep";\neval(x);\n'
    assert opengrep_scope(src, 2, "elder-js-dynamic-eval", {2}) == "none"


def test_nosemgrep_matches_a_dotted_rule_id_suffix():
    src = "eval(x) // nosemgrep: dynamic-eval\n"
    assert opengrep_scope(src, 1, "rules.dynamic-eval", set()) == "base"


def test_noqa_scopes():
    assert ruff_scope("eval(x)  # noqa: S307\n", 1, "S307", {1}) == "pr_added"
    assert ruff_scope("eval(x)  # noqa\n", 1, "S307", {1}) == "pr_added"
    assert ruff_scope("eval(x)  # noqa: B023\n", 1, "S307", {1}) == "none"
    assert ruff_scope("# ruff: noqa: S307\neval(x)\n", 2, "S307", {2}) == "base"
    assert ruff_scope("# ruff: noqa: S307\neval(x)\n", 2, "S307", {1, 2}) == "pr_added"
    assert ruff_scope("eval(x)\n", 1, "S307", {1}) == "none"


# --- ruff ------------------------------------------------------------------

def _ruff_hit(path: str, row: int, code: str) -> dict:
    return {"filename": f"/scan/{path}", "location": {"row": row},
            "code": code, "message": "Use of possibly insecure function"}


def test_ruff_security_finding_with_pr_added_noqa_still_surfaces():
    src = "x = 1\neval(user)  # noqa: S307\n"
    (f,) = lint_mod._map_results([_ruff_hit("a.py", 2, "S307")], "/scan/",
                                 {"a.py": {2}}, {"a.py": src})
    assert f.line == 2 and "S307" in f.message
    assert f.severity == "medium"  # advisory, never blocking
    assert PR_ADDED_NOTE in f.message


def test_ruff_base_file_level_noqa_still_hides_the_finding():
    src = "# ruff: noqa: S307\neval(user)\n"
    out = lint_mod._map_results([_ruff_hit("a.py", 2, "S307")], "/scan/",
                                {"a.py": {2}}, {"a.py": src})
    assert out == ()


def test_ruff_noqa_for_a_different_rule_does_not_hide_the_finding():
    src = "eval(user)  # noqa: B023\n"
    (f,) = lint_mod._map_results([_ruff_hit("a.py", 1, "S307")], "/scan/",
                                 {"a.py": {1}}, {"a.py": src})
    assert f.severity == "high" and PR_ADDED_NOTE not in f.message


def test_ruff_runs_with_noqa_ignored():
    seen: list[list[str]] = []

    def fake_run(argv, **_kw):
        seen.append(argv)
        return MagicMock(returncode=0, stdout="[]", stderr="")

    with patch.object(lint_mod.subprocess, "run", fake_run):
        lint_mod.scan_ruff((_hunk("a.py", "@@ -0,0 +1 @@\n+x = 1\n"),),
                           {"a.py": "x = 1\n"}, enabled=True)
    assert seen and "--ignore-noqa" in seen[0]


def test_deferred_recheck_still_honors_noqa_at_head():
    """ruff_hits has no PR context, so every suppression counts as the
    repo's own, as it did before the engine stopped reading them."""
    payload = json.dumps([_ruff_hit("a.py", 1, "S307")]).replace("/scan/", "")

    def fake_run(argv, **_kw):
        prefix = argv[-1].rstrip("/") + "/"
        return MagicMock(returncode=1, stderr="",
                         stdout=payload.replace('"a.py"', json.dumps(prefix + "a.py")))

    with patch.object(lint_mod.subprocess, "run", fake_run):
        assert lint_mod.ruff_hits({"a.py": "eval(user)  # noqa: S307\n"}) == ()
        assert len(lint_mod.ruff_hits({"a.py": "eval(user)\n"})) == 1


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_real_ruff_reports_a_line_carrying_a_pr_added_noqa():
    src = "def f(user):\n    return eval(user)  # noqa: S307\n"
    body = "@@ -0,0 +1,2 @@\n+def f(user):\n+    return eval(user)  # noqa: S307\n"
    out = lint_mod.scan_ruff((_hunk("a.py", body),), {"a.py": src}, enabled=True)
    assert any("S307" in f.message and PR_ADDED_NOTE in f.message for f in out)


# --- Elder opengrep analyzer -----------------------------------------------

def _og(path: str, line: int, rule: str = "elder-js-dynamic-eval") -> dict:
    return {"path": path, "start": {"line": line}, "check_id": rule,
            "extra": {"message": "Dynamic eval.", "severity": "WARNING"}}


def test_opengrep_finding_with_pr_added_nosemgrep_still_surfaces():
    src = "a\neval(x) // nosemgrep\n"
    (f,) = an.map_opengrep_results({"results": [_og("a.js", 2)]},
                                   {"a.js": {2}}, {"a.js": src})
    assert f.line == 2 and PR_ADDED_NOTE in f.message


def test_opengrep_base_nosemgrep_still_hides_the_finding():
    src = "// nosemgrep: elder-js-dynamic-eval\neval(x)\n"
    assert an.map_opengrep_results({"results": [_og("a.js", 2)]},
                                   {"a.js": {2}}, {"a.js": src}) == ()


def test_opengrep_nosemgrep_for_another_rule_does_not_hide():
    src = "eval(x) // nosemgrep: elder-js-other\n"
    (f,) = an.map_opengrep_results({"results": [_og("a.js", 1)]},
                                   {"a.js": {1}}, {"a.js": src})
    assert PR_ADDED_NOTE not in f.message


def test_elder_opengrep_runs_with_nosem_disabled(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(argv, **_kw):
        seen.append(argv)
        return MagicMock(returncode=0, stdout='{"results": []}', stderr="")

    monkeypatch.setattr(an, "_ELDER_RULES_DIR", str(tmp_path))
    monkeypatch.setattr(an.subprocess, "run", fake_run)
    ctx = an._Context((_hunk("a.js", "@@ -0,0 +1 @@\n+x\n"),), {"a.js": "x\n"},
                      an.RepoAnalyzerConfig(), 10.0)
    an._run_opengrep(ctx)
    assert seen and "--disable-nosem" in seen[0]


# --- SAST opengrep ---------------------------------------------------------

def _sast_result(path: str, line: int, rule: str = "grug-sqli") -> dict:
    return {"path": path, "start": {"line": line}, "check_id": rule,
            "extra": {"metadata": {"vuln_class": "sql-injection"}, "lines": "q"}}


def test_sast_candidate_with_pr_added_nosemgrep_still_surfaces():
    src = "x\nq = 'a' + uid  # nosemgrep\n"
    (c,) = sast._map_opengrep_results(
        {"results": [_sast_result("db.py", 2)]}, {"db.py": {1, 2}}, "",
        {"db.py": src}, {"db.py": {2}})
    assert c.line == 2 and c.pr_added_suppression


def test_sast_base_nosemgrep_on_a_context_line_still_hides_it():
    # Line 2 is a context line next to a removal: in the hunk, not added.
    src = "x\nq = 'a' + uid  # nosemgrep: grug-sqli\n"
    assert sast._map_opengrep_results(
        {"results": [_sast_result("db.py", 2)]}, {"db.py": {1, 2}}, "",
        {"db.py": src}, {"db.py": {1}}) == ()


def test_sast_nosemgrep_for_another_rule_does_not_hide():
    src = "q = 'a' + uid  # nosemgrep: grug-other\n"
    (c,) = sast._map_opengrep_results(
        {"results": [_sast_result("db.py", 1)]}, {"db.py": {1}}, "",
        {"db.py": src}, {"db.py": set()})
    assert not c.pr_added_suppression


def test_sast_judge_keeps_pr_added_suppression_advisory():
    c = sast.Candidate("sql-injection", "auth.py", 2, "q", pr_added_suppression=True)
    hunks = parse_diff("diff --git a/auth.py b/auth.py\n--- a/auth.py\n"
                       "+++ b/auth.py\n@@ -0,0 +1,2 @@\n+x\n+y\n")
    judged = (FindingJudgement(finding_index=0, is_real_bug=True, reasoning="real"),)
    with patch("personas.code_reviewer.sast.judge_findings", return_value=judged):
        (f,) = sast.judge_candidates((c,), hunks, installation_id=1)
    assert f.severity == "medium" and PR_ADDED_NOTE in f.message


def test_sast_opengrep_runs_with_nosem_disabled(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(argv, **_kw):
        seen.append(argv)
        return MagicMock(returncode=0, stdout='{"results": [], "errors": []}', stderr="")

    monkeypatch.setattr(sast, "_RULES_DIR", str(tmp_path))
    monkeypatch.setattr(sast.subprocess, "run", fake_run)
    sast.scan_opengrep((_hunk("a.py", "@@ -0,0 +1 @@\n+x\n"),), {"a.py": "x\n"})
    assert seen and "--disable-nosem" in seen[0]
