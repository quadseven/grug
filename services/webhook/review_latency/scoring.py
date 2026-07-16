"""Pure latency report math — no network, CI-safe (#648)."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Sequence


@dataclass(frozen=True, slots=True)
class TrialResult:
    """One completed (or failed) request at a given concurrency level."""

    concurrency: int
    fixture: str
    backend: str
    # Seconds from request start to first response body byte (stream).
    # None when the backend does not stream or TTFT could not be measured.
    ttft_s: float | None
    # Full wall-clock until the complete response was available.
    complete_s: float
    parse_ok: bool
    errored: bool
    prompt_chars: int
    response_chars: int


@dataclass(frozen=True, slots=True)
class ConcurrencySlice:
    """Aggregates for one (backend, concurrency) cell."""

    concurrency: int
    backend: str
    n: int
    errors: int
    parse_failures: int
    p50_complete_s: float | None
    p95_complete_s: float | None
    p50_ttft_s: float | None
    p95_ttft_s: float | None
    aggregate_chars_per_s: float | None


@dataclass(frozen=True, slots=True)
class LatencyReport:
    """Full harness report."""

    slices: tuple[ConcurrencySlice, ...]
    trials: tuple[TrialResult, ...]

    def as_markdown(self) -> str:
        lines = [
            "# Elder review latency report (#648)",
            "",
            "| Backend | C | N | Err | Parse fail | p50 complete (s) | p95 complete (s) | p50 TTFT (s) | p95 TTFT (s) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for s in self.slices:
            lines.append(
                f"| {s.backend} | {s.concurrency} | {s.n} | {s.errors} | "
                f"{s.parse_failures} | {_fmt(s.p50_complete_s)} | "
                f"{_fmt(s.p95_complete_s)} | {_fmt(s.p50_ttft_s)} | "
                f"{_fmt(s.p95_ttft_s)} |"
            )
        lines.append("")
        lines.append(
            "TTFT is time-to-first-byte under streaming when the backend "
            "supports it; otherwise blank. Complete is full wall-clock."
        )
        return "\n".join(lines) + "\n"


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}"


def percentile(sorted_vals: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile on a pre-sorted non-empty sequence."""
    if not sorted_vals:
        return None
    if p <= 0:
        return sorted_vals[0]
    if p >= 100:
        return sorted_vals[-1]
    # Nearest rank: ceil(p/100 * n) with 1-based rank, clamped.
    k = max(1, min(len(sorted_vals), int((p / 100.0) * len(sorted_vals) + 0.999999)))
    return sorted_vals[k - 1]


def summarize_trials(trials: Sequence[TrialResult]) -> LatencyReport:
    """Group trials by (backend, concurrency) and compute p50/p95."""
    keys: dict[tuple[str, int], list[TrialResult]] = {}
    for t in trials:
        keys.setdefault((t.backend, t.concurrency), []).append(t)

    slices: list[ConcurrencySlice] = []
    for (backend, conc), group in sorted(keys.items(), key=lambda x: (x[0][0], x[0][1])):
        ok = [t for t in group if not t.errored]
        completes = sorted(t.complete_s for t in ok)
        ttfts = sorted(t.ttft_s for t in ok if t.ttft_s is not None)
        parse_fail = sum(1 for t in ok if not t.parse_ok)
        total_resp = sum(t.response_chars for t in ok)
        total_time = sum(t.complete_s for t in ok)
        agg = (total_resp / total_time) if total_time > 0 else None
        slices.append(
            ConcurrencySlice(
                concurrency=conc,
                backend=backend,
                n=len(group),
                errors=sum(1 for t in group if t.errored),
                parse_failures=parse_fail,
                p50_complete_s=percentile(completes, 50) if completes else None,
                p95_complete_s=percentile(completes, 95) if completes else None,
                p50_ttft_s=percentile(ttfts, 50) if ttfts else None,
                p95_ttft_s=percentile(ttfts, 95) if ttfts else None,
                aggregate_chars_per_s=agg,
            )
        )
    return LatencyReport(slices=tuple(slices), trials=tuple(trials))


def median_complete(trials: Sequence[TrialResult]) -> float | None:
    """Convenience for unit tests."""
    ok = [t.complete_s for t in trials if not t.errored]
    return median(ok) if ok else None
