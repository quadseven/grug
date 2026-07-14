"""Pure tests for the SAST clear-text-secret-log detector (#400, ADR-0006).

NO LLM — the recall layer is deterministic. Built from real `parse_diff`
output so line-number computation is exercised end to end. The PRECISION layer
(the exploitability judge that suppresses the #391 FP) is tested separately
with mocked LLM judgement; here we assert the detector is LIBERAL (flags both
the real secret log AND the #391 param-name log as candidates — the judge
discriminates).
"""

from __future__ import annotations

import shutil
from unittest.mock import patch

import pytest

from llm_client import FindingJudgement
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer import sast
from personas.code_reviewer.sast import (
    CLEARTEXT_SECRET_LOG,
    Candidate,
    judge_candidates,
    scan_candidates,
    scan_semgrep,
)


def _hunks(diff: str):
    return parse_diff(diff)


def test_flags_real_secret_in_log():
    diff = (
        "diff --git a/auth.py b/auth.py\n"
        "--- a/auth.py\n"
        "+++ b/auth.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def on_login(user, password):\n"
        '+    logging.info("login user=%s password=%s", user, password)\n'
    )
    cands = scan_candidates(_hunks(diff))
    assert len(cands) == 1
    c = cands[0]
    assert c.vuln_class == CLEARTEXT_SECRET_LOG
    assert c.file == "auth.py"
    assert c.line == 2  # the logging line is new-side line 2
    assert "password" in c.snippet


def test_flags_the_391_public_config_path_log_as_candidate():
    """The #391 FP shape (logging an SSM param NAME) IS a candidate — recall is
    liberal; the JUDGE suppresses it later. Detector must NOT pre-filter it."""
    diff = (
        "diff --git a/secrets.py b/secrets.py\n"
        "--- a/secrets.py\n"
        "+++ b/secrets.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def load_secret(ssm_param_name):\n"
        '+    logging.info("loading secret from SSM param %s", ssm_param_name)\n'
    )
    cands = scan_candidates(_hunks(diff))
    assert len(cands) == 1
    assert cands[0].vuln_class == CLEARTEXT_SECRET_LOG


def test_innocuous_log_not_flagged():
    """A log with no secret-ish token is not a candidate (no detector FP)."""
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1,1 @@\n"
        '+    logging.info("user %s logged in", username)\n'
    )
    assert scan_candidates(_hunks(diff)) == ()


def test_secret_without_sink_not_flagged():
    """A hardcoded credential that is NOT logged is a different class (not this
    tracer's) — no sink -> no clear-text-secret-LOG candidate."""
    diff = (
        "diff --git a/conf.py b/conf.py\n"
        "--- a/conf.py\n"
        "+++ b/conf.py\n"
        "@@ -0,0 +1,1 @@\n"
        '+API_KEY = "sk-secret-value"\n'
    )
    assert scan_candidates(_hunks(diff)) == ()


def test_removed_line_not_flagged_and_line_numbers_correct():
    """A secret-log on a REMOVED line is not a candidate (it's being deleted);
    new-side line numbers advance over context + added lines only."""
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        " import logging\n"
        '-    logging.info("old password=%s", password)\n'
        '+    logging.info("new token=%s", token)\n'
    )
    cands = scan_candidates(_hunks(diff))
    assert len(cands) == 1
    # context line 1 (import), then the added line is new-side line 2.
    assert cands[0].line == 2
    assert "token" in cands[0].snippet


def test_scan_candidates_default_engine_is_builtin():
    """No engine configured -> builtin detector (the #400 zero-dep path)."""
    diff = (
        "diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n"
        "@@ -0,0 +1,1 @@\n"
        '+    logging.info("password=%s", password)\n'
    )
    assert len(scan_candidates(_hunks(diff), engine="builtin")) == 1


# --- Semgrep engine (#401): mocked subprocess (parse/map/diff-filter/budget) --

from unittest.mock import MagicMock  # noqa: E402

from personas.code_reviewer.diff_parser import DiffHunk  # noqa: E402


def _semgrep_json(results):
    r = MagicMock()
    r.returncode = 0  # semgrep success shape - non-zero is the run-failed degrade path
    r.stdout = json.dumps({"results": results, "errors": []})
    return r


import json  # noqa: E402


