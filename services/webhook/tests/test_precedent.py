"""Tests for precedent-cited findings + measured confidence (#555)."""

from __future__ import annotations

from ledger import LedgerRow
from personas.code_reviewer.precedent import (
    ClassPrecision,
    class_precision,
    match_precedent,
    render_precedent_note,
)
from personas.code_reviewer.repo_config import RepoConfig
from personas.code_reviewer.dispatch import _muted_classes


def _row(pr, cls, verdict, *, path="services/webhook/consumer.py", finding="x", ts=""):
    return LedgerRow(
        repo="quadseven/grug", pr=pr, reviewer="claude/x", severity="HIGH",
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

    def test_declined_counts_as_accepted_and_is_cited(self):
        """Ledger fact: `declined` = Grug was right, human chose not to change
        it -> ACCEPTED (citable + counted in precision), NOT a rejection."""
        rows = [
            _row(10, "c", "declined", path="services/webhook/consumer.py",
                 ts="2026-06-01T00:00:00Z"),
            _row(11, "c", "false-positive", path="services/webhook/consumer.py"),
        ]
        p = class_precision(rows)
        assert p["c"].accepted == 1     # declined counts as accepted
        assert p["c"].rejected == 1
        m = match_precedent(finding_class="c",
                            finding_path="services/webhook/consumer.py", ledger_rows=rows)
        assert [pr for pr, _ in m.citations] == [10]
        assert m.citations[0][1] == "declined"
        assert "kept" in render_precedent_note(m)  # verdict word, not "fixed"

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

    def test_null_pr_row_counts_for_precision_but_is_not_cited(self):
        """#764: a consensus row written outside a PR context has pr=None.
        It carries a real verdict, so it MUST move class precision - but it
        cannot be rendered as "Grug saw this in PR #N", and letting None into
        the citation dict would also raise on the (ts, pr) sort below."""
        rows = [
            _row(None, "c", "fixed", ts="2026-07-21T00:00:00Z"),
            _row(366, "c", "fixed", ts="2026-06-01T00:00:00Z"),
        ]
        m = match_precedent(finding_class="c", finding_path="services/webhook/consumer.py",
                            ledger_rows=rows)
        # cited: only the row that actually has a PR number
        assert [pr for pr, _ in m.citations] == [366]
        # counted: both rows, so the evidence is not thrown away
        assert m.labeled_history == 2
        assert class_precision(rows)["c"].accepted == 2

    def test_all_null_pr_rows_yield_precision_without_citations(self):
        """The sort-crash guard: a corpus where EVERY matching row lacks a PR
        must return cleanly with no citations, not raise comparing None to int."""
        rows = [
            _row(None, "c", "fixed", ts="2026-07-21T00:00:00Z"),
            _row(None, "c", "false-positive", ts="2026-07-22T00:00:00Z"),
        ]
        m = match_precedent(finding_class="c", finding_path="services/webhook/consumer.py",
                            ledger_rows=rows)
        assert m.citations == ()
        assert m.has_precedent is False
        assert m.class_precision == 0.5  # 1 accepted / 2 labeled


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


class TestMutedClasses:
    def _make_config(self, **overrides) -> RepoConfig:
        return RepoConfig(
            elder_mute_min_labeled=20,
            elder_mute_below_precision=0.2,
            elder_mute_locked_classes=("locked-class",),
            **overrides,
        )

    def _mock_ledger_rows(self, rows, monkeypatch):
        """Mock list_ledger_rows to return the test rows as dicts."""
        raw = []
        for r in rows:
            raw.append({
                "repo": r.repo,
                "pr": r.pr,
                "reviewer": r.reviewer,
                "severity": r.severity,
                "class": r.finding_class,
                "finding": r.finding,
                "verdict": r.verdict,
                "evidence": r.evidence,
                "ts": r.ts,
                "commit": r.commit,
                "head_sha": r.head_sha,
            })
        monkeypatch.setattr("adapters.install_store.list_ledger_rows", lambda _: raw)

    def test_class_with_high_labeled_low_precision_gets_muted(self, monkeypatch):
        """Class with >= 20 labeled and precision < 0.2 is muted."""
        rows = [
            _row(i, "noisy-rule", "fixed" if i <= 4 else "false-positive")
            for i in range(1, 25)
        ]
        self._mock_ledger_rows(rows, monkeypatch)
        config = self._make_config()
        muted, stats = _muted_classes("quadseven/grug", config)
        assert "noisy-rule" in muted
        assert stats["noisy-rule"] == (4, 24)

    def test_class_below_min_labeled_not_muted(self, monkeypatch):
        """Class with < 20 labeled never gets muted regardless of precision."""
        rows = [
            _row(i, "rare-rule", "false-positive")
            for i in range(1, 10)  # Only 9 labeled
        ]
        self._mock_ledger_rows(rows, monkeypatch)
        config = self._make_config()
        muted, _ = _muted_classes("quadseven/grug", config)
        assert "rare-rule" not in muted

    def test_class_above_precision_threshold_not_muted(self, monkeypatch):
        """Class with >= 20 labeled but precision >= 0.2 is not muted."""
        rows = [
            _row(i, "ok-rule", "fixed" if i <= 6 else "false-positive")
            for i in range(1, 25)  # 24 labeled, 6 accepted = 0.25 precision
        ]
        self._mock_ledger_rows(rows, monkeypatch)
        config = self._make_config()
        muted, _ = _muted_classes("quadseven/grug", config)
        assert "ok-rule" not in muted

    def test_locked_class_never_muted(self, monkeypatch):
        """Locked classes are never muted even with low precision."""
        rows = [
            _row(i, "locked-class", "false-positive")
            for i in range(1, 30)  # 29 labeled, 0 accepted = 0 precision
        ]
        self._mock_ledger_rows(rows, monkeypatch)
        config = self._make_config()
        muted, _ = _muted_classes("quadseven/grug", config)
        assert "locked-class" not in muted

    def test_muted_stats_returned_for_board(self, monkeypatch):
        """Stats dict contains (fixed, labeled) for board rendering."""
        rows = [
            _row(i, "muted-rule", "fixed" if i <= 3 else "false-positive")
            for i in range(1, 25)  # 24 labeled, 3 accepted
        ]
        self._mock_ledger_rows(rows, monkeypatch)
        config = self._make_config()
        _, stats = _muted_classes("quadseven/grug", config)
        assert stats["muted-rule"] == (3, 24)

    def test_no_repo_config_returns_empty(self):
        """Without RepoConfig, no classes are muted."""
        muted, stats = _muted_classes("quadseven/grug", None)
        assert muted == set()
        assert stats == {}
