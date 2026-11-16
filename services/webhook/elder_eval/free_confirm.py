"""Measure whether a free OpenCode Go model's empty answer can be trusted.

    python -m elder_eval.free_confirm                 # every free model now
    python -m elder_eval.free_confirm --models a,b    # named models only
    python -m elder_eval.free_confirm --limit 6       # a smoke run, not a report

For each free model the run asks Elder's own review prompt about each case in
`free_confirm_corpus` (half defect, half clean, each padded to the size the
relaxation applies to) through the same code path a review uses
(`llm_client._run_review_arm`), paced by the provider limits Elder already
implements (`opencode_go_free.FreeCallGate`: Retry-After and cool-downs are
waited out, never skipped). It then writes `services/_shared/
opencode_go_free_eval.json`, which `opencode_go_free.qualified_models` reads.

Two rates matter. FALSE-EMPTY is a defect case answered with no findings at
all: the failure that would let a free model wave a bug through as the second
opinion. FALSE-FINDING is a clean case answered with at least one finding. A
call that errors (timeout, 429 that outlasts the retries, unparseable answer)
is counted as an error and left out of both rates, never read as an empty
answer.

The key comes from GRUG_BENCH_OPENCODE_GO_KEY; the corpus is invented and
public, so the free models are asked about it whatever their data terms.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from . import free_confirm_corpus as corpus

log = logging.getLogger("grug.elder_eval.free_confirm")

REPORT_SCHEMA = 1
_PROMPT_VARIANT = "v2"
ATTEMPTS = 3
PAUSE_BETWEEN_CALLS_SECONDS = 2.0
MAX_WAIT_SECONDS = 180.0

# What one case came to.
EMPTY = "empty"  # no findings at all
CAUGHT = "caught"  # a defect case with a finding on the defective file
OFF_TARGET = "off_target"  # a defect case with findings, none on the defective file
FINDING = "finding"  # a clean case with at least one finding
CLEAN = "clean"  # a clean case with no findings
ERROR = "error"  # no usable answer; counted, never scored


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    defective: bool
    label: str
    detail: str = ""


def label_answer(case: corpus.FreeConfirmCase, finding_paths: list[str] | None) -> str:
    """Name the answer to one case. `finding_paths` is None for an error and
    otherwise the file path of each finding the model gave."""
    if finding_paths is None:
        return ERROR
    if not finding_paths:
        return EMPTY if case.defective else CLEAN
    if not case.defective:
        return FINDING
    return CAUGHT if case.path in finding_paths else OFF_TARGET


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    """Counts and rates for one model's results."""
    defect = [r for r in results if r.defective and r.label != ERROR]
    clean = [r for r in results if not r.defective and r.label != ERROR]
    false_empty = sum(r.label == EMPTY for r in defect)
    false_finding = sum(r.label == FINDING for r in clean)

    def rate(part: int, whole: int) -> float | None:
        return round(part / whole, 4) if whole else None

    return {
        "defect_total": sum(r.defective for r in results),
        "defect_scored": len(defect),
        "false_empty": false_empty,
        "false_empty_rate": rate(false_empty, len(defect)),
        "caught": sum(r.label == CAUGHT for r in defect),
        "off_target": sum(r.label == OFF_TARGET for r in defect),
        "clean_total": sum(not r.defective for r in results),
        "clean_scored": len(clean),
        "false_finding": false_finding,
        "false_finding_rate": rate(false_finding, len(clean)),
        "errors": sum(r.label == ERROR for r in results),
        "cases": {r.case_id: r.label for r in results},
    }


