"""Deterministic docs/code claim consistency for Elder.

Catches the review class Qodo and CodeRabbit found when policy knobs
shipped with comments/docs that asserted the wrong number or the wrong
comparison (settle medium cap, deep-diff exclusive vs inclusive).

Those misses are not an LLM IQ problem - they are a missing detector.
The LLM is free to still flag general prose drift via the
``doc-code-claim-drift`` rule; this module is the high-precision,
no-hallucination floor for the known policy shapes.

Pure: (hunks, file_contents) in, Findings out. No IO. Diff-anchored on
the ADDED claim line so findings pass the anti-hallucination filter.
Fail-open: any parse glitch yields no finding rather than aborting review.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import Finding

_RULE = "doc-code-claim-drift"

# Steady Hunt medium quiet window: `return min(base, N)`.
_SETTLE_CAP_CODE = re.compile(
    r"return\s+min\(\s*base(?:_seconds)?\s*,\s*(\d+)\s*\)",
)

# Deep escalation bound: exclusive `added > threshold` vs inclusive `>=`.
_DEEP_EXCLUSIVE_CODE = re.compile(r"\badded\s*>\s*threshold\b")
_DEEP_INCLUSIVE_CODE = re.compile(r"\badded\s*>=\s*threshold\b")

# "caps medium (Steady) at 5s", "cap medium at 3 seconds", etc.
_SETTLE_CLAIM_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\bcaps?\b[^.\n]{0,50}?\bmedium\b[^.\n]{0,40}?\b(\d+)\s*"
        r"s(?:ec(?:ond)?s?)?\b",
    ),
    re.compile(
        r"(?i)\bmedium\b[^.\n]{0,30}?\((?:[^)]*steady[^)]*)\)[^.\n]{0,20}?"
        r"\bat\s+(\d+)\s*s(?:ec(?:ond)?s?)?\b",
    ),
    re.compile(
        r"(?i)\bsteady\s+hunt\b[^.\n]{0,60}?\b(\d+)\s*"
        r"s(?:ec(?:ond)?s?)?\b",
    ),
    re.compile(
        r"(?i)\bmin\(\s*base(?:_seconds)?\s*,\s*(\d+)\s*\)",
    ),
    re.compile(
        r"(?i)\bmedium\b[^.\n]{0,40}?\bcaps?\b[^.\n]{0,30}?\bat\s+"
        r"(\d+)\s*s(?:ec(?:ond)?s?)?\b",
    ),
)

_DEEP_TOPIC = re.compile(
    r"(?i)(?:deep[_\s-]?diff|deep[_\s-]?escalat|GRUG_DEEP_DIFF|"
    r"added\s+lines?|auto-?deep|reasoner\s+only)",
)
_DEEP_CLAIM_EXCLUSIVE = re.compile(
    r"(?i)(?:\bexclusive\b|"
    r"only\s+when\s+added\s+lines?\s*>|"
    r"added\s+lines?\s*>\s*(?:\d+|N|threshold)|"
    r"more\s+than\s+(?:\d+|N|threshold)|"
    r"only\s+above\s+(?:\d+|N|threshold))",
)
_DEEP_CLAIM_INCLUSIVE = re.compile(
    r"(?i)(?:\binclusive\b|"
    r"added\s+lines?\s*>=\s*(?:\d+|N|threshold)|"
    r"at\s+least\s+\d+\s+added|"
    r">=\s*(?:GRUG_DEEP_DIFF_LINES|\d+|threshold))",
)

Bound = Literal["exclusive", "inclusive"]
ClaimKind = Literal["settle_medium_cap", "deep_bound"]


@dataclass(frozen=True, slots=True)
class _PolicyFacts:
    settle_medium_cap: int | None
    deep_bound: Bound | None


@dataclass(frozen=True, slots=True)
class _Claim:
    kind: ClaimKind
    value: int | Bound
    file: str
    line: int
    snippet: str


def _added_lines(hunk: DiffHunk) -> list[tuple[int, str]]:
    """New-side (lineno, text) for each ADDED line in a hunk."""
    out: list[tuple[int, str]] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith(("@@", "+++", "---")):
            continue
        if raw.startswith("+"):
            out.append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue
        else:
            lineno += 1
    return out


def _is_comment_or_doc_line(path: str, text: str) -> bool:
    """True for comment / doc / markdown lines we treat as claims."""
    if path.lower().endswith((".md", ".rst", ".txt", ".adoc")):
        return True
    stripped = text.lstrip()
    if not stripped:
        return False
    if stripped.startswith(("#", "//", "/*", "*", "<!--", "--")):
        return True
    return " #" in text or "\t#" in text


def _claim_text(text: str) -> str:
    """Prefer the comment tail when a line mixes code and a trailing # note."""
    if " #" in text:
        return text.split(" #", 1)[1]
    if "\t#" in text:
        return text.split("\t#", 1)[1]
    return text


