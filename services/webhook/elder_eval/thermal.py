"""Thermal pacing for a bench run against the Sparks (grug#989).

A review-sized call heats a Spark by several degrees on its own, and the
Sparks have cut power under sustained load. So a run against them starts each
case only when every Spark reads `START_AT_C` or below, and stops when one
reads above `ABORT_ABOVE_C`, including after the last case. Each Spark's host thermal guard serves its
reading as JSON (`hottest_c`) over HTTP, by default on port 11439.

Messages name a guard by its 1-based position in the URL list ("guard 2"),
never by URL: the URLs are private-network addresses, and a public repo's
Actions logs are public.

A guard that cannot be read stops the run: pacing that cannot see the
temperature is not pacing.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence

log = logging.getLogger("grug.elder_eval")

ENV_URLS = "GRUG_BENCH_THERMAL_URLS"
START_AT_C = 65.0
ABORT_ABOVE_C = 82.0
POLL_S = 15.0
# Per case. Short enough that a fleet that will not cool stops the run
# cleanly (exit 4) well inside the workflow job timeout.
MAX_WAIT_S = 600.0


class ThermalAbort(RuntimeError):
    """The run must stop: a Spark is too hot, unreadable, or never cooled."""


def read_guard(url: str) -> dict:
    """stdlib, not httpx: httpx logs each request URL at INFO, and the CLI
    logs at INFO into public Actions logs."""
    with urllib.request.urlopen(url, timeout=10.0) as resp:  # noqa: S310 - operator-set URL
        return json.loads(resp.read())


class ThermalPacer:
    """Call it with a case id before each case; it returns when the case may
    start and raises ThermalAbort when the run must stop."""

    def __init__(
        self,
        urls: Sequence[str],
        *,
        poll_s: float = POLL_S,
        max_wait_s: float = MAX_WAIT_S,
        read: Callable[[str], dict] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not urls:
            raise ValueError("thermal pacing needs at least one guard URL")
        self.urls = tuple(urls)
        self.used = False
        self.poll_s = poll_s
        self.max_wait_s = max_wait_s
        self._read = read if read is not None else read_guard
        self._sleep = sleep
        self._clock = clock

    def _hottest(self, index: int) -> float:
        try:
            value = self._read(self.urls[index]).get("hottest_c")
        except Exception as e:  # noqa: BLE001 - any failure is a blind guard
            raise ThermalAbort(
                f"thermal guard {index + 1} unreadable: {type(e).__name__}"
            ) from e
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ThermalAbort(f"thermal guard {index + 1} gave no hottest_c reading")
        return float(value)

    def _read_all(self, when: str) -> list[float]:
        readings = [self._hottest(i) for i in range(len(self.urls))]
        for index, temp in enumerate(readings):
            if temp > ABORT_ABOVE_C:
                raise ThermalAbort(
                    f"thermal guard {index + 1} reads {temp:.1f} C, above "
                    f"{ABORT_ABOVE_C:.1f} C; stopping {when}"
                )
        return readings

    def check(self) -> None:
        """One reading, no waiting: raises when a Spark is above the stop line
        (the run's last case ends with no next case to check before)."""
        self._read_all("after the last case")

    def __call__(self, case_id: str) -> None:
        self.used = True
        started = self._clock()
        while True:
            readings = self._read_all(f"before case {case_id}")
            if all(temp <= START_AT_C for temp in readings):
                return
            waited = self._clock() - started
            if waited + self.poll_s > self.max_wait_s:
                raise ThermalAbort(
                    f"Sparks did not cool to {START_AT_C:.1f} C within "
                    f"{self.max_wait_s:.0f} s before case {case_id}: {readings}"
                )
            log.info(
                "eval_thermal_wait case=%s hottest=%.1f",
                case_id, max(readings),
            )
            self._sleep(self.poll_s)


def pacer_from_env(env: Mapping[str, str]) -> ThermalPacer | None:
    """`GRUG_BENCH_THERMAL_URLS`, comma-separated; unset or blank: no pacing."""
    urls = [u.strip() for u in env.get(ENV_URLS, "").split(",") if u.strip()]
    return ThermalPacer(urls) if urls else None
