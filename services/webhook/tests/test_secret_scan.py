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


def test_dedups_same_secret_across_lines():
    # Same credential repeated in a file -> reported once (content-dedup, like
    # SCA; bounds judge cost).
    diff = _diff(".env", f"K1={_AWS_KEY}", f"K2={_AWS_KEY}")
    assert len(scan_secrets(_hunks(diff))) == 1


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
    # judge suppressed the only candidate -> no inline secret finding published
    if posted_review:
        assert not any(".env" in c.path or "README" in c.path for c in posted_review[0].comments)
    assert posted_check and posted_check[0].conclusion != "failure"
