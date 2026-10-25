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
        repo="quadseven/grug", pr=1, reviewer="codex", severity="HIGH",
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
    # Also the counterweight to #764: `pr` became optional so null rows stop
    # being discarded, but a non-numeric pr is genuine corruption and must
    # still skip. Loosening the null case must not loosen validation.
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
    """Guard against format drift: the committed corpus must parse.

    The `>= 100` floor below is deliberately NOT the real guard - it passed
    happily while 6 rows were being silently deleted (#764). The
    every-line-parses assertion underneath is the one that bites: a row the
    parser rejects is a row that vanishes from the learning corpus without
    any error, so drift must fail loudly here rather than shrink the corpus.
    """
    from pathlib import Path
    p = Path(__file__).resolve().parents[3] / "logs" / "review-ledger.jsonl"
    text = p.read_text()
    rows = parse_jsonl(text)
    assert len(rows) >= 100  # 150-row corpus at time of writing
    # every parsed row has the load-bearing fields
    assert all(r.repo and r.reviewer and r.finding_class and r.verdict for r in rows)

    # ZERO silent drops: one parsed row per non-blank line, no exceptions.
    non_blank = [ln for ln in text.splitlines() if ln.strip()]
    dropped = [
        ln for ln in non_blank if parse_row(json.loads(ln)) is None
    ]
    assert not dropped, (
        f"{len(dropped)} committed ledger row(s) fail to parse and are being "
        f"silently dropped from the corpus. First: {dropped[0][:200]}"
    )
    assert len(rows) == len(non_blank)


def test_parse_row_accepts_null_pr():
    """#764: a consensus finding written outside a PR context carries
    `pr: null`. It is a real, verdict-bearing row - it must parse, not be
    discarded as malformed."""
    r = _row(pr=None)
    assert isinstance(r, LedgerRow)
    assert r.pr is None
    # and it must still be labelable, which is the whole point
    assert r.accepted is True
    fp = _row(pr=None, verdict="false-positive")
    assert isinstance(fp, LedgerRow)
    assert fp.false_positive is True


def test_parse_row_accepts_missing_pr_key():
    """Absent key behaves the same as an explicit null - `pr` is optional."""
    d = {
        "repo": "quadseven/grug", "reviewer": "codex", "severity": "HIGH",
        "class": "silent-failure", "finding": "x", "verdict": "fixed",
    }
    r = parse_row(d)
    assert r is not None and r.pr is None


# ── #683: per-finding category on the ledger row ───────────────────────


def test_parse_row_category_defaults_to_general():
    r = _row()
    assert r.category == "general"


def test_parse_row_category_carries_through():
    r = _row(category="security")
    assert r.category == "security"


def test_parse_row_category_invalid_degrades_to_general():
    """An invalid category is coerced to the default - the row is kept."""
    d = {
        "repo": "quadseven/grug", "pr": 1, "reviewer": "codex",
        "severity": "HIGH", "class": "silent-failure", "finding": "x",
        "verdict": "fixed", "category": "not-a-category",
    }
    r = parse_row(d)
    assert r is not None and r.category == "general"


def test_parse_row_category_missing_degrades_to_general():
    """A missing category degrades to the default - the row is kept."""
    d = {
        "repo": "quadseven/grug", "pr": 1, "reviewer": "codex",
        "severity": "HIGH", "class": "silent-failure", "finding": "x",
        "verdict": "fixed",
    }
    r = parse_row(d)
    assert r is not None and r.category == "general"


def test_accepted_findings_by_class_still_groups_by_class_not_category():
    """category is a separate axis; the class aggregation is unchanged."""
    rows = [
        _row(finding_class="c", category="security", finding="A", verdict="fixed"),
        _row(finding_class="c", category="correctness", finding="B", verdict="fixed"),
    ]
    by_class = accepted_findings_by_class(rows)
    assert [r.finding for r in by_class["c"]] == ["A", "B"]


def test_ledger_digest_is_content_stable():
    """#536 LORE: the store sk digest must depend on finding CONTENT, not
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


def test_ledger_sk_handles_null_pr():
    """#764: a row with no PR must still get a stable, non-colliding sk.

    `NOPR` is non-numeric on purpose. The corpus scan orders by
    `sk COLLATE "C"` (raw byte order), where 'N' (0x4E) sorts after every
    digit (0x30-0x39), so unattributed rows land at the END of their class
    rather than in the middle of the PR sequence. A zero-padded sentinel
    would be indistinguishable from a real PR #0.
    """
    from adapters.pg_install_store import _ledger_digest, _ledger_sk
    d = _ledger_digest({"finding": "f", "ts": "t", "evidence": "e"})
    none_sk = _ledger_sk("silent-failure", None, "codex", d)
    assert "#NOPR#" in none_sk
    # stable across calls (idempotent upsert)
    assert none_sk == _ledger_sk("silent-failure", None, "codex", d)
    # cannot collide with any real PR number, including 0
    assert none_sk != _ledger_sk("silent-failure", 0, "codex", d)
    # and sorts after every numeric pr within the same class
    assert none_sk > _ledger_sk("silent-failure", 9999999, "codex", d)
