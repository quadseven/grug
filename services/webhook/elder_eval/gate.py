"""The #537 CI gate: prompt changes require a re-recorded eval baseline.

`baseline.json` records `prompt_sha` = sha256 of `code_review_prompt.py`
at record time. The per-PR test suite asserts it matches the committed
prompt - so a prompt change FAILS CI until `python -m elder_eval --record`
is re-run and the refreshed baseline lands in the same PR. Deterministic,
no LLM in CI; the honest cost is that recording needs a configured bench
backend (see `sast_benchmark.backends`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import code_review_prompt

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


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