# Only these paths may supply implementation facts. Scanning every changed
# .py (tests, fixtures, unrelated helpers) lets a fixture like
# `added > threshold` poison the policy fact and hide real drift.
_SETTLE_FACT_PATH_SUFFIXES: tuple[str, ...] = (
    "personas/code_reviewer/snapshot.py",
    "/snapshot.py",
)
_DEEP_FACT_PATH_SUFFIXES: tuple[str, ...] = (
    "services/_shared/llm_client.py",
    "/llm_client.py",
)


def _path_matches(path: str, suffixes: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized.endswith(s) for s in suffixes)


def _unique_or_none(values: set[int] | set[Bound]) -> int | Bound | None:
    """Single agreed value, or None when missing/conflicting (no last-wins)."""
    if len(values) == 1:
        return next(iter(values))
    return None


def _settle_caps_in(source: str) -> set[int]:
    return {int(m.group(1)) for m in _SETTLE_CAP_CODE.finditer(source)}


def _deep_bounds_in(source: str) -> set[Bound]:
    found: set[Bound] = set()
    if _DEEP_EXCLUSIVE_CODE.search(source):
        found.add("exclusive")
    if _DEEP_INCLUSIVE_CODE.search(source):
        found.add("inclusive")
    return found


def _extract_facts(file_contents: dict[str, str]) -> _PolicyFacts:
    """Pull settle cap + deep bound only from known policy helper sources."""
    settle_vals: set[int] = set()
    deep_vals: set[Bound] = set()
    for path, source in file_contents.items():
        if not path.endswith(".py"):
            continue
        if _path_matches(path, _SETTLE_FACT_PATH_SUFFIXES):
            settle_vals |= _settle_caps_in(source)
        if _path_matches(path, _DEEP_FACT_PATH_SUFFIXES):
            deep_vals |= _deep_bounds_in(source)
    return _PolicyFacts(
        settle_medium_cap=_unique_or_none(settle_vals),  # type: ignore[arg-type]
        deep_bound=_unique_or_none(deep_vals),  # type: ignore[arg-type]
    )


def _parse_settle_claim(path: str, lineno: int, text: str, claim_src: str) -> _Claim | None:
    for pat in _SETTLE_CLAIM_RES:
        m = pat.search(claim_src)
        if m:
            return _Claim(
                kind="settle_medium_cap",
                value=int(m.group(1)),
                file=path,
                line=lineno,
                snippet=text.strip()[:200],
            )
    return None


def _parse_deep_claim(path: str, lineno: int, text: str, claim_src: str) -> _Claim | None:
    if not _DEEP_TOPIC.search(claim_src):
        return None
    exclusive = bool(_DEEP_CLAIM_EXCLUSIVE.search(claim_src))
    inclusive = bool(_DEEP_CLAIM_INCLUSIVE.search(claim_src))
    if exclusive == inclusive:
        return None  # both or neither: ambiguous, skip
    return _Claim(
        kind="deep_bound",
        value="exclusive" if exclusive else "inclusive",
        file=path,
        line=lineno,
        snippet=text.strip()[:200],
    )


def _extract_claims(hunks: tuple[DiffHunk, ...]) -> list[_Claim]:
    """Claims asserted on ADDED comment/doc lines in the diff.

    Pure executable lines are never claims - only comments, markdown, and
    trailing ``#`` notes. That keeps the detector from flagging the
    implementation itself when it changes ``min(base, N)``.
    """
    claims: list[_Claim] = []
    for hunk in hunks:
        path = hunk.file_path
        for lineno, text in _added_lines(hunk):
            if not _is_comment_or_doc_line(path, text):
                continue
            claim_src = _claim_text(text)
            settle = _parse_settle_claim(path, lineno, text, claim_src)
            if settle is not None:
                claims.append(settle)
            deep = _parse_deep_claim(path, lineno, text, claim_src)
            if deep is not None:
                claims.append(deep)
    return claims


