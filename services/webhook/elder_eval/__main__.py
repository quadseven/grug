"""Elder replay eval CLI (#361 slice 2, #537).

    python -m elder_eval                     # replay + print report
    python -m elder_eval --record            # ... and update baseline.json
                                             # (same prompt: merges other backends'
                                             # entries; changed prompt: drops them)
    python -m elder_eval --check             # ... and exit 1 on regression vs baseline
    python -m elder_eval --ab-practices      # also measure the #527 practices delta
    python -m elder_eval --ab-few-shot       # also measure the #538 few-shot delta
    python -m elder_eval --production         # shipped staged discovery
    python -m elder_eval --production --published
                                             # findings surviving publication gates

Corpus source: `--repo <owner/name>` reads the INGESTED store rows (needs
the DB env; the in-cluster path), else `--jsonl <path>` parses the
committed corpus via the slice-1 `ledger.parse_jsonl` layer (default:
logs/review-ledger.jsonl at the repo root). Both flow through slice-1
types - there is no third parser.

All modes make REAL backend calls (`sast_benchmark.backends`
GRUG_BENCH_* env) and REAL GitHub diff fetches - never run in the per-PR
CI suite. The per-PR suite exercises only the pure core + the prompt-sha
gate (test_elder_eval.py). The baseline records the STATIC prompt run
(no derived blocks - neither practices nor few-shot) so it is stable
across repos; `--ab-practices` and
`--ab-few-shot` print their ON-vs-OFF deltas separately - the #527 and
#538 measurements. An empty derived block skips its ON arm loudly (an
A/A replay printed as a delta would be a fabricated result).

WITHOUT `--production`, this measures ONE monolithic backend call
(`sast_benchmark.backends`) - never `review_diff` - so it skips the
cohort planner, the Cave coder/reasoner arms, the judge/verify/refute
gates, and the publication gates that decide what actually posts. Every
printed report says so (`_methodology_note`) precisely because a bare
"catch rate" with no such label is how a bench-mode number (e.g. the 0.16
quoted in PR #846) gets read as an Elder number.

`--production` drives the SHIPPED `review_diff` pipeline instead, so its
number reflects what the deployed reviewer actually does. It needs the
Cave gateway (GRUG_CAVE_GATEWAY_URL, an in-cluster address - port-forward
it for a local run, see docs/RUNBOOK.md) reachable. Without network
access to Cave, every case's `review()` call raises or times out and the
run reports `all_errored`, never a fabricated score - that is the
honest-failure behavior working as designed, not a bug.
`benchmark.elder-eval.yml`'s `workflow_dispatch` inputs do not currently
offer a production mode (`mode` is record/check/report against the bench
backends only) - wiring that dispatch path is still open.

WHICH BACKEND A `--production` RUN MEASURES IS NOT THE ONE YOU SET
(grug#916). Under `GRUG_REVIEW_BACKEND_PRIORITY=cloud` the grug#910
chain degrades WITHOUT raising: a tier-1 timeout is caught, logged, and
control falls through to the Cave arms, so the case still returns
`kind="reviewed"` and still scores. Two `--production` runs differing
only in that env var can therefore report near-identical numbers because
BOTH were served by Cave. Every report prints
`backend(s) that actually answered` from the response's own attribution,
and says so loudly when a cloud-priority run was served entirely by the
fallback - read that line before comparing two runs.

GITHUB_TOKEN IS REQUIRED (grug#916). The corpus costs ~96 GitHub API
requests for 18 cases (anchored cases fetch base-sha + fix-commit-parent
+ compare, and a repo referenced under a pre-rename name pays a
quota-spending 301 on each), against an anonymous cap of 60/hr. A
tokenless run scores the prefix that fits and 403s the rest, printing a
catch rate that reads like a backend result and is a rate-limit
artifact - which is exactly what happened on 2026-08-28. `--allow-anonymous`
overrides deliberately.
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
from .runner import run_eval, run_production_eval
from .scoring import EvalReport, compare_to_baseline, score, to_baseline_dict

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_JSONL = _REPO_ROOT / "logs" / "review-ledger.jsonl"

# grug#916: every case costs SEVERAL GitHub API calls, not one. An
# anchored case (#545) pays base-sha + fix-commit-parent + compare on top
# of the diff fetch, and a corpus naming a repo under its PRE-RENAME name
# pays a 301 on EVERY one of those, each of which spends quota too. The
# committed corpus measured 96 requests for 18 cases (47 of them pure
# redirects) - against an anonymous budget of 60/hr. So a tokenless run
# does not fail cleanly: it scores the handful of cases that fit, errors
# the rest on 403, and reports a catch rate computed over whichever
# prefix of the corpus happened to fit in the budget. That is worse than
# no number, because it LOOKS like a result: the 2026-08-28 run that
# opened grug#916 reported "10 of 18 errored, catch 0.19" and was read as
# a signal about the backend under test, which had not been called once
# for those 10 cases.
_ANONYMOUS_GITHUB_ERROR = """GITHUB_TOKEN is not set - refusing to run.

