"""PII guard — scan source for log calls that emit raw secret material.

Scoped to genuinely-secret patterns only (OAuth plaintext tokens, KMS
plaintext data keys, App private keys, webhook signing secrets). Does
NOT scan for `github_user_id` / `install_id` — those are intentionally
logged across grug today, with DD as the authorized observability sink
+ support-flow needing the raw id. Migrating identifiers to
`observability.fingerprint()` is a deliberate future call (see
specs/SLICE_PLAN_TEMPLATE.md §11), not a CI blocker today.

Webhook-side twin of services/api/tests/test_log_pii_guard.py (tests
stay per-service and are allowed to differ - ADR-0014); kept
substantially identical for clarity. Diverge only when the webhook's
threat model genuinely requires different scanning rules. Both twins
also scan services/_shared/ (overlap is deliberate).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


SERVICE_DIR = Path(__file__).resolve().parent.parent  # services/webhook/

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

SAFE_MARKERS = re.compile(
    r"_blob|_encrypted|_ciphertext|_wrapped|fingerprint\("
)

WHITELIST_RELATIVE = (
    "observability.py",
    "tests/",
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
    findings: list[str] = []
    text = path.read_text()
    lines = text.splitlines()
    in_log_call = False
    log_call_buffer: list[tuple[int, str]] = []
    paren_depth = 0

    for line_no, line in enumerate(lines, 1):
        if re.search(r"\blog\.(info|debug|warning|error|critical|exception)\(", line):
            in_log_call = True
            paren_depth = 0
            log_call_buffer = []
        if in_log_call:
            log_call_buffer.append((line_no, line))
            paren_depth += line.count("(") - line.count(")")
            if paren_depth <= 0:
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
    """PII guard — same shape as the api-side test."""
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
    assert len(fp1) == 12
    assert fp1 == fp2
    assert fp1 != fp3
    assert "alice" not in fp1