def _finding_for_claim(claim: _Claim, message: str) -> Finding:
    return Finding(
        file=claim.file,
        line=claim.line,
        severity="medium",
        rule_name=_RULE,
        message=message,
        suggestion=None,
        effort="quick-win",
    )


def _settle_mismatch_message(claimed: int, actual: int) -> str:
    return (
        f"Comment/docs claim Steady/medium settle cap of {claimed}s, but code "
        f"has `min(base, {actual})`. Doc/code claim drift - update the comment "
        f"or the implementation so they match. Grug say: number in mouth must "
        f"equal number in hand."
    )


def _deep_mismatch_message(claimed: Bound, actual: Bound) -> str:
    code_op = ">" if actual == "exclusive" else ">="
    claim_op = ">" if claimed == "exclusive" else ">="
    return (
        f"Comment/docs describe deep-diff escalation as {claimed} "
        f"(`added {claim_op} threshold`), but code uses {actual} "
        f"(`added {code_op} threshold`). Doc/code claim drift - align the "
        f"bound language with `decide_deep_escalation`."
    )


def _finding_vs_facts(
    claim: _Claim,
    facts: _PolicyFacts,
    seen: set[tuple[str, int, str]],
) -> Finding | None:
    """One finding when a claim disagrees with implementation facts."""
    if claim.kind == "settle_medium_cap":
        if facts.settle_medium_cap is None:
            return None
        claimed = int(claim.value)  # type: ignore[arg-type]
        if claimed == facts.settle_medium_cap:
            return None
        key = (claim.file, claim.line, "settle")
        if key in seen:
            return None
        seen.add(key)
        return _finding_for_claim(
            claim, _settle_mismatch_message(claimed, facts.settle_medium_cap),
        )

    if facts.deep_bound is None:
        return None
    claimed_b = claim.value  # exclusive | inclusive
    if claimed_b == facts.deep_bound:
        return None
    key = (claim.file, claim.line, "deep")
    if key in seen:
        return None
    seen.add(key)
    return _finding_for_claim(
        claim,
        _deep_mismatch_message(claimed_b, facts.deep_bound),  # type: ignore[arg-type]
    )


def _intra_pr_settle_conflicts(
    claims: list[_Claim],
    seen: set[tuple[str, int, str]],
) -> list[Finding]:
    """Flag ADDED settle claims that disagree with each other (no code facts)."""
    settle_claims = [c for c in claims if c.kind == "settle_medium_cap"]
    if len(settle_claims) < 2:
        return []
    counts = Counter(int(c.value) for c in settle_claims)  # type: ignore[arg-type]
    if len(counts) < 2:
        return []
    mode_val, _ = counts.most_common(1)[0]
    out: list[Finding] = []
    for claim in settle_claims:
        claimed = int(claim.value)  # type: ignore[arg-type]
        if claimed == mode_val:
            continue
        key = (claim.file, claim.line, "settle-conflict")
        if key in seen:
            continue
        seen.add(key)
        out.append(
            _finding_for_claim(
                claim,
                (
                    f"PR comments disagree on medium settle cap: this line "
                    f"says {claimed}s, other added comments say {mode_val}s. "
                    f"Align every claim with the code (`min(base, N)` in the "
                    f"settle helper)."
                ),
            )
        )
    return out


def scan_claim_checks(
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str],
) -> tuple[Finding, ...]:
    """Flag ADDED comments/docs whose policy claim disagrees with code.

    Requires the implementation fact to be visible in ``file_contents``
    (changed files at head, optionally enriched by the dispatcher). When
    the PR only touches a comment and the source of truth is not in the
    payload, this detector yields nothing - the LLM rule may still fire.
    """
    if not hunks:
        return ()
    facts = _extract_facts(file_contents)
    claims = _extract_claims(hunks)
    if not claims:
        return ()

    findings: list[Finding] = []
    seen: set[tuple[str, int, str]] = set()
    for claim in claims:
        hit = _finding_vs_facts(claim, facts, seen)
        if hit is not None:
            findings.append(hit)

    if facts.settle_medium_cap is None:
        findings.extend(_intra_pr_settle_conflicts(claims, seen))

    return tuple(findings)
