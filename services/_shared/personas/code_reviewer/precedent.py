"""Precedent-cited findings + measured confidence (#555).

The differentiator no external reviewer has: Grug remembers his own PR history.
At review time, a draft finding is matched against the repo's review-ledger
rows (persisted by pg_install_store, ingested from review-ledger.jsonl). When a
prior ACCEPTED finding of the same class touched the same file region, the new
finding carries a citation -- "Grug saw this before in PR #N (fixed)" -- and a
confidence chip derived from the MEASURED precision of that class in the ledger,
never a model's self-reported vibe.

Pure + deterministic: LedgerRows in, `PrecedentMatch` out. The dispatch layer
fetches the rows (`list_ledger_rows`) and renders the citation/chip; this module
does only the matching + confidence math so it is trivially testable and cannot
fabricate a citation (a citation exists only when a real matching row does).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ledger import LedgerRow

# A file-token is a path component or dotted-name atom - enough to tell
# "both touch the auth module" from "unrelated files" without a full path
# match (a fix usually moves lines, sometimes renames within a dir).
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
# Generic path atoms that would over-match everything if they counted toward
# overlap (every python finding shares "py", "services", "test", ...).
_STOPWORDS = frozenset({
    "py", "js", "ts", "go", "rs", "java", "yaml", "yml", "json", "md",
    "services", "src", "test", "tests", "lib", "app", "main", "index",
    "a", "b", "the",
})


def _file_tokens(path: str) -> frozenset[str]:
    """Meaningful atoms of a file path (lowercased, stopwords + pure-numeric
    dropped) - the overlap key for 'same region' without exact-path fragility."""
    toks = {t.lower() for t in _TOKEN_RE.findall(path or "")}
    return frozenset(
        t for t in toks if t not in _STOPWORDS and not t.isdigit() and len(t) > 1
    )


@dataclass(frozen=True, slots=True)
class ClassPrecision:
    """Measured accept-rate for one finding class in the ledger corpus."""

    finding_class: str
    accepted: int
    rejected: int

    @property
    def labeled(self) -> int:
        return self.accepted + self.rejected

    @property
    def precision(self) -> float:
        # No labeled history -> 0.5 (genuinely unknown), NOT 1.0: an unproven
        # class must not present as high-confidence. Callers gate on `labeled`.
        return self.accepted / self.labeled if self.labeled else 0.5


def class_precision(rows: list[LedgerRow]) -> dict[str, ClassPrecision]:
    """Per-class accepted/rejected tally from the ledger (the confidence base).

    Only labeled rows (accepted or false_positive) count; unlabeled/pending
    rows are ignored so a class's precision reflects decided outcomes."""
    tally: dict[str, list[int]] = {}
    for r in rows:
        if r.accepted:
            tally.setdefault(r.finding_class, [0, 0])[0] += 1
        elif r.false_positive:
            tally.setdefault(r.finding_class, [0, 0])[1] += 1
    return {
        cls: ClassPrecision(finding_class=cls, accepted=a, rejected=fp)
        for cls, (a, fp) in tally.items()
    }


@dataclass(frozen=True, slots=True)
class PrecedentMatch:
    """A draft finding's precedent + measured confidence, or the no-history
    default. `citations` are (pr, verdict) of prior ACCEPTED same-class rows on
    an overlapping file region, most recent first."""

    finding_class: str
    citations: tuple[tuple[int, str], ...]
    class_precision: float
    labeled_history: int

    @property
    def has_precedent(self) -> bool:
        return bool(self.citations)

    @property
    def confidence_label(self) -> str:
        """Human chip. Only a class with real labeled history earns a
        high/low label; an unproven class is explicitly 'unproven'."""
        if self.labeled_history == 0:
            return "unproven"
        if self.class_precision >= 0.75:
            return "high"
        if self.class_precision >= 0.4:
            return "medium"
        return "low"


_MIN_TOKEN_OVERLAP = 1  # one meaningful shared atom == same region, heuristically


def match_precedent(
    *,
    finding_class: str,
    finding_path: str,
    ledger_rows: list[LedgerRow],
    precisions: dict[str, ClassPrecision] | None = None,
    max_citations: int = 3,
) -> PrecedentMatch:
    """Match one draft finding against the ledger. Pure.

    A citation is a prior ACCEPTED row of the SAME class whose evidence/finding
    text shares >= _MIN_TOKEN_OVERLAP meaningful file atoms with `finding_path`
    (the ledger's `evidence` is free text that usually names the file). Rows are
    deduped by PR (one PR cites once) and ordered by recency (ts desc, then PR
    desc). Confidence rides the measured class precision - passed in for reuse,
    else computed from these rows.
    """
    precisions = precisions if precisions is not None else class_precision(ledger_rows)
    cp = precisions.get(finding_class)
    target = _file_tokens(finding_path)

    by_pr: dict[int, tuple[str, str]] = {}  # pr -> (verdict, ts) most-recent-wins
    if target:
        for r in ledger_rows:
            if r.finding_class != finding_class or not r.accepted:
                continue
            row_tokens = _file_tokens(r.evidence) | _file_tokens(r.finding)
            if len(target & row_tokens) < _MIN_TOKEN_OVERLAP:
                continue
            prev = by_pr.get(r.pr)
            if prev is None or r.ts > prev[1]:
                by_pr[r.pr] = (r.verdict, r.ts)

    ordered = sorted(by_pr.items(), key=lambda kv: (kv[1][1], kv[0]), reverse=True)
    citations = tuple((pr, verdict) for pr, (verdict, _) in ordered[:max_citations])

    return PrecedentMatch(
        finding_class=finding_class,
        citations=citations,
        class_precision=cp.precision if cp else 0.5,
        labeled_history=cp.labeled if cp else 0,
    )


def render_precedent_note(match: PrecedentMatch) -> str:
    """One-line Grug-voice annotation for a finding, or '' when there's nothing
    measured to say (no precedent AND no labeled history - stay silent rather
    than emit a hollow 'unproven' chip on every finding)."""
    if not match.has_precedent and match.labeled_history == 0:
        return ""
    parts: list[str] = []
    if match.has_precedent:
        prs = ", ".join(f"#{pr}" for pr, _ in match.citations)
        n = len(match.citations)
        parts.append(
            f"Grug see this before -- {n} time(s) fixed here ({prs})."
        )
    if match.labeled_history:
        parts.append(
            f"Grug tribe judge this kind `{match.confidence_label}` "
            f"confidence ({match.class_precision:.0%} of {match.labeled_history} "
            f"past calls stuck)."
        )
    return " ".join(parts)
