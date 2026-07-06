"""Best-effort secret redaction - the ONE pattern set (leaf module).

Extracted from llm_client (#546 peer review) so the derived-block
renderers (best_practices, few_shot) can redact BEFORE their sanitizers
truncate: a PEM key cut mid-body no longer matches its BEGIN...END
pattern, so REDACTION MUST ALWAYS RUN BEFORE ANY TRUNCATION (the same
rule llm_client._redact_payload already documents). Leaf on purpose -
imports nothing internal, so the pure modules can depend on it without
pulling llm_client's heavy deps or risking an import cycle.

False positives are acceptable; missing a real secret is not.
"""

from __future__ import annotations

import re

# Pattern order: anchored format-specific first (AWS, GitHub, Slack),
# then the generic "key=value" sweeps.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws-access-key]"),
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "[REDACTED:github-pat]"),
    (re.compile(r"ghs_[A-Za-z0-9]{36,}"), "[REDACTED:github-app-token]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{82,}"), "[REDACTED:github-fine-grained-pat]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "[REDACTED:openai-style-key]"),
    (re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}"), "[REDACTED:openrouter-key]"),
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ), "[REDACTED:pem-private-key]"),
    # A PEM header whose END marker is ALREADY missing (pre-truncated
    # upstream): mask the rest of the string - a partial private key is
    # still a secret.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*"),
     "[REDACTED:pem-private-key-fragment]"),
    # Generic `KEY=VALUE` env-var leak. Min-length 8 catches
    # `secret=hunter22` but not `key=42`; longer is too strict.
    (re.compile(
        r'((?:password|passwd|secret|api[-_]?key|token|access[-_]?key)\s*[:=]\s*)["\']?[A-Za-z0-9_\-+/=]{8,}["\']?',
        re.IGNORECASE,
    ), r"\1[REDACTED:env-secret]"),
)


def redact_secrets(text: str) -> str:
    """Apply best-effort secret-pattern redaction. Callers that also
    truncate MUST call this first (see module docstring)."""
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text
