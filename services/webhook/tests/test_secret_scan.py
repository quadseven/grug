"""Tests for committed-secret detection (#436, ADR-0007 Track 1 slice 2).

Pure, file-type-agnostic detection over added diff lines (CI-safe, no network)
plus two dispatch integration tests proving (a) a real leaked secret the judge
KEEPS publishes via the existing path, and (b) a known docs example the judge
SUPPRESSES is not published. No raw secret value is ever echoed into a finding
(the no-echo invariant).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from llm_client import Backend, FindingJudgement, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer import secret_scan
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.secret_scan import EXPOSED_SECRET, scan_secrets

# A fake AWS access key id (canonical docs EXAMPLE shape) and a fake GitHub
# token - both are recognizable formats but NOT live credentials.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_GH_TOKEN = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
_HIGH_ENTROPY = "aB3xY7kL9mNp2qR5sT8u"  # 20 chars, mixed -> clears the entropy gate


def _hunks(diff):
    return parse_diff(diff)


def _diff(path, *added):
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added)} @@\n"
        f"{body}"
    )


# --- provider-pattern recall (any file type) -------------------------------


def test_flags_aws_key_in_env_file():
    cands = scan_secrets(_hunks(_diff(".env", f"AWS_SECRET_ACCESS_KEY={_AWS_KEY}")))
    assert len(cands) == 1
    assert cands[0].vuln_class == EXPOSED_SECRET
    assert cands[0].file == ".env" and cands[0].line == 1


def test_flags_github_token_in_yaml():
    cands = scan_secrets(_hunks(_diff("config.yaml", f"  token: {_GH_TOKEN}")))
    assert len(cands) == 1 and cands[0].vuln_class == EXPOSED_SECRET


def test_flags_pem_private_key_header():
    cands = scan_secrets(_hunks(_diff("id_rsa", "-----BEGIN RSA PRIVATE KEY-----")))
    assert len(cands) == 1 and cands[0].vuln_class == EXPOSED_SECRET


# --- generic high-entropy assignment ---------------------------------------


def test_flags_generic_high_entropy_assignment():
    cands = scan_secrets(_hunks(_diff("app.js", f'const api_key = "{_HIGH_ENTROPY}";')))
    assert len(cands) == 1 and cands[0].vuln_class == EXPOSED_SECRET


def test_flags_generic_yaml_colon_bare_value():
    # The generic rule's `:` separator + bare (unquoted) value branch - distinct
    # from the `=`/quoted path and from provider patterns (the value is not a
    # provider format, so it exercises _GENERIC_RE, not a short-circuit).
    cands = scan_secrets(_hunks(_diff("settings.yaml", f"app_token: {_HIGH_ENTROPY}")))
    assert len(cands) == 1 and cands[0].vuln_class == EXPOSED_SECRET


def test_ignores_low_entropy_placeholder():
    # len < 16 placeholder AND a 16-char low-entropy value: neither is flagged
    # (the judge never even sees them - the detector's entropy/len gate drops
    # them so a docs snippet with `api_key = "your-key-here"` is silent).
    diff = _diff(
        "README.md",
        'api_key = "your-key-here"',
        'secret = "passwordpassword"',
    )
    assert scan_secrets(_hunks(diff)) == ()


def test_non_secret_assignment_ignored():
    # A high-entropy value with a NON-secret key name is not a generic hit.
    assert scan_secrets(_hunks(_diff("app.py", f'username = "{_HIGH_ENTROPY}"'))) == ()


# --- diff-scoping + dedup + cap --------------------------------------------


def test_ignores_removed_and_context_lines():
    diff = (
        "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n"
        "@@ -1,2 +1,2 @@\n"
        f"-OLD={_AWS_KEY}\n"          # removed -> not introduced
        f" CONTEXT={_GH_TOKEN}\n"     # context -> not introduced
        "+NEW=plainvalue\n"
    )
    assert scan_secrets(_hunks(diff)) == ()


def test_skips_oversized_line():
    # A real token buried in a 4 KB+ minified blob is skipped (ReDoS/cost bound).
    giant = "x" * (secret_scan._MAX_LINE_LEN + 1) + _AWS_KEY
    assert scan_secrets(_hunks(_diff("bundle.min.js", giant))) == ()


def test_aggregate_scan_budget_stops():
    # Past _MAX_SCAN_BYTES of added text, scanning stops - a real token on a line
    # beyond the budget is not found (aggregate work bound, mirrors SAST).
    filler = [f"comment line {i} " + "x" * 200 for i in range(secret_scan._MAX_SCAN_BYTES // 200 + 50)]
    added = [*filler, f"LEAK={_AWS_KEY}"]  # token is past the byte budget
    assert scan_secrets(_hunks(_diff("big.txt", *added))) == ()


def test_pathological_line_is_linear_time():
    # A 4095-char near-miss of the generic key regex (no `=`) must NOT backtrack
    # super-linearly. Bounded windows keep it linear; this returns fast.
    import time
    line = "apikey" + "a" * 4000  # matches the key class, never reaches `:=`
    start = time.monotonic()
    assert scan_secrets(_hunks(_diff("x.txt", line))) == ()
    assert time.monotonic() - start < 1.0


def test_line_number_anchored_after_context_lines():
    # A secret added after N context lines in a hunk that does NOT start at line
    # 1 must anchor to its true new-side line number (the `lineno += 1` on the
    # context-line branch). Every other test's hunk starts at line 1 with only
    # added lines, so this is the only guard against mis-anchoring real hunks.
    diff = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        "@@ -10,4 +10,5 @@\n"
        " def f():\n"                                   # context -> new line 10
        "     x = 1\n"                                   # context -> new line 11
        f'+    api_key = "{_HIGH_ENTROPY}"\n'            # added   -> new line 12
        "     return x\n"                                # context -> new line 13
    )
    cands = scan_secrets(_hunks(diff))
    assert len(cands) == 1 and cands[0].line == 12


def test_dedups_same_secret_across_lines():
    # Same credential repeated in a file -> reported once (content-dedup, like
    # SCA; bounds judge cost).
    diff = _diff(".env", f"K1={_AWS_KEY}", f"K2={_AWS_KEY}")
    assert len(scan_secrets(_hunks(diff))) == 1


def test_distinct_secrets_sharing_mask_not_collapsed():
    # Two DIFFERENT credentials that mask to the same first4...last4 must both be
    # reported - dedup is on the exact value, not the lossy mask.
    a, b = "AKIABBBBBBBBBBBBWXYZ", "AKIACCCCCCCCCCCCWXYZ"  # both -> AKIA...WXYZ
    cands = scan_secrets(_hunks(_diff(".env", f"K1={a}", f"K2={b}")))
    assert len(cands) == 2


def test_caps_at_max_secrets():
    # Distinct tokens (unique 36-char suffix) so content-dedup does not collapse
    # them; the cap is what bounds the count.
    added = [f"KEY{i}=ghp_{i:036d}" for i in range(secret_scan._MAX_SECRETS + 25)]
    cands = scan_secrets(_hunks(_diff(".env", *added)))
    assert len(cands) == secret_scan._MAX_SECRETS


# --- no-echo invariant -----------------------------------------------------


def test_snippet_masks_secret_value():
    cands = scan_secrets(_hunks(_diff(".env", f"AWS_SECRET_ACCESS_KEY={_AWS_KEY}")))
    snippet = cands[0].snippet
    assert _AWS_KEY not in snippet  # the raw value is never echoed
    assert "AKIA" in snippet         # but a recognizable prefix survives for the judge


def test_generic_snippet_masks_value():
    cands = scan_secrets(_hunks(_diff("app.js", f'api_key = "{_HIGH_ENTROPY}";')))
    assert _HIGH_ENTROPY not in cands[0].snippet


# --- dispatch integration --------------------------------------------------


def _base_payload():
    return {
        "action": "opened", "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {"number": 7, "head": {"sha": "abcd1234"}},
    }


def _wire_common(monkeypatch, posted_check, posted_review):
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(
        cr_dispatch, "review_diff",
        lambda *a, **kw: LlmReviewResponse(kind="reviewed", findings=(), backend_used=Backend.POOLSIDE, model_name="laguna"),
    )
    monkeypatch.setattr(cr_dispatch, "scan_candidates", lambda *a, **kw: ())
    monkeypatch.setattr(cr_dispatch, "scan_dependencies", lambda hunks: ())
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda t, o, r, result, external_id=None: posted_check.append(result) or {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda t, o, r, *, pull_number, result: posted_review.append(result) or {"id": 2})


def test_dispatch_publishes_exposed_secret_finding(monkeypatch):
    """#436 AC: a real leaked secret the judge KEEPS publishes via the existing
    check-run + inline-review path."""
    posted_check, posted_review = [], []
    _wire_common(monkeypatch, posted_check, posted_review)
    monkeypatch.setattr(
        "personas.code_reviewer.sast.judge_findings",
        lambda *a, **kw: (FindingJudgement(finding_index=0, is_real_bug=True, reasoning="a live-looking AWS key committed to .env"),),
    )
    secret_diff = _diff(".env", f"AWS_SECRET_ACCESS_KEY={_AWS_KEY}")
    r = MagicMock(); r.status_code = 200; r.raise_for_status = MagicMock(); r.text = secret_diff
    with patch("httpx.get", return_value=r):
        cr_dispatch.dispatch_code_review(_base_payload(), blocking=True)
    assert posted_review, "a review should be posted"
    inline = posted_review[0].comments
    assert any(c.path == ".env" and c.line == 1 for c in inline)
    assert _AWS_KEY not in inline[0].body  # no-echo invariant holds end-to-end
    assert posted_check[0].conclusion == "failure"  # blocking + high-severity secret


def test_dispatch_suppresses_example_secret(monkeypatch):
    """#436 AC2: a candidate the judge rejects (a docs example) is NOT published."""
    posted_check, posted_review = [], []
    _wire_common(monkeypatch, posted_check, posted_review)
    monkeypatch.setattr(
        "personas.code_reviewer.sast.judge_findings",
        lambda *a, **kw: (FindingJudgement(finding_index=0, is_real_bug=False, reasoning="canonical AWS docs EXAMPLE key, not a real credential"),),
    )
    secret_diff = _diff("README.md", f"export AWS_ACCESS_KEY_ID={_AWS_KEY}  # docs example")
    r = MagicMock(); r.status_code = 200; r.raise_for_status = MagicMock(); r.text = secret_diff
    with patch("httpx.get", return_value=r):
        cr_dispatch.dispatch_code_review(_base_payload(), blocking=True)
    # judge suppressed the only candidate -> no inline secret finding published.
    # Unconditional: if suppression broke and a README.md finding leaked, this
    # fails regardless of what else was posted.
    assert posted_check and posted_check[0].conclusion != "failure"
    leaked = [c for c in (posted_review[0].comments if posted_review else []) if c.path == "README.md"]
    assert not leaked, "a judge-suppressed example secret must not be published"


