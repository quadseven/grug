"""Tests for SCA (dependency-CVE) detection (#434, ADR-0007 Track 1).

Pure extraction + mocked pip-audit (CI-safe, no network) + one dispatch
integration proving a vulnerable dep publishes via the existing path. A real
pip-audit test runs locally / where the binary + network exist (skips else).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from llm_client import Backend, FindingJudgement, LlmReviewResponse
from personas.guard import dispatch as guard_dispatch  # security dispatch moved to Guard (#466)
from personas.code_reviewer import sca
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.sca import (
    VULNERABLE_DEPENDENCY,
    extract_changed_deps,
    scan_dependencies,
)


# --- pure extraction -------------------------------------------------------


def _hunks(diff):
    return parse_diff(diff)


def test_extracts_pinned_dep_from_requirements():
    diff = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n+++ b/requirements.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+flask==1.0.0\n"
        "+requests==2.5.0\n"
    )
    deps = extract_changed_deps(_hunks(diff))
    assert {(d.name, d.version) for d in deps} == {("flask", "1.0.0"), ("requests", "2.5.0")}
    assert all(d.file == "requirements.txt" for d in deps)
    assert {d.line for d in deps} == {1, 2}


def test_ignores_non_manifest_files():
    diff = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+flask==1.0.0\n"  # looks like a dep but it's a .py file
    )
    assert extract_changed_deps(_hunks(diff)) == ()


def test_ignores_unpinned_and_removed():
    diff = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n+++ b/requirements.txt\n"
        "@@ -1,2 +1,2 @@\n"
        "-oldpkg==1.0.0\n"        # removed -> not introduced
        "+unpinned>=2.0\n"        # unpinned -> not deterministically auditable
    )
    assert extract_changed_deps(_hunks(diff)) == ()


def test_dedups_same_dep():
    diff = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n+++ b/requirements.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+flask==1.0.0\n+flask==1.0.0\n"
    )
    assert len(extract_changed_deps(_hunks(diff))) == 1


# --- scan_dependencies (mocked pip-audit) ----------------------------------

_VULN_DIFF = (
    "diff --git a/requirements.txt b/requirements.txt\n"
    "--- a/requirements.txt\n+++ b/requirements.txt\n"
    "@@ -0,0 +1,1 @@\n"
    "+jinja2==2.4.1\n"
)


def _osv_resp(results):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"results": results})
    return r


def test_scan_flags_vulnerable_dep(monkeypatch):
    monkeypatch.setattr(
        sca.httpx, "post",
        lambda *a, **kw: _osv_resp([{"vulns": [{"id": "GHSA-462w-v97r-4m45"}]}]),
    )
    cands = scan_dependencies(_hunks(_VULN_DIFF))
    assert len(cands) == 1
    assert cands[0].vuln_class == VULNERABLE_DEPENDENCY
    assert cands[0].file == "requirements.txt" and cands[0].line == 1
    assert "GHSA-462w-v97r-4m45" in cands[0].snippet


def test_scan_clean_dep_no_candidate(monkeypatch):
    monkeypatch.setattr(sca.httpx, "post", lambda *a, **kw: _osv_resp([{}]))  # OSV: no vulns
    assert scan_dependencies(_hunks(_VULN_DIFF)) == ()


def test_scan_no_changed_deps_skips_osv(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(sca.httpx, "post", lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or _osv_resp([]))
    diff = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+x = 1\n"
    assert scan_dependencies(_hunks(diff)) == ()
    assert called["n"] == 0  # no deps -> OSV never queried


def test_scan_osv_unreachable_fails_safe(monkeypatch):
    def _boom(*a, **kw):
        raise sca.httpx.ConnectError("osv unreachable")
    monkeypatch.setattr(sca.httpx, "post", _boom)
    assert scan_dependencies(_hunks(_VULN_DIFF)) == ()


def test_scan_osv_bad_output_fails_safe(monkeypatch):
    bad = MagicMock(); bad.raise_for_status = MagicMock(); bad.json = MagicMock(side_effect=ValueError("not json"))
    monkeypatch.setattr(sca.httpx, "post", lambda *a, **kw: bad)
    assert scan_dependencies(_hunks(_VULN_DIFF)) == ()


# --- dispatch integration (SCA finding published) --------------------------


def test_dispatch_publishes_vulnerable_dep_finding(monkeypatch):
    """#434 AC: a vulnerable dep the judge KEEPS publishes via the existing
    check-run + inline-review path."""
    monkeypatch.setattr(guard_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"))
    # No SAST candidates; one SCA candidate.
    monkeypatch.setattr(guard_dispatch, "scan_candidates", lambda *a, **kw: ())
    monkeypatch.setattr(
        guard_dispatch, "scan_dependencies",
        lambda hunks: (sca.Candidate(VULNERABLE_DEPENDENCY, "requirements.txt", 1, "jinja2==2.4.1 (known advisories: GHSA-x)"),),
    )
    monkeypatch.setattr(
        "personas.code_reviewer.sast.judge_findings",
        lambda *a, **kw: (FindingJudgement(finding_index=0, is_real_bug=True, reasoning="reachable in a server-side template path"),),
    )
    posted_check, posted_review = [], []
    monkeypatch.setattr(guard_dispatch, "post_check_run", lambda t, o, r, result, external_id=None: posted_check.append(result) or {"id": 1})
    monkeypatch.setattr(guard_dispatch, "post_review", lambda t, o, r, *, pull_number, result: posted_review.append(result) or {"id": 2})

    r = MagicMock(); r.status_code = 200; r.raise_for_status = MagicMock()
    r.text = _VULN_DIFF
    payload = {
        "action": "opened", "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {"number": 7, "head": {"sha": "abcd1234"}},
    }
    with patch("httpx.get", return_value=r):
        guard_dispatch.dispatch_guard_review(payload, blocking=True)

    assert posted_review, "a review should be posted"
    inline = posted_review[0].comments
    assert any(c.path == "requirements.txt" and c.line == 1 for c in inline)
    assert posted_check[0].conclusion == "failure"  # blocking + high-severity vuln dep


# --- real pip-audit (skips without binary/network) -------------------------


@pytest.mark.skipif(not os.getenv("GRUG_SCA_LIVE_TEST"), reason="live OSV test (set GRUG_SCA_LIVE_TEST=1)")
def test_scan_real_osv_flags_known_vuln():
    """End-to-end against the REAL OSV API: a known-vulnerable pinned dep is
    flagged. Opt-in (needs network) so the per-PR CI gate stays deterministic;
    run locally with GRUG_SCA_LIVE_TEST=1."""
    cands = scan_dependencies(_hunks(_VULN_DIFF))  # jinja2==2.4.1 has known advisories
    assert any(c.vuln_class == VULNERABLE_DEPENDENCY and c.file == "requirements.txt" for c in cands)
