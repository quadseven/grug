"""#527: auto-derived best-practices - derivation, decay, bounded block."""

from __future__ import annotations

from best_practices import (
    Practice,
    derive_practices,
    practices_block,
    practices_from_dicts,
    practices_to_dicts,
)
from ledger import parse_row


def _r(pr, cls, finding, verdict="fixed"):
    return parse_row({"repo": "githumps/grug", "pr": pr, "reviewer": "codex",
                      "severity": "HIGH", "class": cls, "finding": finding,
                      "verdict": verdict, "ts": "", "evidence": ""})


def test_derive_ranks_by_hits_then_recency():
    rows = [
        _r(1, "silent-failure", "a"), _r(2, "silent-failure", "b"),
        _r(3, "correctness", "c"),
    ]
    ps = derive_practices(rows)
    assert ps[0].finding_class == "silent-failure" and ps[0].hits == 2
    assert ps[1].finding_class == "correctness"


def test_derive_excludes_false_positives():
    rows = [_r(1, "x", "real"), _r(2, "x", "wrong", verdict="false-positive")]
    ps = derive_practices(rows)
    assert ps[0].hits == 1 and ps[0].rule == "real"


def test_representative_rule_is_most_recent():
    rows = [_r(10, "c", "old"), _r(50, "c", "newest")]
    assert derive_practices(rows)[0].rule == "newest"


def test_decay_drops_stale_classes():
    # 'stale' last reinforced at pr 5; newest accepted pr is 400 -> decayed
    rows = [_r(5, "stale", "old"), _r(400, "fresh", "new")]
    classes = {p.finding_class for p in derive_practices(rows, decay_prs=100)}
    assert classes == {"fresh"}


def test_empty_and_all_fp_yield_no_practices():
    assert derive_practices([]) == []
    assert derive_practices([_r(1, "x", "w", verdict="false-positive")]) == []


def test_block_is_bounded_and_labeled():
    rows = [_r(i, f"class{i}", f"finding {i}") for i in range(1, 20)]
    block = practices_block(derive_practices(rows), top_n=3, max_chars=500)
    assert "TEAM-LEARNED PRACTICES" in block
    assert len(block) <= 500
    assert block.count("\n") <= 3  # header + up to 3 practices


def test_block_empty_when_no_practices():
    assert practices_block([]) == ""


def test_dict_roundtrip():
    ps = [Practice("c", "rule", 3, [5, 4], 5)]
    assert practices_from_dicts(practices_to_dicts(ps)) == ps
