"""Pure tests for the SAST benchmark scoring core (#399, ADR-0006).

NO LLM, NO network — these run in the normal CI test suite. They feed
SYNTHETIC findings into the pure scoring core and assert recall/precision,
the #391 FP guard, and baseline-regression detection. The live recall
measurement (real backends) is the on-demand `benchmark.sast.yml` job, not
these tests.
"""

from __future__ import annotations

from sast_benchmark.corpus import CorpusSample, load_corpus
from sast_benchmark.scoring import (
    compare_to_baseline,
    score,
    to_baseline_dict,
)


# --- corpus integrity ------------------------------------------------------


def test_corpus_loads_with_tp_and_fp():
    corpus = load_corpus()
    assert any(s.is_true_positive for s in corpus), "need true positives for recall"
    assert any(not s.is_true_positive for s in corpus), "need the #391 FP guard"
    # The #391 public-config-path guard must be present by name.
    assert any(s.name == "fp_public_config_path_log" for s in corpus)


def test_corpus_keys_unique():
    corpus = load_corpus()
    assert len({s.name for s in corpus}) == len(corpus)
    assert len({s.path for s in corpus}) == len(corpus)


# --- scoring ---------------------------------------------------------------

_TP_A = CorpusSample("a", "sql-injection", "bench/a.py", "+x", True)
_TP_B = CorpusSample("b", "sql-injection", "bench/b.py", "+y", True)
_TP_C = CorpusSample("c", "ssrf", "bench/c.py", "+z", True)
_FP = CorpusSample("fp", "benign-config-log", "bench/fp.py", "+w", False)
_SAMPLES = (_TP_A, _TP_B, _TP_C, _FP)


def test_perfect_run_recall_one_precision_one():
    """All TPs flagged, FP suppressed -> recall 1.0 per class, precision 1.0."""
    report = score(_SAMPLES, {"a": 1, "b": 1, "c": 2, "fp": 0})
    assert report.per_class_recall == {"sql-injection": 1.0, "ssrf": 1.0}
    assert report.precision == 1.0
    assert report.overall_recall == 1.0
    assert report.fp_flagged == ()


def test_partial_recall_per_class():
    """One of two sql-injection TPs flagged -> class recall 0.5; ssrf missed
    -> 0.0. FP still clean -> precision 1.0."""
    report = score(_SAMPLES, {"a": 1, "b": 0, "c": 0, "fp": 0})
    assert report.per_class_recall == {"sql-injection": 0.5, "ssrf": 0.0}
    assert report.precision == 1.0


def test_false_positive_drags_precision_and_is_listed():
    """The #391 guard: flagging the FP sample is a precision miss and is named
    in fp_flagged."""
    report = score(_SAMPLES, {"a": 1, "b": 1, "c": 1, "fp": 1})
    assert report.fp_flagged == ("fp",)
    # 3 TP flags / 4 total flags = 0.75
    assert report.precision == 0.75


def test_nothing_flagged_precision_vacuously_one_recall_zero():
    """Flag nothing: precision is vacuously 1.0 (no FPs emitted) but recall is
    0.0 — recall is what catches a do-nothing reviewer, not precision."""
    report = score(_SAMPLES, {})
    assert report.precision == 1.0
    assert report.overall_recall == 0.0
    assert report.per_class_recall == {"sql-injection": 0.0, "ssrf": 0.0}


# --- baseline regression ---------------------------------------------------


def test_recall_drop_is_a_regression():
    base = to_baseline_dict(
        score(_SAMPLES, {"a": 1, "b": 1, "c": 1, "fp": 0}), backend="x"
    )
    worse = score(_SAMPLES, {"a": 1, "b": 0, "c": 1, "fp": 0})  # sql recall 1.0->0.5
    regs = compare_to_baseline(worse, base)
    assert any(r.kind == "recall_drop" and r.subject == "sql-injection" for r in regs)


def test_recall_improvement_is_not_a_regression():
    base = to_baseline_dict(
        score(_SAMPLES, {"a": 1, "b": 0, "c": 0, "fp": 0}), backend="x"
    )
    better = score(_SAMPLES, {"a": 1, "b": 1, "c": 1, "fp": 0})
    assert compare_to_baseline(better, base) == ()