def _hunk(path, new_lines):
    body = "@@ -0,0 +1,%d @@\n" % max(new_lines) + "".join("+x\n" for _ in new_lines)
    return DiffHunk(file_path=path, new_start=min(new_lines), new_lines=frozenset(new_lines), body=body)


def test_scan_semgrep_maps_class_and_filters_to_added_lines(monkeypatch):
    """Maps metadata.vuln_class -> Candidate; keeps only findings on lines the
    PR added (a hit on an untouched line is dropped)."""
    results = [
        {"path": "auth.py", "start": {"line": 2}, "extra": {"metadata": {"vuln_class": "sql-injection"}, "lines": "q = ... + uid"}},
        {"path": "auth.py", "start": {"line": 99}, "extra": {"metadata": {"vuln_class": "ssrf"}, "lines": "pre-existing"}},
    ]
    monkeypatch.setattr(sast.subprocess, "run", lambda *a, **kw: _semgrep_json(results))
    hunks = (_hunk("auth.py", {1, 2}),)
    out = scan_semgrep(hunks, {"auth.py": "code\n"})
    assert len(out) == 1
    assert out[0].vuln_class == "sql-injection" and out[0].line == 2  # line 99 filtered (not added)


def test_scan_semgrep_no_file_contents_returns_empty():
    assert scan_semgrep((_hunk("a.py", {1}),), {}) == ()


def test_scan_semgrep_missing_binary_fails_safe(monkeypatch):
    def _missing(*a, **kw):
        raise FileNotFoundError("semgrep not installed")
    monkeypatch.setattr(sast.subprocess, "run", _missing)
    assert scan_semgrep((_hunk("a.py", {1}),), {"a.py": "x"}) == ()


def test_scan_semgrep_nonzero_exit_fails_safe(monkeypatch):
    """Version-dependent, semgrep can exit non-zero AND emit parseable
    JSON - that must degrade to () + log, never a silent zero-findings
    scan (#77 audit stage 2)."""
    r = MagicMock()
    r.returncode = 2
    r.stdout = json.dumps({"results": [], "errors": [{"message": "invalid rules"}]})
    r.stderr = "invalid configuration"
    monkeypatch.setattr(sast.subprocess, "run", lambda *a, **kw: r)
    out = scan_semgrep((_hunk("a.py", {1}),), {"a.py": "code\n"})
    assert out == ()


def test_scan_semgrep_missing_rules_dir_fails_safe(monkeypatch):
    """Post-#77 the rules dir resolves from the service cwd - a wrong
    working directory must degrade loudly to (), not scan without rules."""
    monkeypatch.setattr(sast, "_RULES_DIR", "/nonexistent/sast_rules")
    called = []
    monkeypatch.setattr(sast.subprocess, "run", lambda *a, **kw: called.append(a) or _semgrep_json([]))
    out = scan_semgrep((_hunk("a.py", {1}),), {"a.py": "code\n"})
    assert out == ()
    assert not called, "semgrep must not run without its rules dir"


def test_scan_semgrep_run_failure_fails_safe(monkeypatch):
    monkeypatch.setattr(sast.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(sast.subprocess.TimeoutExpired("semgrep", 60)))
    assert scan_semgrep((_hunk("a.py", {1}),), {"a.py": "x"}) == ()


def test_scan_semgrep_points_home_at_writable_scan_dir(monkeypatch):
    """Semgrep initializes ~/.semgrep at startup. The pods run as uid 10001
    with --no-create-home on a readOnlyRootFilesystem, so inheriting the pod
    HOME crashed semgrep with a mkdir PermissionError (exit 1) before it
    scanned anything - every production scan silently degraded to zero
    findings (2026-07-13, infra#1776 sweep; reproduced locally against real
    semgrep 1.169). The subprocess env must point HOME and XDG_CACHE_HOME
    inside the scan's own temp dir."""
    seen = {}
    def _capture(cmd, **kw):
        tmp = cmd[-1]
        seen["tmp"] = tmp
        seen["env"] = kw.get("env")
        return _semgrep_json([])
    monkeypatch.setattr(sast.subprocess, "run", _capture)
    scan_semgrep((_hunk("a.py", {1}),), {"a.py": "x = 1\n"})
    import os as _os
    assert seen["env"] is not None, "semgrep must run with an explicit env"
    assert seen["env"]["HOME"] == seen["tmp"]
    # XDG_CACHE_HOME must be a real subdirectory of the scan tmp dir, not just
    # a string sharing its prefix (a sibling like "<tmp>.cache" would pass a
    # bare startswith but still land outside the writable, self-cleaning dir).
    assert seen["env"]["XDG_CACHE_HOME"] == _os.path.join(seen["tmp"], ".cache")
    assert seen["env"]["XDG_CACHE_HOME"].startswith(seen["tmp"] + _os.sep)


