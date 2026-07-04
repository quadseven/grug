"""PII guard — scan source for log calls that emit raw secret material.

Scoped to genuinely-secret patterns only (OAuth plaintext tokens, KMS
plaintext data keys, App private keys, webhook signing secrets). Does
NOT scan for `github_user_id` / `install_id` — those are intentionally
logged across grug today, with DD as the authorized observability sink
+ support-flow needing the raw id. Migrating identifiers to
`observability.fingerprint()` is a deliberate future call (see
specs/SLICE_PLAN_TEMPLATE.md §11), not a CI blocker today.

This test is the structural enforcement of the "no raw secrets in logs"
rule. New PRs that introduce a `log.X("...", oauth_access_token=...)`
(or equivalent) red-X here at PR time.

Pattern history: peer-review on PR #151 surfaced the audit category
"PII / secrets emitted into logs / spans / breadcrumbs" (audit.md Stage
8). This guard catches the simple textual form; structured-attribute
leaks via error-tracker breadcrumbs need a separate runtime check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


SERVICE_DIR = Path(__file__).resolve().parent.parent  # services/api/

# Substring patterns that indicate a raw secret reference in a log call's
# args/extra. Each pattern matches the BARE secret-bearing field name —
# the encrypted/wrapped/fingerprinted form is always allowed.
RAW_SECRET_NAMES = (
    "oauth_access_token",
    "oauth_refresh_token",
    "app_private_key",
    "webhook_secret",
    "client_secret",
    "plaintext_dek",
    "plaintext_kek",
    "decrypted_token",
)

# Suffixes / wrappers that signal "this is the SAFE form" — skip findings
# whose containing line includes any of these.
SAFE_MARKERS = re.compile(
    r"_blob|_encrypted|_ciphertext|_wrapped|fingerprint\("
)

# Files that are exempt — typically the observability module itself
# (defines fingerprint), or test fixtures that intentionally seed
# raw values into a private DDB mock.
WHITELIST_RELATIVE = (
    "observability.py",
    "tests/",  # other tests may reference raw names in assertions
    "__pycache__",
    ".pyc",
)


def _candidate_files() -> list[Path]:
    # Post-extraction (#77/ADR-0014) the shared modules live in
    # services/_shared/ — scanning SERVICE_DIR alone would silently
    # shrink coverage to the service-local files only, so the shared
    # tree is scanned too (both suites scan it; overlap is harmless).
    shared_root = SERVICE_DIR.parent / "_shared"
    out: list[Path] = []
    for root in (SERVICE_DIR, shared_root):
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if any(skip in rel for skip in WHITELIST_RELATIVE):
                continue
            out.append(path)
    assert any(shared_root in p.parents for p in out), "shared root contributed no files"
    return out


def _scan_file(path: Path) -> list[str]:
    """Return one finding per raw-secret-in-log emission, or []."""
    findings: list[str] = []
    text = path.read_text()
    lines = text.splitlines()
    in_log_call = False
    log_call_buffer: list[tuple[int, str]] = []  # (line_no, line)
    paren_depth = 0

    for line_no, line in enumerate(lines, 1):
        # Track multi-line log calls by paren depth.
        if re.search(r"\blog\.(info|debug|warning|error|critical|exception)\(", line):
            in_log_call = True
            paren_depth = 0
            log_call_buffer = []
        if in_log_call:
            log_call_buffer.append((line_no, line))
            paren_depth += line.count("(") - line.count(")")
            if paren_depth <= 0:
                # End of log call — scan the buffer.
                whole = "\n".join(l for _, l in log_call_buffer)
                if not SAFE_MARKERS.search(whole):
                    for secret in RAW_SECRET_NAMES:
                        if secret in whole:
                            findings.append(
                                f"{path.relative_to(SERVICE_DIR.parent.parent)}:"
                                f"{log_call_buffer[0][0]} — `{secret}` referenced in log call"
                            )
                            break
                in_log_call = False
                log_call_buffer = []
                paren_depth = 0
    return findings


def test_no_raw_secrets_in_log_emissions():
    """PII guard — fails if any `.py` outside the whitelist references a
    raw-secret field name inside a log.X() call without a SAFE_MARKER
    (`_blob`, `_encrypted`, `_ciphertext`, `_wrapped`, or `fingerprint(`)."""
    all_findings: list[str] = []
    for path in _candidate_files():
        all_findings.extend(_scan_file(path))
    assert not all_findings, (
        "Raw secret field name referenced in log emission. Wrap with "
        "`fingerprint()` from observability OR log the `_blob` / "
        "`_encrypted` form, not the plaintext:\n  "
        + "\n  ".join(all_findings)
    )


def test_fingerprint_helper_is_importable():
    """Sanity: the helper this guard expects exists + is callable."""
    from observability import fingerprint

    fp1 = fingerprint("alice")
    fp2 = fingerprint("alice")
    fp3 = fingerprint("bob")
    assert isinstance(fp1, str)
    assert len(fp1) == 12, f"fingerprint should be 12 hex chars, got {len(fp1)}"
    assert fp1 == fp2, "fingerprint must be stable within a process"
    assert fp1 != fp3, "fingerprint must distinguish different values"
    assert "alice" not in fp1, "fingerprint must not leak the underlying value"
