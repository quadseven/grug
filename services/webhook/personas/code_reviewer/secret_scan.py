# MIRRORED — sibling at services/api/personas/code_reviewer/secret_scan.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Committed-secret detection for Elder (#436, ADR-0007 Track 1 slice 2).

Flags secrets a PR INTRODUCES on added lines of ANY file type, producing the
SAME `Candidate` shape the SAST/SCA pipeline uses, so the exploitability judge
(`sast.judge_candidates`) and the publish path are reused unchanged - secret
scanning is just a new candidate SOURCE.

Why a dedicated source: the vendored SAST ruleset is `languages: [python]`, so
a secret committed to a `.env`, YAML, shell, JS, or Dockerfile is invisible to
it. This detector is file-type-agnostic and diff-scoped (added lines only, like
the SAST/SCA sources).

Two recall rules, deliberately LIBERAL (a detector miss is unrecoverable; an
over-flag is recovered by the judge):
  (A) high-signal provider token patterns (AWS access-key id, GitHub / Slack /
      Google / Stripe tokens, PEM private-key header) - recognizable formats
      that need no key-name context.
  (B) a generic secret-ish assignment (`api_key = "..."`, `token: ...`) whose
      value clears a Shannon-entropy gate, so low-entropy placeholders like
      `your-key-here` never become candidates (and never cost a judge call).

No-echo invariant: the snippet MASKS the value (a few leading/trailing chars
only), so the raw credential is never written into the finding message, the
check-run, or the logs. The judge discriminates real-value-vs-placeholder from
the full-file context (#336) it already receives.

Pure: no IO, no network, no logging - deterministic + unit-tested.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .diff_parser import DiffHunk
from .sast import Candidate

EXPOSED_SECRET = "exposed-secret"

# AC6 cost bound: cap how many secret candidates we emit per review.
_MAX_SECRETS = 100

# Generic rule thresholds: a value must be at least this long AND clear this
# Shannon entropy (bits/char) to count as secret-like. Tuned so dictionary
# placeholders ("your-key-here", "passwordpassword") fall below the bar while
# random tokens clear it.
_MIN_GENERIC_LEN = 16
_MIN_ENTROPY = 3.0


@dataclass(frozen=True, slots=True)
class _ProviderPattern:
    """A high-signal secret format: `kind` labels it for the (masked) snippet,
    `regex` matches the credential substring anywhere on the line."""

    kind: str
    regex: re.Pattern[str]


# Each pattern is a recognizable credential FORMAT - matching one is strong
# evidence on its own (the judge still confirms the value is not an example).
_PROVIDER_PATTERNS: tuple[_ProviderPattern, ...] = (
    _ProviderPattern("AWS access key id", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    _ProviderPattern("GitHub token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}")),
    _ProviderPattern("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{22,}")),
    _ProviderPattern("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    _ProviderPattern("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    _ProviderPattern("Stripe key", re.compile(r"(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{24,}")),
    _ProviderPattern("PEM private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
)

# Generic rule: a secret-ish key name assigned (`=` or `:`) a value. The value
# is captured (quoted or bare) and entropy-gated below. Case-insensitive.
_GENERIC_RE = re.compile(
    r"(?i)(?P<key>[A-Za-z0-9_.-]*"
    r"(?:secret|token|api[_-]?key|access[_-]?key|private[_-]?key|password|passwd|"
    r"client[_-]?secret|auth[_-]?token|apikey)"
    r"[A-Za-z0-9_.-]*)"
    r"\s*[:=]\s*"
    r"""['"]?(?P<value>[^'"\s]+)['"]?"""
)


@dataclass(frozen=True, slots=True)
class _Secret:
    """A detected secret on an added line. `(file, line)` anchor the finding to
    the diff; `kind` labels it; `masked` is the safe-to-publish representation."""

    file: str
    line: int
    kind: str
    masked: str


def _added_lines(hunk: DiffHunk) -> list[tuple[int, str]]:
    """[(new_side_line_number, added_text)] for added lines in a hunk. New-side
    number advances on added/context lines, not on removed lines."""
    out: list[tuple[int, str]] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith("@@") or raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue
        else:
            lineno += 1
    return out


def _shannon_entropy(s: str) -> float:
    """Bits/char Shannon entropy of `s` (0.0 for empty/uniform strings)."""
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _mask(value: str) -> str:
    """A safe-to-publish view of a secret value: short values are fully hidden;
    longer ones keep a few leading/trailing chars so the judge + a human can
    recognize the format without the raw credential being echoed."""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def _detect(text: str) -> tuple[str, str] | None:
    """Return `(kind, masked_value)` if an added line looks like it carries a
    secret, else None. Provider patterns win over the generic rule."""
    for pat in _PROVIDER_PATTERNS:
        m = pat.regex.search(text)
        if m:
            return pat.kind, _mask(m.group(0))
    m = _GENERIC_RE.search(text)
    if m:
        value = m.group("value")
        if len(value) >= _MIN_GENERIC_LEN and _shannon_entropy(value) >= _MIN_ENTROPY:
            return f"secret-like assignment to `{m.group('key')}`", _mask(value)
    return None


def scan_secrets(hunks: tuple[DiffHunk, ...]) -> tuple[Candidate, ...]:
    """Secret-scanning candidate source: a Candidate per committed credential a
    PR introduces, across ANY file type. Diff-scoped, content-deduped by
    `(file, kind, masked)` (the same credential repeated in a file is reported
    once - bounds judge cost, mirrors SCA), capped at `_MAX_SECRETS`. Same
    `Candidate` shape the SAST pipeline judges + publishes, so the judge decides
    whether the value is a real secret (vs a docs example / placeholder). The
    snippet is masked - the raw value is never echoed."""
    secrets: list[_Secret] = []
    seen: set[tuple[str, str, str]] = set()
    for hunk in hunks:
        for lineno, text in _added_lines(hunk):
            hit = _detect(text)
            if hit is None:
                continue
            kind, masked = hit
            key = (hunk.file_path, kind, masked)
            if key in seen:
                continue
            seen.add(key)
            secrets.append(_Secret(file=hunk.file_path, line=lineno, kind=kind, masked=masked))
            if len(secrets) >= _MAX_SECRETS:
                return _to_candidates(secrets)
    return _to_candidates(secrets)


def _to_candidates(secrets: list[_Secret]) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(
            vuln_class=EXPOSED_SECRET,
            file=s.file,
            line=s.line,
            snippet=f"{s.kind} (value masked: {s.masked})",
        )
        for s in secrets
    )