def test_judge_reasoning_cannot_leak_secret(monkeypatch):
    """#436 no-echo (peer-review BLOCK fix): the judge sees full file content and
    could quote the raw secret in its reasoning, which is published verbatim for
    other classes. An exposed-secret finding must use a FIXED rationale, never
    the judge's free text."""
    posted_check, posted_review = [], []
    _wire_common(monkeypatch, posted_check, posted_review)
    monkeypatch.setattr(
        "personas.code_reviewer.sast.judge_findings",
        lambda *a, **kw: (FindingJudgement(finding_index=0, is_real_bug=True, reasoning=f"the key {_AWS_KEY} is live and reaches prod"),),
    )
    r = MagicMock(); r.status_code = 200; r.raise_for_status = MagicMock()
    r.text = _diff(".env", f"AWS_SECRET_ACCESS_KEY={_AWS_KEY}")
    with patch("httpx.get", return_value=r):
        cr_dispatch.dispatch_code_review(_base_payload(), blocking=True)
    assert posted_review and posted_review[0].comments
    body = posted_review[0].comments[0].body
    assert _AWS_KEY not in body, "judge reasoning must not echo the raw secret"
    assert "rotate" in body.lower()  # the fixed rationale is used instead


def test_dispatch_bounds_candidates_to_judge_budget(monkeypatch):
    """#436 (peer-review BLOCK fix): a flood of candidates must NOT exceed the
    judge cap (which fail-closes to zero findings); dispatch truncates first."""
    from personas.code_reviewer.sast import Candidate

    posted_check, posted_review = [], []
    _wire_common(monkeypatch, posted_check, posted_review)
    flood = tuple(Candidate(EXPOSED_SECRET, ".env", i + 1, f"masked-{i}") for i in range(40))
    monkeypatch.setattr(cr_dispatch, "scan_secrets", lambda hunks: flood)
    captured = {}

    def _judge(reprs, *a, **kw):
        captured["n"] = len(reprs)
        return ()

    monkeypatch.setattr("personas.code_reviewer.sast.judge_findings", _judge)
    r = MagicMock(); r.status_code = 200; r.raise_for_status = MagicMock()
    r.text = _diff("app.py", "x = 1")
    with patch("httpx.get", return_value=r):
        cr_dispatch.dispatch_code_review(_base_payload(), blocking=True)
    from llm_client import _JUDGE_MAX_FINDINGS
    assert captured.get("n", 0) <= _JUDGE_MAX_FINDINGS  # judge ran on a bounded set


