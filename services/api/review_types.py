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
