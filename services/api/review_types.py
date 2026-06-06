# MIRRORED — sibling at services/webhook/review_types.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Shared leaf types for the code-review path (#250).

The SINGLE source of the `Severity` vocabulary. Imports nothing internal so
every consumer can depend DOWN on it without a cycle — `llm_client`,
`personas/code_reviewer/persona.py`, and `code_review_prompt.py` all import
`Severity`/`SEVERITIES` from here instead of each redefining the Literal (which
let the three copies silently drift — a finding/rule advertising a severity
another module dropped).
"""
from __future__ import annotations

from typing import Literal, get_args

Severity = Literal["low", "medium", "high", "critical"]

# Derived from the Literal so adding a level (e.g. "info") in ONE place
# updates every consumer's validation at once — no drift possible.
SEVERITIES: frozenset[str] = frozenset(get_args(Severity))


# ── Activity-feed verdict + persona mapping (PRD #301; ADR-0002, ADR-0003) ────
# Kept in this shared leaf so BOTH the write path (the persona dispatchers) and
# the read path (the future /activity endpoint) derive the badge from the SAME
# function — the frontend renders it verbatim and never re-derives it.

Verdict = Literal["block", "warn", "pass", "errored"]
VERDICTS: frozenset[str] = frozenset(get_args(Verdict))

# Canonical caveman persona keys used in new code + the Activity feed (ADR-0002).
Persona = Literal["chief", "elder"]
PERSONAS: frozenset[str] = frozenset(get_args(Persona))

# The ONE place the caveman name <-> legacy code key map lives. The persona
# dirs/config still carry the historical keys (`tpm`/`code_reviewer`); new
# surfaces use `chief`/`elder`. Mapping at this single boundary is what keeps
# the two from drifting into a forked copy in the frontend.
_KEY_TO_PERSONA: dict[str, str] = {"tpm": "chief", "code_reviewer": "elder"}
_PERSONA_TO_KEY: dict[str, str] = {v: k for k, v in _KEY_TO_PERSONA.items()}


def persona_for_key(code_key: str) -> Persona:
    """Map a legacy persona code key (`tpm`/`code_reviewer`) to its canonical
    caveman name (`chief`/`elder`). Raises on an unknown key rather than
    inventing a name."""
    try:
        return _KEY_TO_PERSONA[code_key]  # type: ignore[return-value]
    except KeyError:
        raise ValueError(
            f"unknown persona code key {code_key!r}; expected one of "
            f"{sorted(_KEY_TO_PERSONA)}"
        ) from None


def key_for_persona(persona: str) -> str:
    """Inverse of `persona_for_key` — canonical caveman name back to the legacy
    code key. Raises on an unknown name."""
    try:
        return _PERSONA_TO_KEY[persona]
    except KeyError:
        raise ValueError(
            f"unknown persona {persona!r}; expected one of "
            f"{sorted(_PERSONA_TO_KEY)}"
        ) from None


def verdict(
    *,
    conclusion: str,
    findings_count: int,
    degraded_reason: str | None,
) -> Verdict:
    """Derive the Activity-feed badge from a check-run's RAW facts (ADR-0003) —
    the SINGLE place this mapping lives.

    Precedence (order matters):
      1. `degraded_reason` set     -> `errored` (Grug could not evaluate — LLM
         outage / infra failure. NEVER `pass`, per the "no lies" rule.)
      2. `conclusion == "failure"` -> `block`   (a blocking check failed → gated)
      3. `findings_count > 0`      -> `warn`    (advisory issues raised, not gating)
      4. otherwise                 -> `pass`    (clean)

    `findings_count` is whatever the caller counts as actionable: failed
    BLOCKING checks for Chief (0 on pass), surviving findings for Elder. A
    `failure` conclusion always wins over the finding count (a gated PR is
    `block`, not `warn`)."""
    if degraded_reason:
        return "errored"
    if conclusion == "failure":
        return "block"
    if findings_count > 0:
        return "warn"
    return "pass"