This corpus has {cases} case(s) and each one costs several GitHub API
requests (diff + anchor resolution, plus a redirect apiece for any repo
referenced under a previous name). Unauthenticated calls are capped at
60/hr, so an anonymous run exhausts the budget partway through and
reports a catch rate over whatever prefix of the corpus fit - which
reads as a backend result but is a rate-limit artifact.

Fix:  export GITHUB_TOKEN="$(gh auth token)"
Or:   pass --allow-anonymous to accept the above and run anyway."""


def _methodology_note(name: str) -> str:
    """A one-line self-description of what `name`'s number actually
    measures. The reported "overall catch 0.16" that circulated as if it
    were Elder's number came from the DEFAULT bench mode - one monolithic
    backend call that never touches `review_diff` - printed with no such
    label. Whatever this harness prints must say which pipeline produced
    it."""
    if name.startswith("production"):
        gates = "cohort planner, Cave coder+reasoner arms, judge/verify/refute"
        if "published" in name:
            gates += ", publication gates"
        return (
            f"  methodology: shipped review_diff pipeline ({gates}) - "
            "requires the private Cave gateway to be reachable; this is "
            "what actually posts to a PR"
        )
    return (
        "  methodology: ONE monolithic backend call via "
        "sast_benchmark.backends - bypasses the deployed pipeline (cohort "
        "planner, Cave coder+reasoner arms, judge/verify/refute, "
        "publication gates); NOT comparable to a --production run"
    )


def _cloud_chain_backend_names() -> set[str]:
    """Backend names the grug#910 cloud chain can be served BY (grug#916).

    Read from `_cloud_chain_tiers()` rather than restated here, for the
    same reason `review_cohort_char_budget` is exported rather than
    duplicated: a chain that gains or loses a tier must not leave this
    check silently asserting yesterday's roster."""
    from llm_client import _cloud_chain_tiers

    return {tier.backend.value for tier in _cloud_chain_tiers()}


def _print_report(name: str, report: EvalReport) -> None:
    print(f"\n=== backend: {name} ===")
    print(_methodology_note(name))
    print(
        f"  overall catch: {report.overall_catch:.2f}   "
        f"noise: {report.noise_rate:.2f}   "
        f"cases scored: {report.cases_scored}"
    )
    if report.cases_scored:
        unanchored = report.cases_scored - len(report.anchored_cases)
        print(
            f"  anchored to pre-fix snapshot (#545): {len(report.anchored_cases)}/"
            f"{report.cases_scored} scored cases; {unanchored} replayed the "
            "PR's FINAL merged diff instead (KNOWN METHODOLOGY BIAS - "
            "see specs/DESIGN.md)"
        )
    for cls in sorted(report.per_class_catch):
        print(f"    {cls:28s} catch={report.per_class_catch[cls]:.2f}")
    if report.backend_attribution:
        served = ", ".join(
            f"{backend}x{n}"
            for backend, n in sorted(report.backend_attribution.items())
        )
        print(f"  backend(s) that actually answered: {served}")
        configured = os.getenv("GRUG_REVIEW_BACKEND_PRIORITY", "cave").strip().lower()
        if configured == "cloud" and not (
            report.backend_attribution.keys() & _cloud_chain_backend_names()
        ):
            print(
                "  !! GRUG_REVIEW_BACKEND_PRIORITY=cloud but NO case was "
                "answered by the cloud chain - every case fell through to "
                "the fallback. This number measures the FALLBACK, not the "
                "cloud backend you configured."
            )
    if report.errored_cases:
        print(f"  !! errored (not scored): {', '.join(report.errored_cases)}")
    if report.truncated_cases:
        print(
            "  !! diff hunk-bounded (misses may be amputation, not Elder): "
            f"{', '.join(report.truncated_cases)}"
        )
    if report.staged_cases:
        print(
            "  staged (too big for one cohort call - reviewed via several "
            f"instead of a monolithic one, #859 follow-up): "
            f"{', '.join(report.staged_cases)}"
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


def _parse_args(
    argv: list[str] | None,
) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
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
        "--production",
        action="store_true",
        help="replay through shipped staged review_diff instead of one monolithic bench prompt",
    )
    parser.add_argument(
        "--published",
        action="store_true",
        help="with --production, score findings after diff-only publication gates",
    )
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        help=(
            "run without GITHUB_TOKEN, accepting the 60/hr anonymous cap "
            "(see the startup error for why this usually invalidates a run)"
        ),
    )
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
    return parser, parser.parse_args(argv)


