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
from dataclasses import dataclass
from typing import Literal

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import Finding

_RULE = "doc-code-claim-drift"

# --- implementation facts (from Python at head) -----------------------------

# Steady Hunt medium quiet window: `return min(base, N)`.
_SETTLE_CAP_CODE = re.compile(
    r"return\s+min\(\s*base(?:_seconds)?\s*,\s*(\d+)\s*\)",
)

# Deep escalation bound: exclusive `added > threshold` vs inclusive `>=`.
_DEEP_EXCLUSIVE_CODE = re.compile(r"\badded\s*>\s*threshold\b")
_DEEP_INCLUSIVE_CODE = re.compile(r"\badded\s*>=\s*threshold\b")

# --- claim patterns on ADDED comment / doc / yaml lines ---------------------

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

# Deep-bound claims in prose near escalation / diff-line language.
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


@dataclass(frozen=True, slots=True)
class _PolicyFacts:
    settle_medium_cap: int | None
    deep_bound: Bound | None


@dataclass(frozen=True, slots=True)
class _Claim:
    kind: Literal["settle_medium_cap", "deep_bound"]
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
    lower = path.lower()
    if lower.endswith((".md", ".rst", ".txt", ".adoc")):
        return True
    stripped = text.lstrip()
    if not stripped:
        return False
    if stripped.startswith(("#", "//", "/*", "*", "<!--", "--")):
        return True
    # YAML / k8s inline comments: "  # ... " already covered by #.
    # Trailing comment on a value line: treat the comment tail as claim text.
    if " #" in text or "\t#" in text:
        return True
    return False


def _claim_text(text: str) -> str:
    """Prefer the comment tail when a line mixes code and a trailing # note."""
    if " #" in text:
        return text.split(" #", 1)[1]
    if "\t#" in text:
        return text.split("\t#", 1)[1]
    return text


def _extract_facts(file_contents: dict[str, str]) -> _PolicyFacts:
    """Pull settle cap + deep bound from Python sources at head."""
    settle: int | None = None
    deep: Bound | None = None
    for path, source in file_contents.items():
        if not path.endswith(".py"):
            continue
        for m in _SETTLE_CAP_CODE.finditer(source):
            # Prefer settle-related modules when multiple min(base, N) exist.
            n = int(m.group(1))
            if "settle" in path.lower() or "snapshot" in path.lower():
                settle = n
            elif settle is None:
                settle = n
        if _DEEP_EXCLUSIVE_CODE.search(source):
            deep = "exclusive"
        elif _DEEP_INCLUSIVE_CODE.search(source) and deep is None:
            deep = "inclusive"
    return _PolicyFacts(settle_medium_cap=settle, deep_bound=deep)


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
            for pat in _SETTLE_CLAIM_RES:
                m = pat.search(claim_src)
                if m:
                    claims.append(
                        _Claim(
                            kind="settle_medium_cap",
                            value=int(m.group(1)),
                            file=path,
                            line=lineno,
                            snippet=text.strip()[:200],
                        )
                    )
                    break
            if _DEEP_TOPIC.search(claim_src):
                if _DEEP_CLAIM_EXCLUSIVE.search(claim_src) and not _DEEP_CLAIM_INCLUSIVE.search(
                    claim_src
                ):
                    claims.append(
                        _Claim(
                            kind="deep_bound",
                            value="exclusive",
                            file=path,
                            line=lineno,
                            snippet=text.strip()[:200],
                        )
                    )
                elif _DEEP_CLAIM_INCLUSIVE.search(claim_src) and not _DEEP_CLAIM_EXCLUSIVE.search(
                    claim_src
                ):
                    claims.append(
                        _Claim(
                            kind="deep_bound",
                            value="inclusive",
                            file=path,
                            line=lineno,
                            snippet=text.strip()[:200],
                        )
                    )
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
        if claim.kind == "settle_medium_cap":
            if facts.settle_medium_cap is None:
                continue
            claimed = int(claim.value)  # type: ignore[arg-type]
            if claimed == facts.settle_medium_cap:
                continue
            key = (claim.file, claim.line, "settle")
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                _finding_for_claim(
                    claim,
                    (
                        f"Comment/docs claim Steady/medium settle cap of "
                        f"{claimed}s, but code has `min(base, "
                        f"{facts.settle_medium_cap})`. Doc/code claim drift "
                        f"- update the comment or the implementation so they "
                        f"match. Grug say: number in mouth must equal number "
                        f"in hand."
                    ),
                )
            )
        elif claim.kind == "deep_bound":
            if facts.deep_bound is None:
                continue
            claimed_b = claim.value  # exclusive | inclusive
            if claimed_b == facts.deep_bound:
                continue
            key = (claim.file, claim.line, "deep")
            if key in seen:
                continue
            seen.add(key)
            code_op = ">" if facts.deep_bound == "exclusive" else ">="
            claim_op = ">" if claimed_b == "exclusive" else ">="
            findings.append(
                _finding_for_claim(
                    claim,
                    (
                        f"Comment/docs describe deep-diff escalation as "
                        f"{claimed_b} (`added {claim_op} threshold`), but "
                        f"code uses {facts.deep_bound} (`added {code_op} "
                        f"threshold`). Doc/code claim drift - align the "
                        f"bound language with `decide_deep_escalation`."
                    ),
                )
            )

    # Intra-PR claim-vs-claim: two ADDED comments disagree on the same
    # policy even when code is not in file_contents.
    settle_vals = {
        int(c.value)  # type: ignore[arg-type]
        for c in claims
        if c.kind == "settle_medium_cap"
    }
    if len(settle_vals) > 1 and facts.settle_medium_cap is None:
        # Flag every claim that is not the mode (smallest wins as "likely code")
        # - actually without code, flag all but the first-seen majority is hard.
        # Flag all claims that differ from the minimum (conservative: medium cap
        # is usually the smaller number vs base settle). Prefer flagging outliers
        # vs the most common value.
        from collections import Counter

        counts = Counter(
            int(c.value)  # type: ignore[arg-type]
            for c in claims
            if c.kind == "settle_medium_cap"
        )
        mode_val, _ = counts.most_common(1)[0]
        for claim in claims:
            if claim.kind != "settle_medium_cap":
                continue
            claimed = int(claim.value)  # type: ignore[arg-type]
            if claimed == mode_val:
                continue
            key = (claim.file, claim.line, "settle-conflict")
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                _finding_for_claim(
                    claim,
                    (
                        f"PR comments disagree on medium settle cap: this "
                        f"line says {claimed}s, other added comments say "
                        f"{mode_val}s. Align every claim with the code "
                        f"(`min(base, N)` in the settle helper)."
                    ),
                )
            )

    return tuple(findings)
