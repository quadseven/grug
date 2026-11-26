import json
import re
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METRICS = {
    "defects_caught",
    "false_positives_per_clean_pr",
    "time_to_verdict_seconds",
}


def test_defect_benchmark_baseline_pins_corpus_and_marks_unmeasured_scores():
    baseline = json.loads((ROOT / "bench/baseline.json").read_text(encoding="utf-8"))
    corpus = baseline["corpus"]
    manifest = ROOT / corpus["manifest"]
    cases = json.loads(manifest.read_text(encoding="utf-8"))
    defects = sum(case["expected_class"] is not None for case in cases)
    assert defects == corpus["defects"] == 3
    assert len(cases) - defects == corpus["clean_prs"] == 2
    inputs = {manifest.name}
    for case in cases:
        inputs.add(case["diff"])
        inputs.update(case["context"].values())
    assert corpus["sha256"] == {
        name: sha256((manifest.parent / name).read_bytes()).hexdigest()
        for name in inputs
    }

    assert baseline["status"] == "corpus_only"
    revisions = baseline["revisions"]
    assert set(revisions) == {"pre_epic", "current_default"}
    for revision in revisions.values():
        assert re.fullmatch(r"[0-9a-f]{40}", revision["commit"])
        assert revision["scores"] == dict.fromkeys(METRICS)
    assert revisions["pre_epic"]["commit"] != revisions["current_default"]["commit"]