def test_scan_semgrep_skips_files_over_byte_budget(monkeypatch):
    """AC5: a file beyond the byte budget is not scanned (cost bound)."""
    monkeypatch.setattr(sast, "_MAX_SCAN_BYTES", 50)
    seen_files = {}
    def _capture(cmd, **kw):
        # Count files actually written into the temp scan dir.
        tmp = cmd[-1]
        seen_files["count"] = sum(len(fs) for _, _, fs in __import__("os").walk(tmp))
        return _semgrep_json([])
    monkeypatch.setattr(sast.subprocess, "run", _capture)
    contents = {"small.py": "x" * 10, "huge.py": "y" * 1000}
    scan_semgrep((_hunk("small.py", {1}),), contents)
    assert seen_files["count"] == 1  # only small.py fit the budget


def test_scan_semgrep_rejects_path_traversal_in_file_path(monkeypatch):
    """A PR-controlled path that escapes the temp dir (../../etc/...) is NOT
    written (arbitrary-write guard); the safe file is still scanned."""
    import os as _os
    written = []
    def _capture(cmd, **kw):
        tmp = cmd[-1]
        for root, _d, files in _os.walk(tmp):
            for fn in files:
                written.append(_os.path.relpath(_os.path.join(root, fn), tmp))
        return _semgrep_json([])
    monkeypatch.setattr(sast.subprocess, "run", _capture)
    scan_semgrep((_hunk("ok.py", {1}),), {"ok.py": "x = 1\n", "../../etc/evil.py": "pwned"})
    assert "ok.py" in written
    assert all(".." not in w for w in written)  # the escaping path never landed


@pytest.mark.skipif(shutil.which("semgrep") is None, reason="semgrep not installed")
def test_scan_semgrep_real_engine_detects_multiple_classes():
    """End-to-end with the REAL semgrep over the vendored rules: a multi-class
    diff yields candidates of the right classes on the added lines."""
    diff = (
        "diff --git a/v.py b/v.py\n--- a/v.py\n+++ b/v.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+def f(conn, uid, host):\n"
        '+    conn.execute("SELECT * FROM t WHERE id=" + uid)\n'
        '+    import os; os.system("ping " + host)\n'
        "+    import pickle; pickle.loads(host)\n"
    )
    hunks = parse_diff(diff)
    file_contents = {
        "v.py": (
            "def f(conn, uid, host):\n"
            '    conn.execute("SELECT * FROM t WHERE id=" + uid)\n'
            '    import os; os.system("ping " + host)\n'
            "    import pickle; pickle.loads(host)\n"
        )
    }
    classes = {c.vuln_class for c in scan_semgrep(hunks, file_contents)}
    assert {"sql-injection", "command-injection", "unsafe-deserialization"} <= classes


def test_multiple_candidates_across_hunks():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -0,0 +1,1 @@\n"
        '+    log.error("secret=%s", secret)\n'
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
        "@@ -0,0 +1,1 @@\n"
        '+    print(f"api_key={api_key}")\n'
    )
    cands = scan_candidates(_hunks(diff))
    assert {c.file for c in cands} == {"a.py", "b.py"}
    assert len(cands) == 2


# --- exploitability judge (precision layer, mocked LLM) --------------------

_C_REAL = Candidate(CLEARTEXT_SECRET_LOG, "auth.py", 2, 'logging.info("password=%s", password)')
_C_FP = Candidate(CLEARTEXT_SECRET_LOG, "secrets.py", 2, 'logging.info("param %s", ssm_param_name)')
_HUNK = parse_diff(
    "diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n@@ -0,0 +1,2 @@\n+x\n+y\n"
)


