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
    res = ingest_text(text, put=lambda row, seq: puts.append((row["pr"], seq)))
    assert res == {"ingested": 2, "skipped": 2}
    assert puts == [(1, 0), (2, 0)]


def test_seq_disambiguates_same_key():
    puts = []
    # two findings sharing (repo, class, pr, reviewer) -> seq 0,1
    text = "\n".join([_line(pr=5, finding="a"), _line(pr=5, finding="b")])
    ingest_text(text, put=lambda row, seq: puts.append(seq))
    assert puts == [0, 1]


def test_ingest_is_reusable_across_classes():
    puts = []
    text = "\n".join([
        _line(pr=5, finding_class="silent-failure"),
        _line(pr=5, finding_class="correctness"),
    ])
    ingest_text(text, put=lambda row, seq: puts.append((row["class"], seq)))
    # different class -> independent seq counters, both 0
    assert puts == [("silent-failure", 0), ("correctness", 0)]
