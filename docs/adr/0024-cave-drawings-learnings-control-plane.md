# ADR-0024 - Cave Drawings: an operator control plane for learnings

## Status

Accepted (2026-10-19). Implements the first slice of issue #1203. Builds on
ADR-0020 (reply-mined learnings), whose deferred list this slice works
through.

## Context

ADR-0020 shipped the store-and-apply loop for operator-taught learnings and
deferred their management: there was no way to list, correct, switch off or
delete a learning short of editing the store by hand, and nothing recorded
whether a learning was ever used. Every stored learning steered every later
review of its repository, unaudited.

## Decision

1. A pure core, `services/_shared/cave_drawings.py`, owns every decision
   that needs no I/O: learning identity (the text digest the store already
   keys on), the lifecycle states, one deterministic path matcher, prompt
   selection, and the export format plus a dry-run import plan.
2. Lifecycle. A learning is `active` (injected), `pending` (waits for an
   explicit approval), `disabled` (switched off, kept) or `superseded`
   (replaced by an edited version, kept as history). A row written before
   this ADR has no status and reads as `active`, so nothing changes for
   existing learnings. Reply-mined learnings still start `active`; imported
   ones start `pending`.
3. Editing the text of a learning creates a successor with a new id that
   `supersedes` the old one, carries its provenance and full history, and
   bumps `version`. The old row becomes `superseded`. Scope and conflict
   links change in place with a history entry. A re-teach of the same reply
   never undoes an operator's decision.
4. Prompt selection injects only active learnings, never both sides of a
   `supersedes` or `conflicts_with` link (the more recent one wins), and
   logs every suppression. The renderer drops whole rows at the byte bound,
   so it knows exactly which learnings reached the prompt; only those get a
   use counted, once per PR head sha however many prompts that review
   builds.
5. The path matcher is `fnmatchcase` with `*` crossing directories and
   `**/` also matching zero directories. The classifier's scope check uses
   the same matcher, so a preview shows exactly what a scope covers.
6. Export is a versioned JSON document without instance-local counters.
   Import is a dry run by default; applying requires a clean plan (no
   invalid rows, an unambiguous target repository), writes in one
   transaction, and rolls back entirely if a row collides with a learning
   created since the dry run.
7. The API is admin-only under `/api/v1/admin/learnings` (list and search,
   inspect, edit, approve, disable, delete, preview, export, import).

## Consequences

- Operators can audit and correct what Grug believes without touching the
  database, and every mutation is logged with its actor.
- Org-wide scope, an install-owner (non-admin) view, a dashboard page, and
  an approval delay for newly mined learnings remain open on #1203.
