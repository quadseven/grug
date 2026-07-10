"""Canonical rerun-persona vocabulary - the ONE source the API request
validator (`services/api/installations.py`) and the webhook consumer
(`services/webhook/rerun.py`) both use, so they cannot drift (#581).

The bug this fixes: the consumer was built to re-run Teller/walkthrough
(`dispatch_walkthrough_review`), but the request model's persona regex omitted
`teller`/`walkthrough`, so a Teller rerun 422'd before it could reach the
consumer - a dead capability. The two lived in different services and drifted.

Two sets, one invariant:
  - RERUNNABLE  = personas the consumer actually RE-RUNS.
  - REQUESTABLE = personas a user may REQUEST a rerun for. A superset of
    RERUNNABLE by the `chief`/`tpm` placeholders: those are static checks the
    consumer logs-and-skips today (a deliberate, non-erroring no-op / forward-
    looking follow-up), so accepting the request but not acting is fine.
  - INVARIANT: RERUNNABLE <= REQUESTABLE. A rerunnable persona must never be
    rejected by the request model (that is exactly the #581 dead capability).
"""
from __future__ import annotations

# Alias groups double as the consumer's dispatch-routing keys (each maps to one
# `dispatch_*` call), so they live here and the consumer imports them.
CODE_REVIEWER = frozenset({"elder", "code_reviewer"})
GUARD = frozenset({"guard"})
SMASHER = frozenset({"smasher"})
TELLER = frozenset({"teller", "walkthrough"})

RERUNNABLE: frozenset[str] = CODE_REVIEWER | GUARD | SMASHER | TELLER

# `chief`/`tpm` (the static TPM check) are requestable placeholders the consumer
# logs-and-skips; keeping them accepted preserves the existing API contract.
REQUESTABLE: frozenset[str] = RERUNNABLE | frozenset({"chief", "tpm"})

# Dead-capability guard, enforced at import: a persona the consumer re-runs that
# the request model rejects is unreachable (the #581 class). Fails loudly here
# rather than silently 422-ing real rerun traffic.
assert RERUNNABLE <= REQUESTABLE, (
    "rerun drift: consumer re-runs personas the request model rejects: "
    f"{sorted(RERUNNABLE - REQUESTABLE)}"
)


def requestable_pattern() -> str:
    """Anchored alternation regex of every REQUESTABLE persona, for the
    Pydantic `RerunRequest.persona` `Field(pattern=...)`. Sorted for a stable,
    deterministic pattern string."""
    return rf"^({'|'.join(sorted(REQUESTABLE))})$"
