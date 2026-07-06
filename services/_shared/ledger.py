"""Review-findings ledger corpus (#361 slice 1).

The operator's pipeline emits one row per review finding into
`logs/review-ledger.jsonl`: {ts, repo, pr, reviewer, severity, class,
finding, verdict, commit, evidence}. `verdict` is the ground-truth label
- `fixed` / `declined` (a real finding acted on or consciously kept) vs
`false-positive` (the reviewer was wrong).

This module is the PURE corpus layer: parse rows, and derive the two
aggregations the rest of the learning loop consumes -

- `accepted_findings_by_class` -> the few-shot exemplars (#361 slice 3):
  the team's OWN highest-signal accepted findings, grouped by class.
- `reviewer_precision` -> the independence/routing signal (#361 slice 5,
  #527): accepted / total per reviewer, so a noisy reviewer is weighted
  down and a zero-FP specialist is trusted.

No I/O: the store adapter (`pg_install_store.put_ledger_row` /
`list_ledger_rows`) persists + fetches; the ingest CLI reads the JSONL.
Keeping the aggregation pure gives it a unit-test seam and lets #527 /
Elder reuse it without a database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Verdicts that mean "the finding was REAL" (the reviewer earned the row),
# vs the one verdict that means the reviewer was wrong.
_ACCEPTED = frozenset({"fixed", "declined"})
_REJECTED = frozenset({"false-positive"})

# CRITICAL first; unknown labels sort last (consumers use .get(sev, 4)).
# The ONE severity-ranking convention - few_shot (and any future consumer)
# imports this instead of keeping a drift-prone copy.
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "MED": 2, "LOW": 3}


@dataclass(frozen=True)
class LedgerRow:
    repo: str
    pr: int
    reviewer: str
    severity: str
    finding_class: str
    finding: str
    verdict: str
    evidence: str = ""
    ts: str = ""
    commit: str | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict in _ACCEPTED

    @property
    def false_positive(self) -> bool:
        return self.verdict in _REJECTED


def parse_row(d: dict) -> LedgerRow | None:
    """One dict -> LedgerRow, or None if it lacks the load-bearing fields
    (a malformed/partial line must skip, not abort the ingest)."""
    try:
        return LedgerRow(
            repo=str(d["repo"]),
            pr=int(d["pr"]),
            reviewer=str(d["reviewer"]),
            severity=str(d.get("severity", "")).upper(),
            finding_class=str(d["class"]),
            finding=str(d["finding"]),
            # Some historical rows carry the reason inline -
            # "declined(bounded: ...)" - the label is the leading token;
            # without the split those rows silently match NO verdict class
            # (surfaced by the #537 eval's unknown-verdict counter).
            verdict=str(d["verdict"]).lower().split("(", 1)[0].strip(),
            evidence=str(d.get("evidence", "")),
            ts=str(d.get("ts", "")),
            commit=d.get("commit"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def parse_jsonl(text: str) -> list[LedgerRow]:
    """Parse a review-ledger.jsonl blob; skip blank + malformed lines."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = parse_row(d)
        if row is not None:
            rows.append(row)
    return rows


def accepted_findings_by_class(
    rows: list[LedgerRow], top_n: int = 3,
) -> dict[str, list[LedgerRow]]:
    """Top-N ACCEPTED findings per class, most-severe first - the few-shot
    exemplars. False positives are excluded (we don't want Elder to learn
    a reviewer's mistakes). Severity order CRITICAL>HIGH>MEDIUM>LOW>other;
    within a severity, insertion order (recency of the corpus) breaks ties."""
    order = SEVERITY_ORDER
    by_class: dict[str, list[LedgerRow]] = {}
    for r in rows:
        if r.accepted:
            by_class.setdefault(r.finding_class, []).append(r)
    out: dict[str, list[LedgerRow]] = {}
    for cls, items in by_class.items():
        ranked = sorted(items, key=lambda r: order.get(r.severity, 4))
        out[cls] = ranked[:top_n]
    return out


@dataclass(frozen=True)
class ReviewerScore:
    reviewer: str
    accepted: int
    false_positives: int

    @property
    def total(self) -> int:
        return self.accepted + self.false_positives

    @property
    def precision(self) -> float:
        """Accepted / labeled. 1.0 when a reviewer has no labeled findings
        yet (no evidence to distrust it) - callers weight by `total`."""
        return self.accepted / self.total if self.total else 1.0


def reviewer_precision(rows: list[LedgerRow]) -> dict[str, ReviewerScore]:
    """Per-reviewer accepted/FP tally + precision - the routing signal. A
    reviewer with a false-positive storm scores low; a zero-FP specialist
    scores 1.0 and is trusted."""
    acc: dict[str, list[int]] = {}
    for r in rows:
        cell = acc.setdefault(r.reviewer, [0, 0])
        if r.accepted:
            cell[0] += 1
        elif r.false_positive:
            cell[1] += 1
    return {
        rev: ReviewerScore(reviewer=rev, accepted=a, false_positives=fp)
        for rev, (a, fp) in acc.items()
    }
