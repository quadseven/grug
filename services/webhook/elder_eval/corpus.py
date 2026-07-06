"""Eval corpus for the Elder replay harness (#361 slice 2, #537).

Pure: groups slice-1 `LedgerRow`s into one `EvalCase` per (repo, pr) and
bridges the LEDGER class vocabulary (test-gap, security-scope, ...) onto
Elder's closed `_BUG_CLASSES` taxonomy. Ledger classes Elder cannot
express (doc-truth, iac-hygiene, ...) are surfaced as `out_of_taxonomy`
and EXCLUDED from scoring denominators - a class the reviewer has no way
to name must never score as a miss.

Rows arrive via the slice-1 corpus layer only: `rows_from_store` (the
ingested `pg_install_store` rows) or `ledger.parse_jsonl` on the
committed JSONL - never a third parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Deliberate private import: the eval must bridge onto Elder's REAL closed
# taxonomy, not a copy that drifts (same rationale as the SAST runner
# importing _build_messages - measuring Elder means using Elder's own parts).
from code_review_prompt import _BUG_CLASSES
from ledger import LedgerRow, parse_row

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_class(label: str) -> str:
    """Kebab-normalize a class label so Elder's display labels ("silent
    failure") and the ledger's kebab labels ("silent-failure") compare."""
    return _NORM_RE.sub("-", label.lower()).strip("-")


ELDER_CLASSES: frozenset[str] = frozenset(normalize_class(c) for c in _BUG_CLASSES)

# Ledger vocabulary -> Elder vocabulary, where the words differ but the
# concept is the same. Keys and values are normalized. A ledger class not
# in this map and not itself an Elder class is OUT OF TAXONOMY.
_CLASS_ALIASES: dict[str, frozenset[str]] = {
    "test-gap": frozenset({"test-coverage", "test-fidelity"}),
    "security-scope": frozenset({"security"}),
    "simplification": frozenset({"maintainability"}),
    "upstream-semantics": frozenset({"correctness", "robustness"}),
}


def expected_elder_classes(ledger_class: str) -> frozenset[str]:
    """The Elder classes that would count as CATCHING a ledger finding of
    `ledger_class`. Empty set = out of taxonomy (excluded, not a miss)."""
    norm = normalize_class(ledger_class)
    if norm in _CLASS_ALIASES:
        return _CLASS_ALIASES[norm]
    if norm in ELDER_CLASSES:
        return frozenset({norm})
    return frozenset()


@dataclass(frozen=True)
class EvalCase:
    """One ledger PR as a replay unit.

    `expected_classes`: ledger class (normalized) -> the Elder classes that
    count as a catch. Only accepted (fixed/declined) in-taxonomy rows.
    `fp_only_classes`: ELDER-normalized classes known on this PR ONLY as
    false positives - a replay emission there is measured noise.
    `out_of_taxonomy`: accepted ledger classes Elder cannot express, with
    row counts - reported, never scored.
    """

    repo: str
    pr: int
    expected_classes: dict[str, frozenset[str]]
    fp_only_classes: frozenset[str]
    out_of_taxonomy: dict[str, int]

    @property
    def case_id(self) -> str:
        return f"{self.repo}#{self.pr}"

    @property
    def scorable(self) -> bool:
        """Worth an LLM call: contributes to catch or noise denominators."""
        return bool(self.expected_classes or self.fp_only_classes)


def build_cases(rows: Iterable[LedgerRow]) -> tuple[EvalCase, ...]:
    """Group ledger rows into per-(repo, pr) EvalCases, (repo, pr)-sorted."""
    grouped: dict[tuple[str, int], list[LedgerRow]] = {}
    for r in rows:
        grouped.setdefault((r.repo, r.pr), []).append(r)

    cases: list[EvalCase] = []
    for repo, pr in sorted(grouped):
        expected: dict[str, frozenset[str]] = {}
        out_of_taxonomy: dict[str, int] = {}
        fp_elder: set[str] = set()
        accepted_elder: set[str] = set()
        for r in grouped[(repo, pr)]:
            norm = normalize_class(r.finding_class)
            elder = expected_elder_classes(norm)
            if r.accepted:
                if not elder:
                    out_of_taxonomy[norm] = out_of_taxonomy.get(norm, 0) + 1
                    continue
                expected[norm] = elder
                accepted_elder |= elder
            elif r.false_positive:
                fp_elder |= elder
        cases.append(
            EvalCase(
                repo=repo,
                pr=pr,
                expected_classes=expected,
                # A class both accepted AND FP'd on the same PR is not
                # fp-only - emitting it there is a legitimate catch.
                fp_only_classes=frozenset(fp_elder - accepted_elder),
                out_of_taxonomy=out_of_taxonomy,
            )
        )
    return tuple(cases)


def rows_from_store(repo: str) -> list[LedgerRow]:
    """The INGESTED corpus (#361 slice 1): store rows -> LedgerRows via the
    slice-1 parser. Live-only (needs the DB env); imported lazily so the
    pure paths never touch the adapter."""
    from adapters.pg_install_store import list_ledger_rows  # type: ignore

    out: list[LedgerRow] = []
    for d in list_ledger_rows(repo):
        row = parse_row(d)
        if row is not None:
            out.append(row)
    return out