def test_judge_keeps_exploitable_and_carries_rationale():
    judged = (FindingJudgement(finding_index=0, is_real_bug=True, reasoning="real secret reaches the log sink"),)
    with patch("personas.code_reviewer.sast.judge_findings", return_value=judged):
        out = judge_candidates((_C_REAL,), _HUNK, installation_id=1)
    assert len(out) == 1
    assert out[0].file == "auth.py" and out[0].line == 2
    assert out[0].rule_name == CLEARTEXT_SECRET_LOG
    assert "real secret reaches the log sink" in out[0].message  # exploitability rationale


def test_judge_message_labels_by_class_not_hardcoded():
    """The published message reflects the candidate's CLASS, not a hardcoded
    'clear-text logging' (regression guard for the #401/#434 multi-class fix)."""
    sqli = Candidate("sql-injection", "db.py", 3, 'execute("..." + uid)')
    judged = (FindingJudgement(finding_index=0, is_real_bug=True, reasoning="user input reaches the query"),)
    with patch("personas.code_reviewer.sast.judge_findings", return_value=judged):
        out = judge_candidates((sqli,), _HUNK, installation_id=1)
    assert "SQL injection" in out[0].message
    assert "clear-text logging" not in out[0].message.lower()


def test_judge_suppresses_the_391_false_positive():
    """The #391 shape judged not-a-bug -> suppressed (no Finding posted)."""
    judged = (FindingJudgement(finding_index=0, is_real_bug=False, reasoning="logs a public SSM param name, not a secret value"),)
    with patch("personas.code_reviewer.sast.judge_findings", return_value=judged):
        out = judge_candidates((_C_FP,), _HUNK, installation_id=1)
    assert out == ()


def test_judge_mixed_keeps_only_exploitable():
    judged = (
        FindingJudgement(finding_index=0, is_real_bug=True, reasoning="real"),
        FindingJudgement(finding_index=1, is_real_bug=False, reasoning="public name"),
    )
    with patch("personas.code_reviewer.sast.judge_findings", return_value=judged):
        out = judge_candidates((_C_REAL, _C_FP), _HUNK, installation_id=1)
    assert len(out) == 1 and out[0].file == "auth.py"


def test_judge_outage_fails_closed():
    """A judge LLM failure suppresses candidates (fail-closed) - never posts
    un-triaged candidates (the noisy-SAST failure precision prevents)."""
    with patch("personas.code_reviewer.sast.judge_findings", side_effect=RuntimeError("llm down")):
        out = judge_candidates((_C_REAL,), _HUNK, installation_id=1)
    assert out == ()


def test_judge_count_mismatch_suppresses_unjudged():
    """A judgement missing for a candidate index -> that candidate is suppressed
    (can't confirm exploitability), not posted on faith."""
    judged = (FindingJudgement(finding_index=0, is_real_bug=True, reasoning="real"),)  # only index 0
    with patch("personas.code_reviewer.sast.judge_findings", return_value=judged):
        out = judge_candidates((_C_REAL, _C_FP), _HUNK, installation_id=1)
    assert len(out) == 1  # index 1 unjudged -> suppressed


def test_judge_no_candidates_skips_llm():
    with patch("personas.code_reviewer.sast.judge_findings") as mock_j:
        out = judge_candidates((), _HUNK, installation_id=1)
    assert out == ()
    mock_j.assert_not_called()


# --- merge into the evaluation (with_extra_findings) ------------------------

from personas.code_reviewer.persona import (  # noqa: E402
    CodeReviewEvaluation,
    Finding,
    with_extra_findings,
)

_HIGH = Finding("auth.py", 2, "high", CLEARTEXT_SECRET_LOG, "secret in log", None)
_MED = Finding("x.py", 1, "medium", "style", "nit", None)


def test_merge_high_sast_finding_flips_clean_review_to_failure():
    clean = CodeReviewEvaluation(findings=(), conclusion="success")
    merged = with_extra_findings(clean, (_HIGH,))
    assert merged.findings == (_HIGH,)
    assert merged.conclusion == "failure"
    assert merged.passed is False


def test_merge_empty_is_noop():
    ev = CodeReviewEvaluation(findings=(_MED,), conclusion="success")
    assert with_extra_findings(ev, ()) is ev