def test_secret_candidates_survive_truncation(monkeypatch):
    """#436 (peer-review BLOCK fix): under a candidate flood, secret candidates
    (highest exploitability) are judged FIRST - they must not be the ones the
    budget truncation drops."""
    from personas.code_reviewer.sast import Candidate
    from llm_client import _JUDGE_MAX_FINDINGS

    posted_check, posted_review = [], []
    _wire_common(monkeypatch, posted_check, posted_review)
    sast_flood = tuple(Candidate("sql-injection", "a.py", i + 1, f"s{i}") for i in range(30))
    secrets = (
        Candidate(EXPOSED_SECRET, ".env", 1, "AWS access key id (value masked: AKIA...MPLE)"),
        Candidate(EXPOSED_SECRET, ".env", 2, "GitHub token (value masked: ghp_...0009)"),
    )
    monkeypatch.setattr(cr_dispatch, "scan_candidates", lambda *a, **kw: sast_flood)
    monkeypatch.setattr(cr_dispatch, "scan_secrets", lambda hunks: secrets)
    captured = {}
    monkeypatch.setattr(
        "personas.code_reviewer.sast.judge_findings",
        lambda reprs, *a, **kw: captured.update(reprs=list(reprs)) or (),
    )
    r = MagicMock(); r.status_code = 200; r.raise_for_status = MagicMock()
    r.text = _diff("app.py", "x = 1")
    with patch("httpx.get", return_value=r):
        cr_dispatch.dispatch_code_review(_base_payload(), blocking=True)
    classes = [rp["rule_name"] for rp in captured["reprs"]]
    assert len(classes) <= _JUDGE_MAX_FINDINGS
    assert classes.count(EXPOSED_SECRET) == 2, "both secrets survived truncation"


def test_dispatch_survives_secret_scan_failure(monkeypatch):
    """#436 AC4 (no regression): the wiring's core promise - a scan_secrets
    exception is swallowed by the dispatch security-block guard and the core
    review still publishes."""
    posted_check, posted_review = [], []
    _wire_common(monkeypatch, posted_check, posted_review)

    def _boom(hunks):
        raise RuntimeError("secret scan exploded")

    monkeypatch.setattr(cr_dispatch, "scan_secrets", _boom)
    r = MagicMock(); r.status_code = 200; r.raise_for_status = MagicMock()
    r.text = _diff("app.py", "x = 1")
    with patch("httpx.get", return_value=r):
        cr_dispatch.dispatch_code_review(_base_payload(), blocking=True)
    assert posted_check, "core review still publishes despite a scan failure"
