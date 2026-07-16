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
        TrialResult(
            concurrency=1, fixture="small", backend="cave",
            ttft_s=0.1, complete_s=1.0, parse_ok=True, errored=False,
            prompt_chars=100, response_chars=50, completion_tokens=10,
        ),
        TrialResult(
            concurrency=1, fixture="small", backend="cave",
            ttft_s=0.2, complete_s=3.0, parse_ok=True, errored=False,
            prompt_chars=100, response_chars=50, completion_tokens=10,
        ),
        TrialResult(
            concurrency=1, fixture="small", backend="cave",
            ttft_s=None, complete_s=2.0, parse_ok=True, errored=False,
            prompt_chars=100, response_chars=50, completion_tokens=10,
        ),
        TrialResult(
            concurrency=2, fixture="medium", backend="cave",
            ttft_s=0.5, complete_s=4.0, parse_ok=False, errored=False,
            prompt_chars=200, response_chars=80, completion_tokens=20,
        ),
        TrialResult(
            concurrency=2, fixture="medium", backend="cave",
            ttft_s=None, complete_s=9.0, parse_ok=False, errored=True,
            prompt_chars=200, response_chars=0, completion_tokens=None,
        ),
    ]
    # Cell wall for C=1 is 2.0s (not sum of 1+3+2); C=2 is 4.0s.
    walls = {("cave", 1): 2.0, ("cave", 2): 4.0}
    report = summarize_trials(trials, cell_wall_s=walls)
    assert len(report.slices) == 2
    c1 = next(s for s in report.slices if s.concurrency == 1)
    assert c1.fixture == "small"
    assert c1.n == 3
    assert c1.errors == 0
    assert c1.p50_complete_s == 2.0
    assert c1.p95_complete_s == 3.0
    assert c1.p50_ttft_s == 0.1  # nearest-rank of [0.1, 0.2]
    assert c1.p95_ttft_s == 0.2
    # 30 tokens / 2.0s wall
    assert c1.aggregate_tokens_per_s == 15.0
    # 150 chars / 2.0s
    assert c1.aggregate_chars_per_s == 75.0
    c2 = next(s for s in report.slices if s.concurrency == 2)
    assert c2.fixture == "medium"
    assert c2.errors == 1
    assert c2.parse_failures == 1
    md = report.as_markdown()
    assert "p50 complete" in md
    assert "cave" in md
    assert "small" in md


def test_main_exits_2_without_backends(monkeypatch):
    from review_latency import __main__ as main_mod

    monkeypatch.delenv("GRUG_BENCH_CAVE_URL", raising=False)
    monkeypatch.delenv("GRUG_BENCH_CAVE_MODEL", raising=False)
    monkeypatch.delenv("GRUG_BENCH_REASONER_URL", raising=False)
    assert main_mod.main(["--levels", "1"]) == 2