def test_merge_high_overrides_degraded_neutral():
    """A judge-confirmed secret leak posts failure even if the main LLM review
    degraded (rare: the judge needs the LLM, but the rule is unambiguous)."""
    degraded = CodeReviewEvaluation(findings=(), conclusion="neutral", degraded_reason="all_failed")
    merged = with_extra_findings(degraded, (_HIGH,))
    assert merged.conclusion == "failure"


def test_merge_preserves_degraded_reason_and_counts():
    degraded = CodeReviewEvaluation(
        findings=(), conclusion="neutral", dropped_hallucinations=3, degraded_reason="parse_failed"
    )
    merged = with_extra_findings(degraded, (_MED,))  # medium, non-blocking
    assert merged.conclusion == "neutral"  # degraded preserved (no high/critical)
    assert merged.degraded_reason == "parse_failed"
    assert merged.dropped_hallucinations == 3


# --- end-to-end via dispatch (AC1: published through the existing path) -----

import httpx  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from llm_client import Backend, LlmReviewResponse  # noqa: E402
from personas.guard import dispatch as guard_dispatch  # noqa: E402  — security dispatch moved to Guard (#466)

_SECRET_DIFF = (
    "diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def login(user, password):\n"
    '+    logging.info("password=%s", password)\n'
)


def _diff_resp():
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.text = _SECRET_DIFF
    return r


def test_dispatch_publishes_kept_sast_finding_and_drives_verdict(monkeypatch):
    """#400 AC1: a clear-text-secret-log the judge KEEPS is published via the
    existing check-run + inline-review path; in blocking mode the high-severity
    SAST finding drives the verdict to failure. The Elder LLM review is clean
    (no findings) so the published finding is purely the SAST tracer's."""
    monkeypatch.setattr(guard_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"))
    # The exploitability judge KEEPS the candidate.
    monkeypatch.setattr(
        "personas.code_reviewer.sast.judge_findings",
        lambda *a, **kw: (FindingJudgement(finding_index=0, is_real_bug=True, reasoning="real secret reaches the log sink"),),
    )
    posted_check, posted_review = [], []
    monkeypatch.setattr(guard_dispatch, "post_check_run", lambda t, o, r, result, external_id=None: posted_check.append(result) or {"id": 1})
    monkeypatch.setattr(guard_dispatch, "post_review", lambda t, o, r, *, pull_number, result: posted_review.append(result) or {"id": 2})

    payload = {
        "action": "opened", "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {"number": 7, "head": {"sha": "abcd1234"}},
    }
    with patch("httpx.get", return_value=_diff_resp()):
        out = guard_dispatch.dispatch_guard_review(payload, blocking=True)

    assert out["persona"] == "guard"
    # The SAST finding reached the inline review on the real line.
    assert len(posted_review) == 1
    inline = posted_review[0].comments
    assert any(c.path == "auth.py" and c.line == 2 for c in inline)
    # Blocking mode + a high-severity confirmed secret leak -> failure verdict.
    assert posted_check[0].conclusion == "failure"


def test_dispatch_suppressed_sast_finding_not_published(monkeypatch):
    """#400 AC2: the #391-shape candidate the judge SUPPRESSES is NOT published
    (no inline comment, verdict stays clean)."""
    monkeypatch.setattr(guard_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(
        "personas.code_reviewer.sast.judge_findings",
        lambda *a, **kw: (FindingJudgement(finding_index=0, is_real_bug=False, reasoning="public param name, not a secret value"),),
    )
    posted_check, posted_review = [], []
    monkeypatch.setattr(guard_dispatch, "post_check_run", lambda t, o, r, result, external_id=None: posted_check.append(result) or {"id": 1})
    monkeypatch.setattr(guard_dispatch, "post_review", lambda t, o, r, *, pull_number, result: posted_review.append(result) or {"id": 2})

    payload = {
        "action": "opened", "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {"number": 7, "head": {"sha": "abcd1234"}},
    }
    with patch("httpx.get", return_value=_diff_resp()):
        guard_dispatch.dispatch_guard_review(payload, blocking=True)

    # No SAST inline comment, verdict not driven to failure by SAST.
    if posted_review:
        assert all(not (c.path == "auth.py" and c.line == 2) for c in posted_review[0].comments)
    assert posted_check[0].conclusion != "failure"
