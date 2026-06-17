"""Pure scoring for the SAST benchmark (#399, ADR-0006).

NO LLM, NO I/O, fully deterministic — this is the CI-safe core, unit-tested
with synthetic findings. The live runner (`runner.py`) produces
`findings_by_sample`; this module turns that into recall/precision and
compares against the committed baseline.

Metrics (matches PRD #392 "SAST recall + LLM precision"):
- A sample is FLAGGED if the runner returned >= 1 finding whose path matches
  the sample's path. (Each corpus path is unique, so a finding maps to exactly
  one sample.)
- RECALL (per class, over true-positive samples): flagged TP samples / TP
  samples. A class with all TP samples flagged has recall 1.0.
- PRECISION (overall): flagged TP samples / all flagged samples. Any flag on a
  false-positive sample (the #391 guard) drags precision down — that is the
  whole point of the FP guard.
"""

from __future__ import annotations

from dataclasses import dataclass

from .corpus import CorpusSample


@dataclass(frozen=True, slots=True)
class SampleResult:
    """Per-sample outcome. `flagged` = the runner returned >=1 finding on this
    sample's path. `is_true_positive` is carried from ground truth so a report
    consumer needn't re-join against the corpus."""

    name: str
    vuln_class: str
    is_true_positive: bool
    flagged: bool


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Scored outcome over the whole corpus. `per_class_recall` maps each
    true-positive class -> recall in [0,1]. `precision` is overall in [0,1]
    (1.0 if nothing was flagged at all — vacuously, no false positives).
    `fp_flagged` lists FALSE-POSITIVE sample names that were wrongly flagged
    (the #391 guard: this MUST be empty)."""

    results: tuple[SampleResult, ...]
    per_class_recall: dict[str, float]
    precision: float
    fp_flagged: tuple[str, ...]

    @property
    def overall_recall(self) -> float:
        """TP samples flagged / TP samples (1.0 if there are no TP samples)."""
        tps = [r for r in self.results if r.is_true_positive]
        if not tps:
            return 1.0
        return sum(1 for r in tps if r.flagged) / len(tps)


def score(
    samples: tuple[CorpusSample, ...],
    findings_by_sample: dict[str, int],
) -> BenchmarkReport:
    """Score a run. `findings_by_sample` maps a sample NAME -> count of findings
    the runner returned for that sample (0 if absent). A sample is flagged iff
    that count > 0.

    Pure: no I/O, no clock, no randomness — same inputs, same report."""
    results = tuple(
        SampleResult(
            name=s.name,
            vuln_class=s.vuln_class,
            is_true_positive=s.is_true_positive,
            flagged=findings_by_sample.get(s.name, 0) > 0,
        )
        for s in samples
    )

    tp_by_class: dict[str, list[SampleResult]] = {}
    for r in results:
        if r.is_true_positive:
            tp_by_class.setdefault(r.vuln_class, []).append(r)
    per_class_recall = {
        cls: sum(1 for r in rs if r.flagged) / len(rs)
        for cls, rs in tp_by_class.items()
    }

    flagged = [r for r in results if r.flagged]
    flagged_tp = [r for r in flagged if r.is_true_positive]
    # Precision is vacuously 1.0 when nothing was flagged: no false positives
    # were emitted. (Recall, scored separately, is what catches "flagged
    # nothing" as a failure.)
    precision = (len(flagged_tp) / len(flagged)) if flagged else 1.0

    fp_flagged = tuple(r.name for r in flagged if not r.is_true_positive)

    return BenchmarkReport(
        results=results,
        per_class_recall=per_class_recall,
        precision=precision,
        fp_flagged=fp_flagged,
    )


@dataclass(frozen=True, slots=True)
class Regression:
    """A per-class recall DROP vs the baseline, or a NEW false positive. These
    are the conditions the regression-check mode fails on."""

    kind: str  # "recall_drop" | "new_false_positive"
    subject: str  # the vuln class (recall_drop) or sample name (new FP)
    baseline: float | None
    current: float | None


def compare_to_baseline(
    report: BenchmarkReport, baseline: dict
) -> tuple[Regression, ...]:
    """Return regressions of `report` vs a committed `baseline` dict (the shape
    `to_baseline_dict` writes). A regression is: (a) a class whose recall fell
    below its baseline recall, or (b) a false-positive sample flagged now that
    the baseline did not flag. Improvements (higher recall, fewer FPs) are NOT
    regressions. An UNKNOWN class (not in the baseline) is not a regression —
    new corpus classes raise the bar without breaking the gate; re-record to
    capture them."""
    regressions: list[Regression] = []

    base_recall: dict = baseline.get("per_class_recall", {})
    for cls, cur in report.per_class_recall.items():
        if cls in base_recall and cur < base_recall[cls]:
            regressions.append(
                Regression("recall_drop", cls, base_recall[cls], cur)
            )

    base_fp = set(baseline.get("fp_flagged", []))
    for name in report.fp_flagged:
        if name not in base_fp:
            regressions.append(
                Regression("new_false_positive", name, None, None)
            )

    return tuple(regressions)


def to_baseline_dict(report: BenchmarkReport, *, backend: str) -> dict:
    """Serialize a report to the committed-baseline JSON shape. `backend` names
    which backend produced it (the baseline file holds one entry per backend)."""
    return {
        "backend": backend,
        "overall_recall": report.overall_recall,
        "precision": report.precision,
        "per_class_recall": dict(sorted(report.per_class_recall.items())),
        "fp_flagged": list(report.fp_flagged),
    }
