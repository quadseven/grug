# Paired historical review benchmark

Replay Elder on 30 historical defect PRs and 20 clean controls. The original benchmark's case file, scorer, and scoring thresholds stay pinned to the same revision. Each input comes from the recorded base and head in a local source clone. Input validation checks the raw corpus fingerprint and every diff's file and line counts before any review.

`PINNED_SUITE` points to the `suites/pr_review` directory in a clean checkout at `621b7655c4adb737a1a93404cf6687b98ff34fe4`. `SNAPSHOT_REPOS` holds the source clones, each named by its repository slug and containing the recorded commits. `BEFORE_REVIEWER` points to a clean checkout at `a0e314393b1f99a3bd22d47eef8b3445103f3286`. `AFTER_REVIEWER` points to the clean reviewer revision being measured.

Validate all inputs from the current repository root.

```sh
python3 -m bench.paired_replay --suite "$PINNED_SUITE" --repositories "$SNAPSHOT_REPOS" --check-inputs
```

For measurements, use an authorized local Cave endpoint and set `GRUG_BENCH_CAVE_URL`, `GRUG_BENCH_CAVE_MODEL`, and `GRUG_BENCH_THERMAL_URLS` in the process environment. An endpoint that needs authentication also reads `GRUG_BENCH_CAVE_KEY` from that environment. The model is the concrete identifier that the endpoint returns. Run from the current repository root using the same Python 3.13 dependencies as `make elder-eval`.

```sh
PYTHONPATH=services/webhook:services/_shared \
  uv run --no-project --python 3.13 --with httpx --with pyjwt --with cryptography \
  --with boto3 --with pyyaml --with 'psycopg[binary,pool]' --with 'ddtrace>=3.5,<4' \
  --with 'datadog-lambda>=6.107,<7' python -m bench.paired_replay \
  --suite "$PINNED_SUITE" --repositories "$SNAPSHOT_REPOS" \
  --baseline "$BEFORE_REVIEWER" --candidate "$AFTER_REVIEWER" \
  --model "$GRUG_BENCH_CAVE_MODEL" --output "$REPORT_DIR"
```

The runner alternates which revision reviews first, checks thermal admission before every case, and checks the guards after the final case. It exports both reviewer revisions and the scorer from their committed trees before starting the pair. Each arm performs staged Tier-1 discovery and applies its publication filters using the pinned PR snapshot. Both arms use the same model, empty-answer allowlist, and limit of 16 review cohorts per case. The largest frozen case uses 14 cohorts. The scorer grades only inline-eligible findings. Timings cover review and publication filtering within a worker. The historical score measures reviews by the deployed versions at the time, including queue and publication latency.

The per-request timeout defaults to 150 seconds. Set `GRUG_BENCH_TIMEOUT_SECONDS` to a number greater than 0 and at most 300 to override it for both arms. Each arm still has a 1,200-second limit.

The output directory holds each revision's findings and scorer result. `comparison.json` includes the corpus and diff hashes, measured revisions, served model, request timeout, metrics, and candidate-minus-baseline deltas. An incomplete verdict, inconsistent served model, scorer exclusion, or thermal stop prevents publication of the comparison file.

A stopped replay reports the case and arm, plus a fixed reason code when the worker can classify the failure. `backend_timeout` means the model call reached its deadline. Worker exception messages and response bodies stay out of the report.
