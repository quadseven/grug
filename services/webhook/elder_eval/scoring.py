"""Pure scoring for the Elder replay eval (#361 slice 2, #537).

NO LLM, NO I/O, fully deterministic - the CI-safe core, unit-tested with
synthetic ledger rows + replays. The live runner produces `CaseReplay`s;
this module turns them into per-class catch-rate + noise-rate and
compares against the committed baseline.

Metrics:
- CATCH (per ledger class, over non-errored cases that expect it): the
  replay emitted >= 1 finding in any of the class's bridged Elder classes.
- OVERALL CATCH (micro): caught (case, class) cells / expected cells.
- NOISE: replay findings landing on (case, class) cells the corpus knows
  ONLY as false positives / all replay findings. 0.0 when nothing was
  emitted (vacuously noise-free, mirroring the SAST precision convention).
- HONEST-ZERO RULE: an errored case is EXCLUDED from every denominator
  and listed in `errored_cases` - a non-run is not a miss, and an
  all-errored sweep must never record as a valid (zero) baseline.
  Excluded from RATES, but not from the GATE: `compare_to_baseline`
  flags errored cases and `cases_scored` shrinkage as regressions (a
  partial run must not pass as complete).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

from .corpus import EvalCase


@dataclass(frozen=True)
class CaseReplay:
    """One case's replay outcome: ELDER-normalized class -> finding count.
    `errored` = the replay could not run (fetch/transport/parse failure) -
    distinguishable from "Elder found nothing" ({} with errored=False)."""

    case_id: str
    emitted: Mapping[str, int]
    errored: bool


@dataclass(frozen=True)
class EvalReport:
    """Scored outcome over the corpus. `per_class_catch` keys are LEDGER
    classes (the ground-truth vocabulary). Empty when every case errored."""

    per_class_catch: dict[str, float]
    overall_catch: float
    noise_rate: float
    errored_cases: tuple[str, ...]
    out_of_taxonomy: dict[str, int]
    unknown_verdicts: dict[str, int]
    cases_scored: int

    @property
    def all_errored(self) -> bool:
        """True when NOTHING scored - the run is broken, not a result."""
        return self.cases_scored == 0 and bool(self.errored_cases)


def score(
    cases: Sequence[EvalCase], replays: Mapping[str, CaseReplay]
) -> EvalReport:
    """Join cases with their replays and compute the report. A case with no
    replay entry counts as errored (it did not run)."""
    expected_cells: Counter[str] = Counter()
    caught_cells: Counter[str] = Counter()
    noise = 0
    total_emitted = 0
    errored: list[str] = []
    out_of_taxonomy: Counter[str] = Counter()
    unknown_verdicts: Counter[str] = Counter()
    scored = 0

    # An orphan replay (no matching case) would silently vanish from every
    # metric - surface it loudly, it means the join key drifted.
    orphans = set(replays) - {c.case_id for c in cases}
    if orphans:
        raise ValueError(f"replays reference unknown cases: {sorted(orphans)}")

    for case in cases:
        out_of_taxonomy.update(case.out_of_taxonomy)
        unknown_verdicts.update(case.unknown_verdicts)
        replay = replays.get(case.case_id)
        if replay is None or replay.errored:
            errored.append(case.case_id)
            continue
        scored += 1
        emitted_classes = {c for c, n in replay.emitted.items() if n > 0}
        for ledger_cls, elder_set in case.expected_classes.items():
            expected_cells[ledger_cls] += 1
            if emitted_classes & elder_set:
                caught_cells[ledger_cls] += 1
        for elder_cls, n in replay.emitted.items():
            total_emitted += n
            if elder_cls in case.fp_only_classes:
                noise += n

    per_class = {
        cls: caught_cells[cls] / expected_cells[cls] for cls in expected_cells
    }
    total_expected = sum(expected_cells.values())
    return EvalReport(
        per_class_catch=per_class,
        overall_catch=(
            sum(caught_cells.values()) / total_expected if total_expected else 0.0
        ),
        noise_rate=noise / total_emitted if total_emitted else 0.0,
        errored_cases=tuple(errored),
        out_of_taxonomy=dict(out_of_taxonomy),
        unknown_verdicts=dict(unknown_verdicts),
        cases_scored=scored,
    )


def to_baseline_dict(report: EvalReport, *, prompt_sha: str, backend: str) -> dict:
    """Serialize one backend's report as the committed-baseline shape. The
    top level carries `prompt_sha` - the #537 CI gate key."""
    return {
        "prompt_sha": prompt_sha,
        "backends": {
            backend: {
                "overall_catch": report.overall_catch,
                "per_class_catch": dict(sorted(report.per_class_catch.items())),
                "noise_rate": report.noise_rate,
                "cases_scored": report.cases_scored,
                "errored_cases": sorted(report.errored_cases),
            }
        },
    }


def compare_to_baseline(
    report: EvalReport,
    backend_baseline: dict,
    *,
    catch_tolerance: float = 0.05,
    noise_tolerance: float = 0.05,
) -> list[str]:
    """Regressions of `report` vs one backend's recorded scores. Empty list
    = no regression. Only classes PRESENT in the baseline are compared (a
    corpus can grow new classes without failing the check); a baseline
    class missing from the new report IS a regression (coverage lost).

    COVERAGE LOSS IS A REGRESSION: a run that errored cases or scored
    fewer cases than the baseline compares rates over a smaller corpus -
    a partial result must never read as a complete pass."""
    regressions: list[str] = []
    if report.errored_cases:
        regressions.append(
            f"{len(report.errored_cases)} case(s) errored (not scored): "
            f"{', '.join(report.errored_cases)}"
        )
    base_cases = int(backend_baseline.get("cases_scored", 0))
    if report.cases_scored < base_cases:
        regressions.append(
            f"cases_scored shrank {base_cases} -> {report.cases_scored} "
            "(coverage loss - rates are not comparable)"
        )
    base_overall = float(backend_baseline.get("overall_catch", 0.0))
    if report.overall_catch < base_overall - catch_tolerance:
        regressions.append(
            f"overall_catch regressed {base_overall:.2f} -> "
            f"{report.overall_catch:.2f}"
        )
    base_noise = float(backend_baseline.get("noise_rate", 0.0))
    if report.noise_rate > base_noise + noise_tolerance:
        regressions.append(
            f"noise_rate regressed {base_noise:.2f} -> {report.noise_rate:.2f}"
        )
    for cls, base_catch in backend_baseline.get("per_class_catch", {}).items():
        new = report.per_class_catch.get(cls)
        if new is None:
            regressions.append(f"class {cls} vanished from the report")
        elif new < float(base_catch) - catch_tolerance:
            regressions.append(
                f"class {cls} catch regressed {float(base_catch):.2f} -> {new:.2f}"
            )
    return regressions
