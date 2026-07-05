"""#361 slice 1: ledger ingest - seq disambiguation, skip counting."""

from __future__ import annotations

import json

from ingest_ledger import ingest_text


def _line(**kw):
    base = dict(repo="githumps/grug", pr=1, reviewer="codex",
                severity="HIGH", finding="f", verdict="fixed")
    base.update(kw)
    return json.dumps({**base, "class": kw.get("finding_class", "silent-failure")})


def test_ingest_persists_valid_and_counts_skips():
    puts = []
    text = "\n".join([
        _line(pr=1),
        "",                 # blank -> skip (not counted)
        "{bad json",        # malformed -> skipped
        json.dumps({"repo": "r"}),  # missing fields -> skipped
        _line(pr=2),
    ])
    res = ingest_text(text, put=lambda row: puts.append(row["pr"]))
    assert res == {"ingested": 2, "skipped": 2}
    assert puts == [1, 2]


def test_all_valid_rows_are_put():
    puts = []
    # two findings sharing (repo, class, pr, reviewer) but different content
    # -> both persisted (content-derived key disambiguates, no seq needed)
    text = "\n".join([_line(pr=5, finding="a"), _line(pr=5, finding="b")])
    ingest_text(text, put=lambda row: puts.append(row["finding"]))
    assert puts == ["a", "b"]


def test_ingest_is_reusable_across_classes():
    puts = []
    text = "\n".join([
        _line(pr=5, finding_class="silent-failure"),
        _line(pr=5, finding_class="correctness"),
    ])
    ingest_text(text, put=lambda row: puts.append(row["class"]))
    assert puts == ["silent-failure", "correctness"]
