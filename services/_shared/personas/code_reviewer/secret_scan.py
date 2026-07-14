"""Committed-secret detection for Elder (#436, ADR-0007 Track 1 slice 2).

Flags secrets a PR INTRODUCES on added lines of ANY file type, producing the
SAME `Candidate` shape the SAST/SCA pipeline uses, so the exploitability judge
(`sast.judge_candidates`) and the publish path are reused unchanged - secret
scanning is just a new candidate SOURCE.

Why a dedicated source: the vendored SAST ruleset is `languages: [python]`, so
a secret committed to a `.env`, YAML, shell, JS, or Dockerfile is invisible to
it. This detector is file-type-agnostic and diff-scoped (added lines only, like
the SAST/SCA sources).

Two recall rules:
  (A) high-signal provider token patterns (AWS access-key id, GitHub / Slack /
      Google / Stripe tokens, PEM private-key header) - recognizable formats
      that need no key-name context.
  (B) a generic secret-ish assignment (`api_key = "..."`, `token: ...`) whose
      LITERAL value clears a Shannon-entropy gate, so low-entropy placeholders
      and runtime references such as `credentials.token` never become
      candidates (and never cost a judge call).

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
from .sast import EXPOSED_SECRET, Candidate

__all__ = ["EXPOSED_SECRET", "scan_secrets"]

# AC6 cost bound: cap how many secret candidates we emit per review.
_MAX_SECRETS = 100

# Skip absurdly long added lines (minified JS, lockfile blobs). A real secret on
# a 4 KB+ single line is vanishingly rare.
_MAX_LINE_LEN = 4096

# Aggregate work budget across ALL added lines in a review (mirrors the SAST
# source's per-review byte budget). The per-line cap alone does NOT bound total
# work: a PR with thousands of just-under-cap lines is attacker-influenceable
# content that could otherwise burn minutes of CPU. Once this many bytes of
# added text have been scanned, stop (the diff is already pathological).
_MAX_SCAN_BYTES = 1_048_576

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
# The two `[A-Za-z0-9_.-]` windows around the keyword are BOUNDED (`{0,64}`),
# not unbounded `*`: unbounded greedy classes flanking an alternation backtrack
# super-linearly on a long word-char line with no `[:=]`, and this scanner runs
# over attacker-influenceable PR content. A 64-char key affix covers any real
# identifier while keeping the match linear in line length.
_GENERIC_RE = re.compile(
    r"(?i)(?P<key>[A-Za-z0-9_.-]{0,64}"
    r"(?:secret|token|api[_-]?key|access[_-]?key|private[_-]?key|password|passwd|"
    r"client[_-]?secret|auth[_-]?token|apikey)"
    r"[A-Za-z0-9_.-]{0,64})"
    r"[ \t]*[:=][ \t]*"
    r"""(?:(?P<quote>['"])(?P<quoted>[^'"]+)(?P=quote)|(?P<bare>[^'"\s]+))"""
)

_RUNTIME_MEMBER_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\??\.[A-Za-z_][A-Za-z0-9_]*)+"
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _generic_literal_value(match: re.Match[str]) -> str | None:
    """Return only a committed literal from a generic assignment.

    Secret-shaped variable names are common around legitimate credential
    plumbing. Runtime references and expressions are not committed secret
    material and must never become candidates merely because their source text
    has high entropy (for example ``credentials?.token`` or ``fetch_token()``).
    Provider-format detection remains independent and runs first.
    """
    quoted = match.group("quoted")
    value = quoted if quoted is not None else match.group("bare").rstrip(",;")
    if not value or "://" in value:
        return None
    if any(marker in value for marker in ("$", "?", "(", ")", "{", "}", "[", "]")):
        return None
    if quoted is None:
        if _RUNTIME_MEMBER_RE.fullmatch(value):
            return None
        # A bare alphabetic identifier is a reference, not a literal. Generic
        # unquoted tokens remain detectable when they carry numeric/symbolic
        # token material; quoted all-letter secrets remain detectable too.
        if _IDENTIFIER_RE.fullmatch(value) and not any(ch.isdigit() for ch in value):
            return None
    return value


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


def _detect(text: str) -> tuple[str, str, str] | None:
    """Return `(kind, raw, masked)` if an added line looks like it carries a
    secret, else None. `raw` is the exact matched credential, used ONLY for
    in-memory dedup (never published/logged); `masked` is the safe snippet form.
    Provider patterns win over the generic rule."""
    for pat in _PROVIDER_PATTERNS:
        m = pat.regex.search(text)
        if m:
            return pat.kind, m.group(0), _mask(m.group(0))
    m = _GENERIC_RE.search(text)
    if m:
        value = _generic_literal_value(m)
        if value is None:
            return None
        if len(value) >= _MIN_GENERIC_LEN and _shannon_entropy(value) >= _MIN_ENTROPY:
            return f"secret-like assignment to `{m.group('key')}`", value, _mask(value)
    return None


def scan_secrets(hunks: tuple[DiffHunk, ...]) -> tuple[Candidate, ...]:
    """Secret-scanning candidate source: a Candidate per committed credential a
    PR introduces, across ANY file type. Diff-scoped, content-deduped by the
    EXACT matched credential per `(file, kind)` (the same credential repeated in
    a file is reported once - bounds judge cost, mirrors SCA; deduping on the
    exact value, not the lossy mask, so two distinct secrets sharing a masked
    prefix/suffix are both kept), capped at `_MAX_SECRETS`. Same `Candidate`
    shape the SAST pipeline judges + publishes, so the judge decides whether the
    value is a real secret (vs a docs example / placeholder). The raw value is
    used only for in-memory dedup; the published snippet is masked - the raw
    value is never echoed into a Candidate or a log."""
    secrets: list[_Secret] = []
    seen: set[tuple[str, str, str]] = set()
    scanned = 0
    for hunk in hunks:
        for lineno, text in _added_lines(hunk):
            if len(text) > _MAX_LINE_LEN:
                continue
            scanned += len(text)
            if scanned > _MAX_SCAN_BYTES:
                return _to_candidates(secrets)
            hit = _detect(text)
            if hit is None:
                continue
            kind, raw, masked = hit
            key = (hunk.file_path, kind, raw)
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
