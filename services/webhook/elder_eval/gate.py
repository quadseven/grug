"""The #537 CI gate: prompt changes require a re-recorded eval baseline.
Also the grug#1191 cutover gate for the vLLM deep arm
(`deep_cutover_regressions`).

`baseline.json` records `prompt_sha` = sha256 of `code_review_prompt.py`
at record time. The per-PR test suite asserts it matches the committed
prompt - so a prompt change FAILS CI until `python -m elder_eval --record`
is re-run and the refreshed baseline lands in the same PR. Deterministic,
no LLM in CI; the honest cost is that recording needs a configured bench
backend (see `sast_benchmark.backends`).

grug#1511: each backend entry also records `measured_prompt_sha`, the sha its
scores were measured at, written only by `--record`. The top-level sha alone
can be bumped by hand, so the suite also runs `measured_sha_failures`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import code_review_prompt

from .scoring import model_identity

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"

# grug#1191: the baseline entry the vLLM deep arm is recorded under (the
# `deep-shadow` bench backend), and the entry it must not do worse than.
DEEP_CANDIDATE = "deep-shadow"
DEEP_INCUMBENT = "cave"


def compute_prompt_sha() -> str:
    """sha256 of the SHIPPED prompt module's source bytes."""
    src = code_review_prompt.__file__
    if src is None:  # pragma: no cover - a namespace-package accident
        raise RuntimeError("code_review_prompt has no source file to hash")
    return hashlib.sha256(Path(src).read_bytes()).hexdigest()


def load_baseline() -> dict:
    """The committed baseline. Raises if missing - callers that tolerate
    absence (first-ever record) check BASELINE_PATH.exists() first."""
    return json.loads(BASELINE_PATH.read_text())


def merge_baseline(existing: dict, fresh: dict) -> tuple[dict, list[str]]:
    """Merge a freshly-recorded single-backend baseline into the existing
    file's contents. Returns (merged, dropped_backend_names).

    Same prompt_sha: other backends' recorded scores still describe this
    prompt - keep them, refresh the fresh backend's entry. CHANGED
    prompt_sha: their scores describe the OLD prompt - carrying them
    forward would re-bless stale data as fresh, so they are dropped and
    named (re-record them against the new prompt)."""
    if existing.get("prompt_sha") == fresh["prompt_sha"]:
        merged = dict(fresh)
        merged["backends"] = {
            **existing.get("backends", {}),
            **fresh["backends"],
        }
        return merged, []
    dropped = sorted(set(existing.get("backends", {})) - set(fresh["backends"]))
    return dict(fresh), dropped


def measured_sha_failures(baseline: dict) -> list[str]:
    """One message per backend whose scores do not belong to the top-level
    `prompt_sha`; empty means every entry does.

    `carried_prompt_sha` names the one top-level sha an entry was accepted at
    with older scores (the grug#1511 migration, which had no way to re-measure).
    It is not a standing exemption: the next prompt change fails here, and
    `--record` rebuilds the entry without it."""
    prompt_sha = baseline["prompt_sha"]
    return [
        f"baseline entry {name!r} was measured at "
        f"{entry.get('measured_prompt_sha') or 'an unknown prompt'}, not {prompt_sha} - "
        f"re-record it with `python -m elder_eval --backend {name} --record`"
        for name, entry in baseline.get("backends", {}).items()
        if prompt_sha not in (entry.get("measured_prompt_sha"), entry.get("carried_prompt_sha"))
    ]


def deep_cutover_regressions(baseline: dict, model: str) -> list[str]:
    """Why the vLLM deep arm may not yet serve `model`; empty means it may.

    A manifest may set `GRUG_DEEP_ARM_MODE=serve` only when the baseline
    holds a `deep-shadow` entry that measured `model` itself, scored every
    case the incumbent did with none errored, and is no worse than the
    incumbent on overall catch or noise. Both entries come from the same
    bench mode, so they are comparable; a changed prompt drops the entry
    (`merge_baseline`), and serving then waits for a re-record."""
    entries = baseline.get("backends", {})
    candidate = entries.get(DEEP_CANDIDATE)
    incumbent = entries.get(DEEP_INCUMBENT)
    if candidate is None or incumbent is None:
        missing = DEEP_CANDIDATE if candidate is None else DEEP_INCUMBENT
        hint = f"`python -m elder_eval --backend {missing} --record`"
        return [f"baseline has no {missing!r} entry - record it with {hint}"]
    measured = model_identity(candidate)
    if measured != model:
        return [f"{DEEP_CANDIDATE!r} measured {measured or 'an unknown model'}, not {model!r}"]
    return _score_regressions(candidate, incumbent)


def _score_regressions(candidate: dict, incumbent: dict) -> list[str]:
    """The candidate-vs-incumbent comparison behind `deep_cutover_regressions`."""
    reasons: list[str] = []
    if candidate.get("errored_cases"):
        reasons.append(f"{len(candidate['errored_cases'])} case(s) errored")
    cases, base_cases = int(candidate.get("cases_scored", 0)), int(incumbent.get("cases_scored", 0))
    if cases < base_cases:
        reasons.append(f"cases_scored {cases} < incumbent {base_cases}")
    catch, base_catch = float(candidate.get("overall_catch", 0.0)), float(incumbent.get("overall_catch", 0.0))
    if catch < base_catch:
        reasons.append(f"overall_catch {catch:.2f} < incumbent {base_catch:.2f}")
    noise, base_noise = float(candidate.get("noise_rate", 0.0)), float(incumbent.get("noise_rate", 0.0))
    if noise > base_noise:
        reasons.append(f"noise_rate {noise:.2f} > incumbent {base_noise:.2f}")
    return reasons
