"""CLI: python -m review_latency

Live Cave/OpenAI-compatible latency sweep for Elder-shaped review prompts
(#648). Never runs in per-PR CI.

Env (same family as sast_benchmark / elder_eval):
  GRUG_BENCH_CAVE_URL     OpenAI-compatible chat completions URL (required for Cave)
  GRUG_BENCH_CAVE_MODEL   Model id (e.g. coder or reasoner tag)
  GRUG_BENCH_CAVE_KEY     Optional bearer token
  GRUG_BENCH_CAVE_NAME    Backend label (default: cave)

Optional second arm:
  GRUG_BENCH_REASONER_URL / GRUG_BENCH_REASONER_MODEL / GRUG_BENCH_REASONER_KEY

Flags:
  --levels 1,2,4,8     concurrency levels (default)
  --no-stream          wall-clock only
  --fixture small|medium|large|all
  --json PATH          also write machine-readable trials
  --timeout-s 330      per-request timeout

Example:
  cd services/webhook && PYTHONPATH=../_shared \\
    GRUG_BENCH_CAVE_URL=http://127.0.0.1:11434/v1/chat/completions \\
    GRUG_BENCH_CAVE_MODEL=qwen3-coder-next:q8_0 \\
    python -m review_latency --levels 1,2,4
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from sast_benchmark.backends import BenchBackend

from .fixtures import default_fixtures
from .runner import sweep
from .scoring import summarize_trials


def _backends_from_env() -> list[BenchBackend]:
    out: list[BenchBackend] = []
    cave_url = os.getenv("GRUG_BENCH_CAVE_URL", "").strip()
    cave_model = os.getenv("GRUG_BENCH_CAVE_MODEL", "").strip()
    if cave_url and cave_model:
        out.append(
            BenchBackend(
                name=os.getenv("GRUG_BENCH_CAVE_NAME", "cave").strip() or "cave",
                url=cave_url,
                model=cave_model,
                api_key=os.getenv("GRUG_BENCH_CAVE_KEY", ""),
            )
        )
    r_url = os.getenv("GRUG_BENCH_REASONER_URL", "").strip()
    r_model = os.getenv("GRUG_BENCH_REASONER_MODEL", "").strip()
    if r_url and r_model:
        out.append(
            BenchBackend(
                name=os.getenv("GRUG_BENCH_REASONER_NAME", "reasoner").strip() or "reasoner",
                url=r_url,
                model=r_model,
                api_key=os.getenv("GRUG_BENCH_REASONER_KEY", ""),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Elder review latency harness (#648)")
    p.add_argument(
        "--levels",
        default="1,2,4,8",
        help="Comma-separated concurrency levels",
    )
    p.add_argument(
        "--fixture",
        default="all",
        choices=("small", "medium", "large", "all"),
    )
    p.add_argument("--no-stream", action="store_true")
    p.add_argument("--timeout-s", type=float, default=330.0)
    p.add_argument("--json", dest="json_path", default="", help="Write trials JSON")
    args = p.parse_args(argv)

    backends = _backends_from_env()
    if not backends:
        print(
            "No backends configured. Set GRUG_BENCH_CAVE_URL and "
            "GRUG_BENCH_CAVE_MODEL (see module docstring).",
            file=sys.stderr,
        )
        return 2

    levels = tuple(int(x.strip()) for x in args.levels.split(",") if x.strip())
    fixtures = default_fixtures()
    if args.fixture != "all":
        fixtures = tuple(f for f in fixtures if f.name == args.fixture)
    if not fixtures:
        print("No fixtures selected", file=sys.stderr)
        return 2

    print("Fixtures:")
    for f in fixtures:
        print(f"  {f.name}: ~{f.added_lines} added lines, {f.prompt_chars} prompt chars")

    all_trials = []
    for backend in backends:
        print(f"\n=== backend={backend.name} model={backend.model} ===")
        trials = sweep(
            backend,
            fixtures,
            levels,
            stream=not args.no_stream,
            timeout_s=args.timeout_s,
        )
        all_trials.extend(trials)

    report = summarize_trials(all_trials)
    print()
    print(report.as_markdown())

    if args.json_path:
        path = Path(args.json_path)
        payload = [
            {
                "concurrency": t.concurrency,
                "fixture": t.fixture,
                "backend": t.backend,
                "ttft_s": t.ttft_s,
                "complete_s": t.complete_s,
                "parse_ok": t.parse_ok,
                "errored": t.errored,
                "prompt_chars": t.prompt_chars,
                "response_chars": t.response_chars,
            }
            for t in all_trials
        ]
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
