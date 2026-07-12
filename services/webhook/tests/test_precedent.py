"""Tests for precedent-cited findings + measured confidence (#555)."""

from __future__ import annotations

from ledger import LedgerRow
from personas.code_reviewer.precedent import (
    ClassPrecision,
    class_precision,
    match_precedent,
    render_precedent_note,
)


def _row(pr, cls, verdict, *, path="services/webhook/consumer.py", finding="x", ts=""):
    return LedgerRow(
        repo="githumps/grug", pr=pr, reviewer="claude/x", severity="HIGH",
        finding_class=cls, finding=finding, verdict=verdict,
        evidence=f"re-targeted {path}", ts=ts,
    )


class TestClassPrecision:
    def test_accept_reject_tally(self):
        rows = [
            _row(1, "sync-io-in-async", "fixed"),
            _row(2, "sync-io-in-async", "fixed"),
            _row(3, "sync-io-in-async", "false-positive"),
            _row(4, "flaky", "fixed"),
        ]
        p = class_precision(rows)
        assert p["sync-io-in-async"].accepted == 2
        assert p["sync-io-in-async"].rejected == 1
        assert round(p["sync-io-in-async"].precision, 3) == 0.667
        assert p["flaky"].precision == 1.0

    def test_unlabeled_class_is_half_not_one(self):
        cp = ClassPrecision("x", 0, 0)
        assert cp.labeled == 0
        assert cp.precision == 0.5


class TestMatchPrecedent:
    def _corpus(self):
        return [
            _row(366, "sync-io-in-async", "fixed",
                 path="services/webhook/dispatcher.py", ts="2026-06-01T00:00:00Z"),
            _row(400, "sync-io-in-async", "fixed",
                 path="services/webhook/dispatcher.py", ts="2026-06-10T00:00:00Z"),
            _row(401, "sync-io-in-async", "false-positive",   # FP: never cited
                 path="services/webhook/dispatcher.py", ts="2026-06-11T00:00:00Z"),
            _row(500, "n-plus-one", "fixed",
                 path="services/api/store.py", ts="2026-06-12T00:00:00Z"),
        ]

    def test_cites_prior_accepted_same_class_same_region_recent_first(self):
        m = match_precedent(
            finding_class="sync-io-in-async",
            finding_path="services/webhook/dispatcher.py",
            ledger_rows=self._corpus(),
        )
        assert m.has_precedent
        assert [pr for pr, _ in m.citations] == [400, 366]   # recency desc, FP excluded
        assert m.labeled_history == 3
        assert round(m.class_precision, 3) == 0.667
        assert m.confidence_label == "medium"

    def test_no_cross_class_or_cross_file_citation(self):
        m = match_precedent(
            finding_class="sync-io-in-async",
            finding_path="services/api/store.py",   # only the n-plus-one row is here
            ledger_rows=self._corpus(),
        )
        assert not m.has_precedent           # class matches nothing in this file
        # but the class DID have labeled history elsewhere -> confidence still known
        assert m.labeled_history == 3

    def test_unknown_class_defaults_unproven(self):
        m = match_precedent(
            finding_class="brand-new-rule",
            finding_path="services/x.py",
            ledger_rows=self._corpus(),
        )
        assert not m.has_precedent
        assert m.labeled_history == 0
        assert m.class_precision == 0.5
        assert m.confidence_label == "unproven"

    def test_one_pr_cited_once_even_with_multiple_rows(self):
        rows = [
            _row(366, "c", "fixed", ts="2026-06-01T00:00:00Z"),
            _row(366, "c", "fixed", ts="2026-06-02T00:00:00Z"),  # same PR, 2nd row
        ]
        m = match_precedent(finding_class="c", finding_path="services/webhook/consumer.py",
                            ledger_rows=rows)
        assert [pr for pr, _ in m.citations] == [366]


class TestRenderNote:
    def test_precedent_and_confidence_rendered(self):
        m = match_precedent(
            finding_class="sync-io-in-async",
            finding_path="services/webhook/dispatcher.py",
            ledger_rows=[
                _row(400, "sync-io-in-async", "fixed",
                     path="services/webhook/dispatcher.py", ts="2026-06-10T00:00:00Z"),
            ],
        )
        note = render_precedent_note(m)
        assert "#400" in note
        assert "confidence" in note

    def test_silent_when_no_precedent_and_no_history(self):
        m = match_precedent(
            finding_class="brand-new", finding_path="x.py", ledger_rows=[],
        )
        assert render_precedent_note(m) == ""
