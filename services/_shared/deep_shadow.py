"""Optional vLLM deep arm: shadow it, then serve it, with one-switch rollback (grug#1191).

`GRUG_DEEP_ARM_MODE` decides what the deep (reasoner) pass does with a
dedicated vLLM server named by `GRUG_DEEP_SHADOW_URL` (its base URL) and
`GRUG_DEEP_SHADOW_MODEL` (the model id it serves):

- `off`, the default: the deep pass runs exactly as it did before this module.
- `shadow`: the deep pass still answers and is the only thing published. The
  same prompt is replayed against the vLLM arm on one background worker, and
  only a comparison is logged (`deep_shadow_compared`). Shadow findings never
  reach a PR.
- `serve`: the vLLM arm replaces the Cave reasoner call. When it is not
  resident or fails, the Cave reasoner answers as before. A committed manifest
  in this mode fails CI until the elder_eval baseline holds a passing
  `deep-shadow` entry for the same model (`elder_eval.gate`).

Rollback from either mode is one change: `GRUG_DEEP_ARM_MODE=off`.

Always-hot: the arm is only called when the server's `/v1/models` lists the
configured model as owned by vLLM. vLLM loads its one model at process start,
so a listed model is resident. An Ollama-style front lists every model it
could load on demand, and calling one of those is the cold load (an ~87GB
read per review) that once timed out every production review; it is refused.
The probe result is cached for `_RESIDENCY_TTL_S`, so a review pays at most
one short GET per window.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal, get_args

import httpx

log = logging.getLogger("grug.deep_shadow")

DeepArmMode = Literal["off", "shadow", "serve"]
_MODES: frozenset[str] = frozenset(get_args(DeepArmMode))

_RESIDENCY_TTL_S = 60.0
_PROBE_TIMEOUT_S = 2.0

# One worker, and a slot that is taken before submit: a shadow call that is
# still running when the next review finishes means that review is not
# shadowed, rather than a queue that grows behind a slow GPU host.
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deep-shadow")
_busy = threading.BoundedSemaphore(1)

# (base_url, model) -> (resident, monotonic time checked)
_residency_cache: dict[tuple[str, str], tuple[bool, float]] = {}


@dataclass(frozen=True, slots=True)
class DeepArm:
    """A resolved, non-off deep arm: which mode, and where the vLLM server is."""

    mode: Literal["shadow", "serve"]
    base_url: str
    model: str


def resolve_deep_arm() -> DeepArm | None:
    """The configured deep arm, or None for `off`.

    An unknown mode, or a mode whose URL or model is unset, resolves to None
    and is logged: a typo must fall back to the current reasoner, never to an
    arm nobody has evaluated."""
    raw = os.getenv("GRUG_DEEP_ARM_MODE", "off").strip().lower() or "off"
    if raw not in _MODES:
        log.warning("deep_arm_mode_unknown", extra={"mode": raw})
        return None
    if raw == "off":
        return None
    base_url = os.getenv("GRUG_DEEP_SHADOW_URL", "").strip().rstrip("/")
    model = os.getenv("GRUG_DEEP_SHADOW_MODEL", "").strip()
    if not base_url or not model:
        log.error(
            "deep_arm_unconfigured",
            extra={"mode": raw, "has_url": bool(base_url), "has_model": bool(model)},
        )
        return None
    return DeepArm(mode=raw, base_url=base_url, model=model)  # type: ignore[arg-type]


def _probe(arm: DeepArm) -> bool:
    """True when the server's /v1/models lists `arm.model` as owned by vLLM."""
    try:
        resp = httpx.get(f"{arm.base_url}/v1/models", timeout=_PROBE_TIMEOUT_S)
        resp.raise_for_status()
        served = resp.json().get("data", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning(
            "deep_vllm_probe_failed",
            extra={"model": arm.model, "error_type": type(e).__name__, "error": str(e)[:200]},
        )
        return False
    return any(
        m.get("id") == arm.model and m.get("owned_by") == "vllm"
        for m in served
        if isinstance(m, dict)
    )


def is_resident(arm: DeepArm) -> bool:
    """Whether `arm.model` is loaded and served by vLLM right now (cached)."""
    key = (arm.base_url, arm.model)
    now = time.monotonic()
    cached = _residency_cache.get(key)
    if cached is not None and now - cached[1] < _RESIDENCY_TTL_S:
        return cached[0]
    resident = _probe(arm)
    _residency_cache[key] = (resident, now)
    if not resident:
        log.warning("deep_vllm_not_resident", extra={"model": arm.model, "mode": arm.mode})
    return resident


def submit(job: Callable[[], None]) -> Future | None:
    """Run `job` on the shadow worker, or skip it (None) when the worker is busy.

    A job that raises is logged as `deep_shadow_failed`; the exception also
    stays on the returned future."""
    if not _busy.acquire(blocking=False):
        log.info("deep_shadow_skipped_busy")
        return None

    def run() -> None:
        try:
            job()
        except Exception:
            log.exception("deep_shadow_failed")
            raise
        finally:
            _busy.release()

    try:
        return _POOL.submit(run)
    except RuntimeError:
        # The pool is shut down (interpreter exit); nothing will run the job.
        _busy.release()
        log.exception("deep_shadow_failed")
        return None


def finding_keys(findings) -> set[tuple[str, int]]:
    """(path, line) per finding: the overlap measure the comparison log uses."""
    return {(f.path, f.line) for f in findings}
