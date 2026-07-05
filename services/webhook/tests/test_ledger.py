"""#361 slice 1: the pure ledger corpus - parsing + aggregations."""

from __future__ import annotations

import json

from ledger import (
    LedgerRow,
    accepted_findings_by_class,
    parse_jsonl,
    parse_row,
    reviewer_precision,
)


def _row(**kw):
    base = dict(
        repo="githumps/grug", pr=1, reviewer="codex", severity="HIGH",
        finding_class="silent-failure", finding="swallowed exception",
        verdict="fixed", evidence="", ts="2026-07-05T00:00:00Z", commit=None,
    )
    base.update(kw)
    d = {**base, "class": base.pop("finding_class")}
    return parse_row(d)


def test_parse_row_maps_class_field():
    r = _row()
    assert isinstance(r, LedgerRow)
    assert r.finding_class == "silent-failure"
    assert r.accepted and not r.false_positive


def test_parse_row_skips_missing_fields():
    assert parse_row({"repo": "x"}) is None
    assert parse_row({"pr": "notanint", "repo": "x", "reviewer": "c",
                      "class": "y", "finding": "z", "verdict": "fixed"}) is None


def test_accepted_and_false_positive_verdicts():
    assert _row(verdict="fixed").accepted
    assert _row(verdict="declined").accepted
    assert _row(verdict="false-positive").false_positive
    assert not _row(verdict="false-positive").accepted


def test_parse_jsonl_skips_blank_and_malformed():
    text = "\n".join([
        json.dumps({"repo": "r", "pr": 1, "reviewer": "codex", "class": "c",
                    "finding": "f", "verdict": "fixed", "severity": "HIGH"}),
        "",
        "{not json",
        json.dumps({"repo": "r", "pr": 2, "reviewer": "spark", "class": "c",
                    "finding": "g", "verdict": "false-positive", "severity": "LOW"}),
    ])
    rows = parse_jsonl(text)
    assert len(rows) == 2
    assert rows[0].pr == 1 and rows[1].pr == 2


def test_accepted_findings_by_class_excludes_false_positives():
    rows = [
        _row(finding_class="silent-failure", verdict="fixed", finding="A"),
        _row(finding_class="silent-failure", verdict="false-positive", finding="B"),
        _row(finding_class="correctness", verdict="declined", finding="C"),
    ]
    by_class = accepted_findings_by_class(rows)
    assert [r.finding for r in by_class["silent-failure"]] == ["A"]  # B (FP) excluded
    assert [r.finding for r in by_class["correctness"]] == ["C"]


def test_accepted_findings_ranked_by_severity_and_capped():
    rows = [
        _row(finding_class="c", severity="LOW", finding="low", verdict="fixed"),
        _row(finding_class="c", severity="CRITICAL", finding="crit", verdict="fixed"),
        _row(finding_class="c", severity="MEDIUM", finding="med", verdict="fixed"),
        _row(finding_class="c", severity="HIGH", finding="high", verdict="fixed"),
    ]
    top2 = accepted_findings_by_class(rows, top_n=2)["c"]
    assert [r.finding for r in top2] == ["crit", "high"]  # severity-ordered, capped at 2


def test_reviewer_precision():
    rows = [
        _row(reviewer="codex", verdict="fixed"),
        _row(reviewer="codex", verdict="declined"),
        _row(reviewer="spark", verdict="false-positive"),
        _row(reviewer="spark", verdict="fixed"),
    ]
    scores = reviewer_precision(rows)
    assert scores["codex"].accepted == 2 and scores["codex"].false_positives == 0
    assert scores["codex"].precision == 1.0
    assert scores["spark"].total == 2 and scores["spark"].precision == 0.5


def test_reviewer_precision_no_findings_is_one():
    assert reviewer_precision([]) == {}
    # a reviewer with only unlabeled verdicts -> precision defaults to 1.0
    r = _row(reviewer="new", verdict="pending")
    scores = reviewer_precision([r])
    assert scores["new"].total == 0 and scores["new"].precision == 1.0


def test_parses_the_real_committed_ledger():
    """Guard against format drift: the committed corpus must parse."""
    from pathlib import Path
    p = Path(__file__).resolve().parents[3] / "logs" / "review-ledger.jsonl"
    rows = parse_jsonl(p.read_text())
    assert len(rows) >= 100  # 150-row corpus at time of writing
    # every parsed row has the load-bearing fields
    assert all(r.repo and r.reviewer and r.finding_class and r.verdict for r in rows)


def test_ledger_digest_is_content_stable():
    """#536 Qodo: the store sk digest must depend on finding CONTENT, not
    ingest order - so re-ingesting a reordered corpus heals in place."""
    from adapters.pg_install_store import _ledger_digest, _ledger_sk
    a = {"finding": "swallowed exception", "ts": "2026-07-05T00:00:00Z", "evidence": "e1"}
    b = {"finding": "different finding", "ts": "2026-07-05T00:00:00Z", "evidence": "e1"}
    assert _ledger_digest(a) == _ledger_digest(a)      # deterministic
    assert _ledger_digest(a) != _ledger_digest(b)      # content-sensitive
    # same finding -> same sk regardless of when it's ingested
    sk1 = _ledger_sk("silent-failure", 5, "codex", _ledger_digest(a))
    sk2 = _ledger_sk("silent-failure", 5, "codex", _ledger_digest(a))
    assert sk1 == sk2
