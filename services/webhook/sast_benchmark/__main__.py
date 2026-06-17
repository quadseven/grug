"""SAST benchmark CLI (#399, ADR-0006).

    python -m sast_benchmark              # run configured backends, print report
    python -m sast_benchmark --record    # ... and (over)write baseline.json
    python -m sast_benchmark --check      # ... and exit 1 on a regression vs baseline

`--record`/`--check`/default all make REAL backend calls (whichever backends
`configured_backends()` finds in the env). They are NOT run in the per-PR CI
suite — only from the on-demand `benchmark.sast.yml` job or a manual run with
the free-key (+ tailnet, for sparkles) env present. The pure scoring core is
what CI exercises (test_sast_benchmark.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .backends import configured_backends
from .corpus import load_corpus
from .runner import run_backend
from .scoring import compare_to_baseline, score, to_baseline_dict

_BASELINE_PATH = os.path.join(os.path.dirname(__file__), "baseline.json")


def _print_report(name: str, report) -> None:
    print(f"\n=== backend: {name} ===")
    print(f"  overall recall: {report.overall_recall:.2f}   precision: {report.precision:.2f}")
    for cls in sorted(report.per_class_recall):
        print(f"    {cls:28s} recall={report.per_class_recall[cls]:.2f}")
    if report.fp_flagged:
        print(f"  !! FALSE POSITIVES flagged (precision miss): {', '.join(report.fp_flagged)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sast_benchmark")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="write baseline.json")
    mode.add_argument("--check", action="store_true", help="exit 1 on regression vs baseline")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    corpus = load_corpus()
    backends = configured_backends()
    if not backends:
        print(
            "No backends configured. Set GRUG_BENCH_{OPENROUTER,POOLSIDE}_KEY "
            "and/or GRUG_BENCH_CAVE_URL+MODEL (sparkles).",
            file=sys.stderr,
        )
        return 2

    reports = {}
    broken = []
    for backend in backends:
        run = run_backend(backend, corpus)
        if run.all_errored:
            # Every call failed (bad key/URL/unreachable Cave). Recording its
            # all-zero recall as a baseline would be a fabricated "Elder detects
            # nothing" — refuse it instead.
            print(
                f"\n!! backend {backend.name}: ALL {run.total} samples errored "
                "- not a valid run (check key/URL/reachability); skipping.",
                file=sys.stderr,
            )
            broken.append(backend.name)
            continue
        reports[backend.name] = score(corpus, run.findings_by_sample)
        _print_report(backend.name, reports[backend.name])

    if not reports:
        print("\nNo backend produced a valid run.", file=sys.stderr)
        return 2

    if args.record:
        out = {name: to_baseline_dict(rep, backend=name) for name, rep in reports.items()}
        with open(_BASELINE_PATH, "w") as f:
            json.dump({"backends": out}, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"\nbaseline written: {_BASELINE_PATH} ({len(out)} backend(s))")
        return 0

    if args.check:
        with open(_BASELINE_PATH) as f:
            baseline = json.load(f).get("backends", {})
        regressions = []
        for name, rep in reports.items():
            base = baseline.get(name)
            if base is None:
                print(f"  (no baseline for {name} — skipping regression check)")
                continue
            regressions += [(name, r) for r in compare_to_baseline(rep, base)]
        if regressions:
            print("\nREGRESSIONS:")
            for name, r in regressions:
                print(f"  [{name}] {r.kind}: {r.subject} (baseline={r.baseline} current={r.current})")
            return 1
        print("\nno regressions vs baseline")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