def test_new_false_positive_is_a_regression():
    base = to_baseline_dict(
        score(_SAMPLES, {"a": 1, "b": 1, "c": 1, "fp": 0}), backend="x"
    )
    regressed = score(_SAMPLES, {"a": 1, "b": 1, "c": 1, "fp": 1})
    regs = compare_to_baseline(regressed, base)
    assert any(r.kind == "new_false_positive" and r.subject == "fp" for r in regs)


def test_new_class_not_in_baseline_is_not_a_regression():
    """A corpus class absent from the baseline doesn't fail the gate (re-record
    to capture it) — only a DROP on a known class does."""
    base = {"per_class_recall": {"sql-injection": 1.0}, "fp_flagged": []}
    report = score(_SAMPLES, {"a": 1, "b": 1, "c": 0, "fp": 0})  # ssrf new, 0.0
    regs = compare_to_baseline(report, base)
    assert all(r.subject != "ssrf" for r in regs)


def test_baseline_dict_shape_is_sorted_and_serializable():
    import json

    d = to_baseline_dict(score(_SAMPLES, {"a": 1, "b": 1, "c": 1, "fp": 0}), backend="poolside")
    assert d["backend"] == "poolside"
    assert list(d["per_class_recall"]) == sorted(d["per_class_recall"])
    json.dumps(d)  # must round-trip to the committed baseline.json


# --- backend env-config (backends.py) --------------------------------------


