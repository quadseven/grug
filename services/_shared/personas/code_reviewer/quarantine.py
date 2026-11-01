"""Metadata-quarantined security pass (grug#1569).

A flattering PR description can talk a reviewer out of a real finding
(arXiv:2603.18740), and the normal pass reads that description. For a PR
touching a sensitive path this module builds evidence with the author's prose
removed, so a second pass, its judge, and its refute gate read code only. The
merge is one way: the second pass can add a finding or raise a severity, never
remove or lower one.
"""
from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

from personas.code_reviewer.persona import Finding
from personas.code_reviewer.repo_config import (
    _SEVERITY_RANK,
    RepoConfig,
    path_matches,
)
from review_evidence import ElderEvidence, without_author_text

SECURITY_SCOPE = (
    "security defects only: authentication, authorization, cryptography, "
    "secret handling, injection, and CI or reviewer configuration"
)
UNAVAILABLE_NOTE = (
    "The code-only security pass did not finish on this head, so only the "
    "normal review covered the sensitive paths."
)

# Whole words, so `authors.md` and `tokenizer.py` stay ordinary paths.
_SENSITIVE_WORDS = frozenset({
    "auth", "oauth", "authn", "authz", "authenticate", "authentication",
    "authorization", "authorize",
    "crypto", "cryptography", "encryption", "jwt", "kms",
    "payment", "payments", "billing",
    "secret", "secrets", "credential", "credentials", "password", "passwords",
})
_CONFIG_GLOBS = (
    ".github/workflows/**", ".github/actions/**", ".gitlab-ci.yml", ".grug.yaml",
)
# Splits `AuthService` and `oauth2`: words are runs of letters, camelCase too.
_WORD_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+")
_BLOCKING = frozenset({"high", "critical"})
_FALSE_VALUES = frozenset({"", "0", "false", "off", "no"})


def quarantined_pass_enabled() -> bool:
    """`GRUG_QUARANTINED_SECURITY_PASS`, off unless set: the pass spends
    model calls on every PR touching a sensitive path."""
    raw = os.getenv("GRUG_QUARANTINED_SECURITY_PASS", "")
    return raw.strip().lower() not in _FALSE_VALUES


def _is_default_sensitive(path: str) -> bool:
    if any(path_matches(path, glob) for glob in _CONFIG_GLOBS):
        return True
    words = {word.lower() for word in _WORD_RE.findall(path)}
    return not _SENSITIVE_WORDS.isdisjoint(words)


def sensitive_paths(paths: Iterable[str], config: RepoConfig) -> tuple[str, ...]:
    """The changed paths this repo treats as sensitive, in input order. A
    `security_review_paths` list from the base ref replaces the defaults."""
    globs = config.security_review_paths
    if globs is None:
        return tuple(p for p in paths if _is_default_sensitive(p))
    return tuple(p for p in paths if any(path_matches(p, g) for g in globs))


def quarantined_evidence(
    evidence: ElderEvidence, paths: Iterable[str],
) -> ElderEvidence:
    """`evidence` narrowed to `paths`, with diff and code context only. Repo
    docs come from the PR head, so the author can write those too."""
    context = without_author_text(evidence.pr_context) or {}
    context = {**context, "review_scope": SECURITY_SCOPE, "code_only": True}
    return dataclasses.replace(
        evidence.scoped_to(paths),
        pr_context=context,
        repo_docs_context=None,
        runtime_context=None,
        ci_context=None,
    )


@dataclass(frozen=True)
class MergedFindings:
    findings: tuple[Finding, ...]
    added: int
    raised: int


def _fingerprint(finding: Finding) -> tuple[str, int, str]:
    return (finding.file, finding.line, finding.rule_name)


def _in_scope(finding: Finding) -> bool:
    return finding.category == "security" or finding.severity in _BLOCKING


def merge_by_fingerprint(
    primary: tuple[Finding, ...], second: tuple[Finding, ...],
) -> MergedFindings:
    """One marking per fingerprint, at the stricter severity. A second-pass
    finding outside the security scope is a duplicate nit and is dropped."""
    merged = list(primary)
    positions: dict[tuple[str, int, str], int] = {}
    for index, finding in enumerate(primary):
        positions.setdefault(_fingerprint(finding), index)
    added = raised = 0
    for finding in second:
        key = _fingerprint(finding)
        position = positions.get(key)
        if position is None:
            if _in_scope(finding):
                positions[key] = len(merged)
                merged.append(finding)
                added += 1
        elif _SEVERITY_RANK[finding.severity] > _SEVERITY_RANK[merged[position].severity]:
            merged[position] = dataclasses.replace(
                merged[position], severity=finding.severity,
            )
            raised += 1
    return MergedFindings(tuple(merged), added, raised)
