# ADR-0002 — Caveman persona names (`chief`/`elder`) are the canonical identity in new code

## Status

Accepted (2026-06-06). Amended 2026-07-16: GitHub check-run titles are
`Grug — <Caveman>` (Chief/Elder/…); SSOT in `personas/tribe.py`.

## Context

Grug's **personas** carry two names:

- A **caveman name** used on every product surface — the dashboard, check-run chrome (`_PERSONA = "Elder"` already in `code_reviewer/dispatch.py`), and the design's "Caveman Editorial" voice: **Chief**, **Elder**, (roadmap: Guard, Smasher, Warder).
- A **historical code key** baked into the codebase: the persona directories `personas/tpm/` and `personas/code_reviewer/`, plus config fields like `tpm_enabled`.

The Activity feed (PRD #301) introduces a new persisted record (`CheckVerdictRecord`) and a JSON API (`/activity`) that must store and serve a persona identity. We had to decide which name is canonical in the new code, and whether the map between the two lives in one place or is forked into the frontend.

The map (`tpm → Chief`, `code_reviewer → Elder`) was previously written **nowhere** — a latent single-source-of-truth hazard: if the frontend hardcoded "Chief" while the backend stored `tpm`, the two would drift.

## Decision

**New code uses the caveman keys (`chief`, `elder`) as the canonical persona identity.** The `CheckVerdictRecord.persona` field and the `/activity` payload carry `chief`/`elder`, never `tpm`/`code_reviewer`.

- The legacy persona dirs (`personas/tpm/`, `personas/code_reviewer/`) and config keys keep their names for now — a full rename is a separate, large refactor, out of scope.
- The `tpm → chief` / `code_reviewer → elder` map lives in **exactly one** persona-name mapper, applied at the single write boundary. The frontend renders the resolved name from the API; it never re-encodes the map.
- The feed shows **only personas that actually ran** (Chief + Elder today). Roadmap personas (Guard/Smasher/Warder) produce no rows until they are real services — no placeholders ("no lies").

## Consequences

### Positive

- One source of truth for the persona-name map; the frontend can't drift from the backend.
- New surfaces are on-brand (caveman voice) from day one, matching the existing `_PERSONA = "Elder"`.
- Decouples the outward identity from the historical code keys, so a later dir rename doesn't churn the API contract.

### Negative

- Two names for one persona coexist (caveman key in new code/feed; legacy key in `personas/` dirs + config). A reader must know the map.
- The mapper is a small boundary that must be kept in lockstep across the mirrored services (ADR-0001).

### Reconsideration triggers

- A full rename of `personas/tpm/` → `personas/chief/` (and `code_reviewer` → `elder`) lands — at which point the legacy keys disappear and this ADR's "two names" tension dissolves.
- A roadmap persona (Guard/Smasher/Warder) ships and needs its own caveman↔key entry.
- Check-run titles diverge from caveman names again (the 2026-07-16 tribe polish
  closed the "Definition of Ready" / "Code Review" gap).

## Amendment (2026-07-16) — tribe check titles

Product check-run names are now `Grug — Chief`, `Grug — Elder`, etc., matching
dashboard and Activity. Legacy titles (`Grug — Definition of Ready`,
`Grug — Code Review`) dual-post as alias status checks and remain accepted by
`detect_enforcement` so required-status rulesets do not brick merges mid-cutover.
Capability voice: **Seer** (exploitability filter), **Omen**, **Lore**,
**Markings**, **Cave**, hunt settle tiers.

## References

- PRD #301 (Activity feed backend + re-run)
- ADR-0001 (mirror discipline — the mapper is a mirrored boundary)
- `CONTEXT.md` — `Persona`, `Chief`, `Elder`
- `services/_shared/personas/tribe.py` — check-title SSOT
