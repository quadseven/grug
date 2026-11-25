from __future__ import annotations

import json
import re
from pathlib import Path

from elder_eval import free_confirm_corpus

ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "bench" / "baseline.json"
METRICS = (
    "defects_caught",
    "false_positives_per_clean_pr",
    "time_to_verdict_seconds",
)


def test_baseline_records_the_fixed_corpus_and_unmeasured_fields() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    corpus = free_confirm_corpus.build_cases()

    assert baseline["schema"] == 1
    assert baseline["status"] == "corpus_only"
    assert baseline["corpus"]["source"] == (
        "services/webhook/elder_eval/free_confirm_corpus.py"
    )
    assert baseline["corpus"]["digest"] == free_confirm_corpus.corpus_sha()
    assert baseline["corpus"]["case_count"] == len(corpus) == 48
    assert baseline["corpus"]["defect_cases"] == sum(c.defective for c in corpus) == 24
    assert baseline["corpus"]["clean_cases"] == sum(not c.defective for c in corpus) == 24
    assert baseline["corpus"]["languages"] == sorted({c.language for c in corpus})

    revisions = baseline["revisions"]
    assert set(revisions) == {"pre_epic", "current_default"}
    for revision in revisions.values():
        assert all(metric in revision for metric in METRICS)
        assert all(revision[metric] is None for metric in METRICS)

    assert revisions["pre_epic"]["commit"] is None
    assert re.fullmatch(r"[0-9a-f]{40}", revisions["current_default"]["commit"])
    missing = "\n".join(baseline["missing_requirements"]).lower()
    for requirement in (
        "no network",
        "pre-epic revision",
        "verdict duration",
        "fixed model selection",
    ):
        assert requirement in missing