def test_configured_backends_honors_env(monkeypatch):
    from sast_benchmark import backends

    for var in (
        "GRUG_BENCH_OPENROUTER_KEY", "GRUG_BENCH_POOLSIDE_KEY",
        "GRUG_BENCH_CAVE_URL", "GRUG_BENCH_CAVE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    assert backends.configured_backends() == []  # nothing configured -> empty

    monkeypatch.setenv("GRUG_BENCH_OPENROUTER_KEY", "k")
    monkeypatch.setenv("GRUG_BENCH_CAVE_URL", "http://cave.example/v1/chat/completions")
    monkeypatch.setenv("GRUG_BENCH_CAVE_MODEL", "qwen-coder")
    configured = backends.configured_backends()
    names = {b.name for b in configured}
    assert names == {"openrouter", "sparkles"}  # poolside absent (no key)
    openrouter = next(b for b in configured if b.name == "openrouter")
    assert openrouter.model == "anthropic/claude-opus-4.7"
    assert openrouter.extra_body["reasoning"] == {
        "effort": "high", "exclude": True,
    }


def test_cave_needs_both_url_and_model(monkeypatch):
    """sparkles only runs with BOTH a URL and a model (no partial/leaky default)."""
    from sast_benchmark import backends

    for var in ("GRUG_BENCH_OPENROUTER_KEY", "GRUG_BENCH_POOLSIDE_KEY", "GRUG_BENCH_CAVE_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GRUG_BENCH_CAVE_URL", "http://cave.example/v1/chat/completions")
    assert backends.configured_backends() == []  # URL without MODEL -> not configured


def test_cave_carries_require_keys_json_schema_others_keep_json_object(monkeypatch):
    """#544: Ollama maps bare json_object to format=json which silently
    truncates multi-item answers, so the Cave backend must carry a
    require-keys json_schema response_format via extra_body (which the
    runner's body-dict merge lets override the default). Cloud backends keep
    json_object so their numbers stay comparable with the #537 baselines."""
    from sast_benchmark import backends, runner

    monkeypatch.setenv("GRUG_BENCH_OPENROUTER_KEY", "k")
    monkeypatch.setenv("GRUG_BENCH_CAVE_URL", "http://cave.example/v1/chat/completions")
    monkeypatch.setenv("GRUG_BENCH_CAVE_MODEL", "qwen-coder")
    monkeypatch.delenv("GRUG_BENCH_POOLSIDE_KEY", raising=False)
    configured = {b.name: b for b in backends.configured_backends()}

    # The Cave's schema requires the findings envelope with the exact fields
    # Elder's parser (_coerce_finding) demands.
    rf = configured["sparkles"].extra_body["response_format"]
    assert rf["type"] == "json_schema"
    schema = rf["json_schema"]["schema"]
    assert schema["required"] == ["findings"]
    item = schema["properties"]["findings"]["items"]
    assert item["required"] == ["path", "line", "rule", "severity", "message"]
    assert item["properties"]["severity"]["enum"] == [
        "low", "medium", "high", "critical",
    ]
    # Cloud backend unchanged: no response_format override.
    assert "response_format" not in configured["openrouter"].extra_body

    # And the runner's POST body honors the override: capture the body per
    # backend and assert the resulting response_format type.
    sent = {}

    def _capture(url, json=None, headers=None, timeout=None):
        sent[json["model"]] = json["response_format"]["type"]

        class _R:  # minimal response stand-in; runner returns it verbatim
            status_code = 200

        return _R()

    monkeypatch.setattr(runner.httpx, "post", _capture)
    runner._post(configured["sparkles"], [{"role": "user", "content": "x"}])
    runner._post(configured["openrouter"], [{"role": "user", "content": "x"}])
    assert sent["qwen-coder"] == "json_schema"
    assert sent["anthropic/claude-opus-4.7"] == "json_object"


# --- runner wiring (no real LLM) -------------------------------------------


class _F:
    """Duck-typed finding: the runner only reads `.path`."""

    def __init__(self, path):
        self.path = path


def test_run_sample_counts_findings_on_sample_path(monkeypatch):
    from sast_benchmark import runner

    sample = _TP_A  # path bench/a.py
    monkeypatch.setattr(runner, "_build_messages", lambda hunks, ver, *a: [{"role": "user", "content": "x"}])
    monkeypatch.setattr(runner, "_post", lambda b, m: object())
    # Two findings on this path + one on another path -> count 2.
    monkeypatch.setattr(
        runner, "_parse_response",
        lambda resp: ([_F("bench/a.py"), _F("bench/a.py"), _F("other.py")], "model", None),
    )
    backend = _bench_backend()
    assert runner.run_sample(backend, sample) == (2, False)


def test_run_sample_transport_error_returns_zero(monkeypatch):
    """A backend transport failure reads as 'found nothing' (the honest floor),
    never raises (one sample must not abort the sweep)."""
    from sast_benchmark import runner

    def _boom(b, m):
        raise RuntimeError("backend down")

    monkeypatch.setattr(runner, "_build_messages", lambda hunks, ver, *a: [{"role": "user", "content": "x"}])
    monkeypatch.setattr(runner, "_post", _boom)
    count, errored = runner.run_sample(_bench_backend(), _TP_A)
    assert count == 0 and errored is True


def test_run_backend_maps_every_sample(monkeypatch):
    from sast_benchmark import runner

    monkeypatch.setattr(runner, "_build_messages", lambda hunks, ver, *a: [{"role": "user", "content": "x"}])
    monkeypatch.setattr(runner, "_post", lambda b, m: object())
    monkeypatch.setattr(runner, "_parse_response", lambda resp: ([], "m", None))
    run = runner.run_backend(_bench_backend(), _SAMPLES)
    assert set(run.findings_by_sample) == {s.name for s in _SAMPLES}
    assert all(v == 0 for v in run.findings_by_sample.values())
    assert run.errors == 0 and run.all_errored is False


def test_run_backend_all_errored_is_flagged(monkeypatch):
    """Every sample erroring -> all_errored True, so the CLI can reject a bogus
    zero-recall baseline (broken run != 'Elder found nothing')."""
    from sast_benchmark import runner

    def _boom(b, m):
        raise RuntimeError("backend unreachable")

    monkeypatch.setattr(runner, "_build_messages", lambda hunks, ver, *a: [{"role": "user", "content": "x"}])
    monkeypatch.setattr(runner, "_post", _boom)
    run = runner.run_backend(_bench_backend(), _SAMPLES)
    assert run.errors == len(_SAMPLES)
    assert run.all_errored is True
    # Counts are all zero, but all_errored tells the caller this is NOT a result.
    assert all(v == 0 for v in run.findings_by_sample.values())


def _bench_backend():
    from sast_benchmark.backends import BenchBackend

    return BenchBackend(name="t", url="http://x/v1", model="m", api_key="")