def _load_corpus(args):
    rows = (
        rows_from_store(args.repo)
        if args.repo
        else parse_jsonl(Path(args.jsonl).read_text())
    )
    all_cases = build_cases(rows)
    cases = tuple(case for case in all_cases if case.scorable)
    skipped = len(all_cases) - len(cases)
    if skipped:
        print(
            f"skipping {skipped} unscorable case(s) "
            "(no accepted/false-positive rows in Elder's taxonomy)"
        )
    return rows, all_cases, cases


def _run_requested_mode(parser, args, cases, token):
    if args.published and not args.production:
        parser.error("--published requires --production")
    if args.production:
        if args.backend or args.ab_practices or args.ab_few_shot:
            parser.error(
                "--production cannot be combined with backend or prompt A/B modes"
            )
        name = "production-published" if args.published else "production-discovery"
        return (
            None,
            name,
            run_production_eval(cases, token=token, published=args.published),
        )

    from sast_benchmark.backends import configured_backends

    backends = [
        backend
        for backend in configured_backends()
        if not args.backend or backend.name == args.backend
    ]
    if not backends:
        print(
            "No bench backend configured/matched. Set GRUG_BENCH_"
            "{OPENROUTER,POOLSIDE}_KEY and/or GRUG_BENCH_CAVE_URL+MODEL.",
            file=sys.stderr,
        )
        return None, "", None
    backend = backends[0]
    if len(backends) > 1:
        print(
            f"running backend {backend.name!r}; skipping configured "
            f"{', '.join(item.name for item in backends[1:])} "
            "(use --backend to select)"
        )
    return backend, backend.name, run_eval(backend, cases, token=token)


def _run_ab_modes(args, rows, all_cases, cases, token, report, backend) -> None:
    block = ""
    if args.ab_practices:
        from best_practices import derive_practices, practices_block

        block = practices_block(derive_practices(list(rows)))
        if not block:
            print(
                "#527 practices: no practices derivable from this corpus - "
                "skipping ON arm (delta would be A/A noise)"
            )
    if block:
        _run_ab_arm(
            backend,
            all_cases,
            cases,
            token,
            report,
            label="practices (#527)",
            block=block,
            kwarg="team_practices",
        )

    examples = ""
    if args.ab_few_shot:
        from few_shot import exemplars_block, exemplars_from_rows
        from ledger import accepted_findings_by_class

        examples = exemplars_block(
            exemplars_from_rows(accepted_findings_by_class(list(rows)))
        )
        if not examples:
            print(
                "#538 few-shot: no exemplars derivable from this corpus "
                "(0 accepted findings) - skipping ON arm (delta would be "
                "A/A noise)"
            )
    if examples:
        _run_ab_arm(
            backend,
            all_cases,
            cases,
            token,
            report,
            label="few-shot (#538)",
            block=examples,
            kwarg="few_shot",
        )


def _record_report(report: EvalReport, backend_name: str) -> int:
    if report.errored_cases:
        print(
            "refusing --record: "
            f"{len(report.errored_cases)} case(s) errored "
            f"({', '.join(report.errored_cases)}) - a baseline must "
            "come from a complete run; fix or prune the cases and re-run",
            file=sys.stderr,
        )
        return 3
    fresh = to_baseline_dict(
        report, prompt_sha=compute_prompt_sha(), backend=backend_name
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


def _check_report(report: EvalReport, backend_name: str) -> int:
    if not BASELINE_PATH.exists():
        print("no baseline.json to check against - record one first", file=sys.stderr)
        return 2
    backend_baseline = load_baseline().get("backends", {}).get(backend_name)
    if backend_baseline is None:
        print(f"baseline has no entry for backend {backend_name!r}", file=sys.stderr)
        return 2
    regressions = compare_to_baseline(report, backend_baseline)
    if regressions:
        print("\nREGRESSIONS vs baseline:")
        for regression in regressions:
            print(f"  - {regression}")
        return 1
    print("\nno regression vs baseline")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser, args = _parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rows, all_cases, cases = _load_corpus(args)
    if not cases:
        print("corpus has no scorable cases - nothing to eval", file=sys.stderr)
        return 2

    token = os.getenv("GITHUB_TOKEN", "")
    if not token and not args.allow_anonymous:
        print(
            _ANONYMOUS_GITHUB_ERROR.format(cases=len(cases)), file=sys.stderr
        )
        return 2

    backend, backend_name, replays = _run_requested_mode(parser, args, cases, token)
    if replays is None:
        return 2

    report = score(all_cases, replays)
    _print_report(backend_name, report)

    if report.all_errored:
        print(
            "every case errored - refusing to treat a broken run as a result",
            file=sys.stderr,
        )
        return 3

    if backend is not None:
        _run_ab_modes(args, rows, all_cases, cases, token, report, backend)

    if args.record:
        return _record_report(report, backend_name)

    if args.check:
        return _check_report(report, backend_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