def build_report(
    per_model: dict[str, list[CaseResult]], *, when: date, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The checked-in report. `allowlist` is a convenience copy: the runtime
    reads the counts, so editing the list by hand changes nothing."""
    import opencode_go_free

    models = {m: summarize(rs) for m, rs in sorted(per_model.items())}
    cases = corpus.build_cases()
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "date": when.isoformat(),
        "provider": "opencode-go",
        "what": (
            "False-empty and false-finding rates of OpenCode Go's free models on a "
            "fixed set of small diffs, for grug's rule that a free model's empty "
            "answer may confirm a paid model's empty answer."
        ),
        "prompt_variant": _PROMPT_VARIANT,
        "corpus": {
            "sha": corpus.corpus_sha(),
            "cases": len(cases),
            "defect": sum(c.defective for c in cases),
            "clean": sum(not c.defective for c in cases),
            "languages": sorted({c.language for c in cases}),
        },
        "bar": {
            "min_defect_cases": opencode_go_free.MIN_DEFECT_CASES,
            "max_false_empty_rate_percent": opencode_go_free.MAX_FALSE_EMPTY_RATE_PERCENT,
        },
        "models": models,
        "allowlist": sorted(
            m for m, stats in models.items() if opencode_go_free.model_qualifies(stats)
        ),
    }
    if extra:
        report.update(extra)
    return report


# --- the live run ----------------------------------------------------------------


def _finding_paths(outcome: Any) -> list[str] | None:
    if outcome.kind != "success":
        return None
    return [f.path for f in outcome.findings]


def _is_permanent_refusal(detail: str) -> bool:
    """A 4xx other than 429 will answer the same way again (a model that does
    not accept the request body Elder sends), so retrying only spends calls."""
    status = detail.rsplit("http_", 1)[-1][:3]
    return "http_" in detail and status.isdigit() and status.startswith("4") and status != "429"


def ask_model(
    model: str,
    case: corpus.FreeConfirmCase,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> CaseResult:
    """One case to one model, as a review would ask it. Waits out a cool-down
    and retries an error up to `ATTEMPTS` times."""
    import llm_client as lc
    from review_evidence import assemble_evidence

    from .runner import diff_to_hunks

    hunks = diff_to_hunks(corpus.diff_for(case))
    messages = lc._build_messages(assemble_evidence(hunks), _PROMPT_VARIANT)
    tier = lc._opencode_go_extra_model_config(model, lc._CLOUD_CHAIN_TIMEOUT_SECONDS)
    gate = lc._opencode_go_free_gate_get()
    detail = ""
    for attempt in range(1, ATTEMPTS + 1):
        wait = max(gate.cooldown_until(model) - time.time(), 0.0)
        if wait > MAX_WAIT_SECONDS:
            # The provider asked for a longer pause than a run can spend (a
            # spent daily allowance sends Retry-After of many hours). Respect
            # it: no call is made, and the case is an error, not an answer.
            return CaseResult(
                case.case_id, case.defective, ERROR, f"rate limited for {round(wait)}s",
            )
        if wait > 0:
            log.info("free_confirm_waiting", extra={"model": model, "seconds": round(wait)})
            sleep(wait)
        # A failed call benches a model for a while; a measurement must not
        # read that bench as an answer.
        for state in (getattr(lc, "_bench_state", None), getattr(lc, "_slow_bench_state", None)):
            if isinstance(state, dict):
                state.clear()
        with lc._reviewed_diff_scope(hunks):
            outcome = lc._run_review_arm(
                lc.Backend.OPENCODE_GO, messages, _PROMPT_VARIANT, {}, None, config_override=tier,
            )
        paths = _finding_paths(outcome)
        if paths is not None:
            return CaseResult(case.case_id, case.defective, label_answer(case, paths))
        detail = f"{outcome.kind}: {(outcome.error_text or outcome.parse_err or '')[:160]}"
        log.warning(
            "free_confirm_call_failed",
            extra={"model": model, "case": case.case_id, "attempt": attempt, "why": detail},
        )
        if _is_permanent_refusal(detail):
            break
        sleep(PAUSE_BETWEEN_CALLS_SECONDS * attempt)
    return CaseResult(case.case_id, case.defective, ERROR, detail)


def current_free_models() -> list[str]:
    import llm_client as lc

    return lc._opencode_go_free_catalog_get().models()


def _load_state(path: Path | None, models: list[str]) -> dict[str, list[CaseResult]]:
    results: dict[str, list[CaseResult]] = {m: [] for m in models}
    if path is None or not path.exists():
        return results
    saved = json.loads(path.read_text(encoding="utf-8"))
    for model, rows in saved.items():
        if model in results:
            results[model] = [CaseResult(**row) for row in rows]
    return results


def _save_state(path: Path | None, results: dict[str, list[CaseResult]]) -> None:
    if path is not None:
        path.write_text(
            json.dumps({m: [asdict(r) for r in rs] for m, rs in results.items()}),
            encoding="utf-8", newline="\n",
        )


def run(
    models: list[str],
    cases: tuple[corpus.FreeConfirmCase, ...],
    *,
    state: Path | None = None,
    budget_seconds: float | None = None,
) -> tuple[dict[str, list[CaseResult]], bool]:
    """Ask every model every case. With a `state` file the run resumes where it
    stopped; with a `budget_seconds` it stops cleanly once the budget is spent.
    Returns the results and whether every case was asked."""
    results = _load_state(state, models)
    started = time.time()
    for case in cases:
        for model in models:
            if any(r.case_id == case.case_id for r in results[model]):
                continue
            if budget_seconds is not None and time.time() - started > budget_seconds:
                return results, False
            result = ask_model(model, case)
            results[model].append(result)
            _save_state(state, results)
            print(f"{model:28} {case.case_id:32} {result.label}", flush=True)
            time.sleep(PAUSE_BETWEEN_CALLS_SECONDS)
    return results, True


def render_table(report: dict[str, Any]) -> str:
    lines = [
        f"{'model':28} {'defect':>7} {'false-empty':>12} {'caught':>7} {'clean':>6} "
        f"{'false-find':>11} {'errors':>7}  qualifies",
    ]
    for model, s in report["models"].items():
        fe = s["false_empty_rate"]
        ff = s["false_finding_rate"]
        lines.append(
            f"{model:28} {s['defect_scored']:>7} "
            f"{s['false_empty']:>3} ({'n/a' if fe is None else f'{fe:.1%}':>6}) "
            f"{s['caught']:>7} {s['clean_scored']:>6} "
            f"{s['false_finding']:>3} ({'n/a' if ff is None else f'{ff:.1%}':>5}) "
            f"{s['errors']:>7}  {'YES' if model in report['allowlist'] else 'no'}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models", default="", help="comma-separated; default: every free model now")
    parser.add_argument("--limit", type=int, default=0, help="only the first N defect and N clean cases")
    parser.add_argument("--out", default="", help="where to write the report (default: print only)")
    parser.add_argument("--state", default="", help="a file to resume from and save progress to")
    parser.add_argument("--budget", type=float, default=0.0, help="stop cleanly after this many seconds")
    args = parser.parse_args(argv)

    key = os.getenv("GRUG_BENCH_OPENCODE_GO_KEY", "").strip()
    if not key:
        print("GRUG_BENCH_OPENCODE_GO_KEY is not set", file=sys.stderr)
        return 2
    import llm_client as lc

    lc._load_opencode_go_key = lambda: key  # type: ignore[assignment]
    lc._spark_data_class_header = lambda: "public"  # type: ignore[assignment]
    os.environ["GRUG_OPENCODE_GO_FREE_DISCOVERY"] = "1"

    models = [m.strip() for m in args.models.split(",") if m.strip()] or current_free_models()
    if not models:
        print("no free models are listed right now", file=sys.stderr)
        return 3
    cases = corpus.build_cases()
    if args.limit:
        defect = [c for c in cases if c.defective][: args.limit]
        clean = [c for c in cases if not c.defective][: args.limit]
        cases = tuple(defect + clean)
    results, finished = run(
        models, cases, state=Path(args.state) if args.state else None,
        budget_seconds=args.budget or None,
    )
    if not finished:
        print("budget spent: run again with the same --state to continue", file=sys.stderr)
        return 4
    report = build_report(results, when=date.today())
    if args.limit:
        report["smoke_run"] = True
    print(render_table(report))
    if args.out:
        if args.limit:
            print("--limit run: not writing a report", file=sys.stderr)
            return 0
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
