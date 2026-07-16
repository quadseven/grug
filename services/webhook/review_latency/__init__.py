"""Elder review latency replay harness (#648 / epic #645).

Measures wall-clock and (when streaming works) time-to-first-token on
long-context review prompts at concurrency 1/2/4/8. Pure scoring is
CI-safe; live runs need GRUG_BENCH_CAVE_* env and never run in per-PR CI.
"""
