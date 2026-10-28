"""Review-findings ledger corpus (#361 slice 1).

The operator's pipeline emits one row per review finding into
`logs/review-ledger.jsonl`: {ts, repo, pr, reviewer, severity, class,
finding, verdict, commit, evidence, head_sha}. `verdict` is the
ground-truth label - `fixed` / `declined` (a real finding acted on or
consciously kept) vs `false-positive` (the reviewer was wrong).

`head_sha` (#545, optional): the full PR-head SHA the finding was actually
recorded against - the pre-fix snapshot, still carrying the bug. The
`_record_reaction_learning` writer (`personas.code_reviewer.reactions`)
populates it from the same `CommentRecord.head_sha` it already stamps onto
`commit` (that field predates this one and stays for display continuity;
`head_sha` is the one the eval replay trusts). Historical rows written
before #545 lack it - `elder_eval.corpus` falls back to treating `commit`
as a FIX commit sha and replaying its parent (see `corpus.EvalCase.anchored`).

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
    # OPTIONAL. Not every finding comes from a pull request - multi-reviewer
    # consensus rows written outside a PR context carry `pr: null`. Before
    # #764 those rows raised TypeError in `int(d["pr"])` and `parse_row`
    # swallowed it as "malformed", silently deleting them from the learning
    # corpus; the 6 newest rows in the committed ledger were all invisible.
    # A row without a PR still carries a real verdict, so it MUST count
    # toward class precision - it just cannot be cited by number.
    pr: int | None
    reviewer: str
    severity: str
    finding_class: str
    finding: str
    verdict: str
    evidence: str = ""
    ts: str = ""
    commit: str | None = None
    # #545: the reviewed PR-head SHA, when the writer captured it directly
    # (see module docstring). Optional - most historical rows predate it.
    head_sha: str | None = None

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
            # `pr` is optional (see LedgerRow.pr). Absent or null -> None,
            # NOT a parse failure. Anything else still coerces, so a
            # genuinely malformed value ("abc") still skips the row.
            pr=int(d["pr"]) if d.get("pr") is not None else None,
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
            head_sha=d.get("head_sha"),
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


def learning_metrics(rows: list[LedgerRow]) -> dict[str, object]:
    """Calculate corpus metrics: total, accepted, false-positive, precision,
    per-reviewer scores, and per-class precision breakdown (#539)."""
    scores = reviewer_precision(rows)
    accepted = sum(s.accepted for s in scores.values())
    fps = sum(s.false_positives for s in scores.values())
    total = accepted + fps
    overall_precision = accepted / total if total else 1.0

    class_counts: dict[str, list[int]] = {}
    for r in rows:
        cell = class_counts.setdefault(r.finding_class, [0, 0])
        if r.accepted:
            cell[0] += 1
        elif r.false_positive:
            cell[1] += 1
    class_precision = {
        cls: (a / (a + fp) if (a + fp) > 0 else 1.0)
        for cls, (a, fp) in class_counts.items()
    }

    return {
        "total_rows": len(rows),
        "labeled_rows": total,
        "accepted": accepted,
        "false_positives": fps,
        "precision": overall_precision,
        "reviewers": {
            rev: {
                "accepted": s.accepted,
                "false_positives": s.false_positives,
                "total": s.total,
                "precision": s.precision,
            }
            for rev, s in scores.items()
        },
        "classes": {
            cls: {
                "accepted": a,
                "false_positives": fp,
                "total": a + fp,
                "precision": class_precision[cls],
            }
            for cls, (a, fp) in class_counts.items()
        },
    }


def format_metrics(rows: list[LedgerRow]) -> str:
    """Human-readable string representation of the ledger metrics (#539)."""
    m = learning_metrics(rows)
    lines = [
        f"ledger: {m['total_rows']} rows, {m['accepted']} accepted, {m['false_positives']} false positives (precision {m['precision']:.2f})",
        "reviewers:",
    ]
    reviewers = m["reviewers"]  # type: ignore[assignment]
    for rev, s in sorted(reviewers.items()):
        lines.append(f"  {rev}: {s['accepted']}/{s['total']} accepted (precision {s['precision']:.2f})")
    lines.append("classes:")
    classes = m["classes"]  # type: ignore[assignment]
    for cls, c in sorted(classes.items()):
        lines.append(f"  {cls}: {c['accepted']}/{c['total']} accepted (precision {c['precision']:.2f})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import sys
    args = sys.argv[1:] if argv is None else argv
    path = args[0] if args else "logs/review-ledger.jsonl"
    try:
        with open(path, encoding="utf-8") as f:
            rows = parse_jsonl(f.read())
    except FileNotFoundError:
        print(f"ledger: file not found: {path}")
        return 1
    print(format_metrics(rows))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
