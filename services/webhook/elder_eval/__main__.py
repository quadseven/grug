"""Elder replay eval CLI (#361 slice 2, #537).

    python -m elder_eval                     # replay + print report
    python -m elder_eval --record            # ... and update baseline.json
                                             # (same prompt: merges other backends'
                                             # entries; changed prompt: drops them)
    python -m elder_eval --check             # ... and exit 1 on regression vs baseline
    python -m elder_eval --ab-practices      # also measure the #527 practices delta

Corpus source: `--repo <owner/name>` reads the INGESTED store rows (needs
the DB env; the in-cluster path), else `--jsonl <path>` parses the
committed corpus via the slice-1 `ledger.parse_jsonl` layer (default:
logs/review-ledger.jsonl at the repo root). Both flow through slice-1
types - there is no third parser.

All modes make REAL backend calls (`sast_benchmark.backends`
GRUG_BENCH_* env) and REAL GitHub diff fetches - never run in the per-PR
CI suite. The per-PR suite exercises only the pure core + the prompt-sha
gate (test_elder_eval.py). The baseline records the STATIC prompt run
(no practices block) so it is stable across repos; `--ab-practices`
prints the ON-vs-OFF delta separately - the #527 measurement.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from ledger import parse_jsonl

from .corpus import build_cases, rows_from_store
from .gate import (
    BASELINE_PATH,
    compute_prompt_sha,
    load_baseline,
    merge_baseline,
)
from .runner import run_eval
from .scoring import EvalReport, compare_to_baseline, score, to_baseline_dict

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_JSONL = _REPO_ROOT / "logs" / "review-ledger.jsonl"


def _print_report(name: str, report: EvalReport) -> None:
    print(f"\n=== backend: {name} ===")
    print(
        f"  overall catch: {report.overall_catch:.2f}   "
        f"noise: {report.noise_rate:.2f}   "
        f"cases scored: {report.cases_scored}"
    )
    for cls in sorted(report.per_class_catch):
        print(f"    {cls:28s} catch={report.per_class_catch[cls]:.2f}")
    if report.errored_cases:
        print(f"  !! errored (not scored): {', '.join(report.errored_cases)}")
    if report.truncated_cases:
        print(
            "  !! diff hunk-bounded (misses may be amputation, not Elder): "
            f"{', '.join(report.truncated_cases)}"
        )
    if report.out_of_taxonomy:
        oot = ", ".join(f"{c}x{n}" for c, n in sorted(report.out_of_taxonomy.items()))
        print(f"  out-of-taxonomy (excluded, not misses): {oot}")
    if report.unknown_verdicts:
        uv = ", ".join(f"{v}x{n}" for v, n in sorted(report.unknown_verdicts.items()))
        print(f"  !! unknown verdicts (excluded - fix the corpus labels): {uv}")



def _run_ab_arm(
    backend, all_cases, cases, token, baseline_report, *, label, block, kwarg
) -> None:
    """One ON-arm replay + delta print vs the baseline report. `kwarg` is
    the run_eval keyword carrying the block (team_practices / few_shot)."""
    on = score(
        all_cases, run_eval(backend, cases, token=token, **{kwarg: block})
    )
    _print_report(f"{backend.name} + {label}", on)
    print(
        f"\n{label} delta: catch "
        f"{baseline_report.overall_catch:.2f} -> {on.overall_catch:.2f} "
        f"({on.overall_catch - baseline_report.overall_catch:+.2f}), noise "
        f"{baseline_report.noise_rate:.2f} -> {on.noise_rate:.2f} "
        f"({on.noise_rate - baseline_report.noise_rate:+.2f})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="elder_eval")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--repo", help="read the ingested store corpus for this repo")
    src.add_argument(
        "--jsonl",
        default=str(_DEFAULT_JSONL),
        help="committed-ledger path (slice-1 parse_jsonl)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="write baseline.json")
    mode.add_argument(
        "--check", action="store_true", help="exit 1 on regression vs baseline"
    )
    parser.add_argument("--backend", help="run only this configured backend")
    parser.add_argument(
        "--ab-practices",
        action="store_true",
        help="also replay WITH the #527 practices block and print the delta",
    )
    parser.add_argument(
        "--ab-few-shot",
        action="store_true",
        help="also replay WITH the #538 few-shot EXAMPLES and print the delta",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.repo:
        rows = rows_from_store(args.repo)
    else:
        rows = parse_jsonl(Path(args.jsonl).read_text())
    all_cases = build_cases(rows)
    cases = tuple(c for c in all_cases if c.scorable)
    skipped = len(all_cases) - len(cases)
    if skipped:
        # Unscorable cases (nothing accepted or FP'd inside Elder's
        # taxonomy) are skipped LLM calls - skip loudly, never silently.
        print(
            f"skipping {skipped} unscorable case(s) "
            "(no accepted/false-positive rows in Elder's taxonomy)"
        )
    if not cases:
        print("corpus has no scorable cases - nothing to eval", file=sys.stderr)
        return 2

    from sast_benchmark.backends import configured_backends

    backends = [
        b for b in configured_backends()
        if not args.backend or b.name == args.backend
    ]
    if not backends:
        print(
            "No bench backend configured/matched. Set GRUG_BENCH_"
            "{OPENROUTER,POOLSIDE}_KEY and/or GRUG_BENCH_CAVE_URL+MODEL.",
            file=sys.stderr,
        )
        return 2
    backend = backends[0]
    skipped_backends = [b.name for b in backends[1:]]
    if skipped_backends:
        print(
            f"running backend {backend.name!r}; skipping configured "
            f"{', '.join(skipped_backends)} (use --backend to select)"
        )
    token = os.getenv("GITHUB_TOKEN", "")

    # Score over ALL cases: unscorable ones contribute their excluded-row
    # tallies to the report without being replayed or read as errored.
    replays = run_eval(backend, cases, token=token)
    report = score(all_cases, replays)
    _print_report(backend.name, report)

    if report.all_errored:
        print(
            "every case errored - refusing to treat a broken run as a result",
            file=sys.stderr,
        )
        return 3

    if args.ab_practices:
        from best_practices import derive_practices, practices_block

        block = practices_block(derive_practices(list(rows)))
    else:
        block = ""
    if args.ab_practices and not block:
        # An empty block makes the ON arm byte-identical to baseline -
        # the "delta" would be pure sampling noise dressed as a result.
        print(
            "#527 practices: no practices derivable from this corpus - "
            "skipping ON arm (delta would be A/A noise)"
        )
    if block:
        _run_ab_arm(
            backend, all_cases, cases, token, report,
            label="practices (#527)", block=block, kwarg="team_practices",
        )

    if args.ab_few_shot:
        from few_shot import exemplars_block, exemplars_from_rows
        from ledger import accepted_findings_by_class

        examples = exemplars_block(
            exemplars_from_rows(accepted_findings_by_class(list(rows)))
        )
    else:
        examples = ""
    if args.ab_few_shot and not examples:
        print(
            "#538 few-shot: no exemplars derivable from this corpus "
            "(0 accepted findings) - skipping ON arm (delta would be "
            "A/A noise)"
        )
    if examples:
        _run_ab_arm(
            backend, all_cases, cases, token, report,
            label="few-shot (#538)", block=examples, kwarg="few_shot",
        )

    if args.record:
        if report.errored_cases:
            # A partial run must never become the committed ruler: an
            # errored case would permanently shrink cases_scored and the
            # missing slice of the corpus would silently stop gating
            # Elder (codex+poolside peer review, PR #543).
            print(
                "refusing --record: "
                f"{len(report.errored_cases)} case(s) errored "
                f"({', '.join(report.errored_cases)}) - a baseline must "
                "come from a complete run; fix or prune the cases and re-run",
                file=sys.stderr,
            )
            return 3
        fresh = to_baseline_dict(
            report, prompt_sha=compute_prompt_sha(), backend=backend.name
        )
        if BASELINE_PATH.exists():
            fresh, dropped = merge_baseline(load_baseline(), fresh)
            if dropped:
                print(
                    "prompt changed since the last record - dropping "
                    f"stale backend baseline(s): {', '.join(dropped)} "
                    "(re-record them against the new prompt)"
                )
        BASELINE_PATH.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
        print(f"baseline recorded -> {BASELINE_PATH}")
        return 0

    if args.check:
        if not BASELINE_PATH.exists():
            print("no baseline.json to check against - record one first",
                  file=sys.stderr)
            return 2
        baseline = load_baseline()
        backend_baseline = baseline.get("backends", {}).get(backend.name)
        if backend_baseline is None:
            print(f"baseline has no entry for backend {backend.name!r}",
                  file=sys.stderr)
            return 2
        regressions = compare_to_baseline(report, backend_baseline)
        if regressions:
            print("\nREGRESSIONS vs baseline:")
            for r in regressions:
                print(f"  - {r}")
            return 1
        print("\nno regression vs baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
