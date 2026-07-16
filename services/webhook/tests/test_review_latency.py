"""CI-safe tests for review_latency pure core (#648)."""

from __future__ import annotations

from review_latency.fixtures import default_fixtures
from review_latency.scoring import (
    TrialResult,
    percentile,
    summarize_trials,
)


def test_default_fixtures_build_elder_shaped_prompts():
    fixtures = default_fixtures()
    names = {f.name for f in fixtures}
    assert names == {"small", "medium", "large"}
    for f in fixtures:
        assert f.prompt_chars > 500, f"{f.name} prompt too small for latency stress"
        assert f.added_lines > 0
        assert any(m["role"] == "system" for m in f.messages)
        assert any(m["role"] == "user" for m in f.messages)
        # Larger fixtures must cost more prefill.
    by_name = {f.name: f for f in fixtures}
    assert by_name["small"].prompt_chars < by_name["medium"].prompt_chars
    assert by_name["medium"].prompt_chars < by_name["large"].prompt_chars


def test_percentile_nearest_rank():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(vals, 50) == 3.0
    assert percentile(vals, 95) == 5.0
    assert percentile([], 50) is None


def test_summarize_trials_groups_and_p50():
    trials = [
        TrialResult(1, "small", "cave", 0.1, 1.0, True, False, 100, 50),
        TrialResult(1, "small", "cave", 0.2, 3.0, True, False, 100, 50),
        TrialResult(1, "small", "cave", None, 2.0, True, False, 100, 50),
        TrialResult(2, "medium", "cave", 0.5, 4.0, False, False, 200, 80),
        TrialResult(2, "medium", "cave", None, 9.0, False, True, 200, 0),
    ]
    report = summarize_trials(trials)
    assert len(report.slices) == 2
    c1 = next(s for s in report.slices if s.concurrency == 1)
    assert c1.n == 3
    assert c1.errors == 0
    assert c1.p50_complete_s == 2.0
    assert c1.p50_ttft_s == 0.1 or c1.p50_ttft_s == 0.2
    c2 = next(s for s in report.slices if s.concurrency == 2)
    assert c2.errors == 1
    assert c2.parse_failures == 1  # non-error parse_ok=False
    md = report.as_markdown()
    assert "p50 complete" in md
    assert "cave" in md


def test_main_exits_2_without_backends(monkeypatch):
    from review_latency import __main__ as main_mod

    monkeypatch.delenv("GRUG_BENCH_CAVE_URL", raising=False)
    monkeypatch.delenv("GRUG_BENCH_CAVE_MODEL", raising=False)
    monkeypatch.delenv("GRUG_BENCH_REASONER_URL", raising=False)
    assert main_mod.main(["--levels", "1"]) == 2
