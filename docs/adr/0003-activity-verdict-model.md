# ADR-0003 — Activity verdict model: store raw facts, derive the badge; `errored` is a first-class state

## Status

Accepted (2026-06-06)

## Context

The Activity feed (PRD #301) shows one **Verdict** badge per row: `block` / `warn` / `pass` / `errored`. The design mock used three (`block/warn/pass`), but the domain doesn't have a `warn` conclusion, and GitHub check-runs are `success | failure | neutral | …`. Two complications surfaced during design:

1. **`neutral` is overloaded.** Elder is advisory by default, so it posts `neutral` whether it found 3 issues or 0 — *and* a degraded run (LLM outage) also posts `neutral`. The raw conclusion alone cannot tell `warn` (neutral + findings), `pass` (neutral + clean), and a failed run apart. (Confirmed live during the 2026-06 Elder outage: degraded runs posted `neutral` with 0 findings.)
2. **A degraded run is not a pass.** Under the naive mapping, an LLM-outage `neutral` + 0 findings would render `PASS` — a fabricated "all clear" for a review that never happened, violating the project's hard **"no lies"** constraint.

We also had to decide whether to persist the computed badge or the raw inputs, given the log is append-only (a row written today is permanent).

## Decision

**Store the raw facts; derive the badge with one shared pure function; make `errored` a first-class verdict.**

- `CheckVerdictRecord` stores raw inputs: `conclusion`, `summary`, `findings_count`, `blocking`, `degraded_reason` — NOT a pre-collapsed badge.
- A single pure function maps them to the badge — the **only** place the mapping lives:
  - `failure → block`
  - advisory/`neutral` **with** findings → `warn`
  - clean (`success`, or `neutral` with nothing actionable) → `pass`
  - `degraded_reason` set → **`errored`** (never `pass`)
- The mapping is applied **server-side** in `GET /activity`; the frontend renders the result verbatim and never re-derives it.
- A denormalized `verdict` may be stored on the row for cheap DDB filtering, but the **raw facts remain canonical** — a future mapping change can re-derive all of history truthfully.

## Consequences

### Positive

- "No lies": a degraded run is visibly `errored`, never a fake pass. Distinct from `pass`/`warn`/`block`.
- Single source of truth for the badge — frontend can't drift; SSOT satisfied by *compute-once-logic*, not by persisting the value.
- Append-only history stays re-derivable: if the mapping rules change, old rows recompute correctly because raw facts were kept.
- The richer facts (`findings_count`, `summary`) are preserved for future UI (e.g. per-finding detail) instead of discarded.

### Negative

- Slightly more per-row storage than a single badge byte (negligible at this volume + 90-day TTL).
- The derive step runs on read; acceptable given the feed is small and capped.

### Reconsideration triggers

- Read volume grows enough that read-time derivation matters — at which point the denormalized `verdict` (already stored) can be served directly, or moved to a GSI.
- A new conclusion class or persona outcome needs a 5th badge.

## References

- PRD #301 (Activity feed backend + re-run)
- ADR-0002 (persona naming) — the row's `persona` field
- `CONTEXT.md` — `Check verdict`, `Verdict`
- Decision-critic review of the verdict-display question (this conversation)
