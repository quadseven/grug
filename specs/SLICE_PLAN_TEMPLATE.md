# Slice Plan Template

This is the template for every SDD slice plan. Copy into
`specs/<NNNN-spec-slug>/plans/<YYYY-MM-DD>-<slice>.md` when starting a new
slice. Every section is required — if a section genuinely doesn't apply,
the plan author states `N/A — <reason>` so reviewers know it was
considered, not forgotten.

The template exists because consecutive slice plans BLOCK at `/peer-review`
on the **same recurring patterns**. Baking them into v1 plans up-front
collapses the v1→v2 round from ceremonial to substantive — the review still
adds value catching NEW failure classes per slice, but the predictable
CRITs are pre-empted at draft time.

The grug-specific CRITs (from PR #151 peer-review with 4 reviewers) are
folded in as §10 onward.

---

## 0. Slice header

- **Spec:** `NNNN-<slug>`
- **IOA action(s) grounded:** `AttestXContract` (and any others)
- **Bools grounded (atomic set):** list every bool in each action's `effect`;
  cherry-picking is forbidden by the manifest validator
- **Bools left deferred (with reason):** every bool not grounded must appear
  in the explicit deferral checklist (§6)
- **Estimated size:** 90 / 120 / 180 min target

---

## 1. IOA atomicity verification

Confirm the **complete** effect set of every IOA action this slice claims to
ground. Cherry-picking a subset of an action's bools is the #1 BLOCK pattern.

For each `AttestXContract` being promoted to `grounded`:

```
AttestXContract effect set (from spec NNNN/<slug>.ioa.toml):
  - bool_one        ← grounded this slice by attester <path>
  - bool_two        ← grounded this slice by attester <path>
  - bool_three      ← grounded this slice by attester <path>
```

---

## 2. Runtime attester design (per bool)

Every grounded bool needs an attester that **proves a behavioral claim**
against real source/runtime, not just a regex over text.

For each attester, specify:
- **What real artifact is exercised:** AST walk of a specific function /
  FastAPI TestClient call / DDB moto fixture / live API probe — NOT a
  unit test on the helper.
- **What's asserted:** structural / ordered / property-equality / behavioral.
- **Allowlist or denylist?** Per peer-review HIGH on PR #151 (4x): use
  ALLOWLIST for "what's permitted" (closed set). Denylist escapes via
  new patterns the author didn't think of.
- **Negative path** if relevant — does the attester also produce a
  deliberate violation fixture that confirms it fails?

---

## 3. Cross-user / cross-tenant defense (if the slice mutates state)

If this slice adds any mutation endpoint, ownership defense MUST live at
**both** layers:

- **Route layer:** preflight check → 404 if no permission.
- **Adapter / storage layer:** DDB `ConditionExpression: attribute_exists(...)`
  on the row's owning attribute — the storage primitive itself enforces
  ownership.

Pytest cases required:
- Route-level: cross-user request returns 404 + no state change.
- Adapter-level (under moto): direct cross-user call raises ownership
  error AND neither user's row is mutated AND no orphan rows leak.

If no mutation: `N/A — read-only slice`.

---

## 4. Single transactional mutation (if multi-entity)

If the slice's mutation touches more than one entity, use a **single**
`TransactWriteItems` or `update_item` with `if_not_exists()` clauses.
Sequential adapter calls create split-brain + lost-update races
(peer-review CRITICAL on PR #151).

Document:
- The items in the transaction.
- Which item carries the `ConditionExpression` + what it asserts.
- How `TransactionCanceledException` maps to a domain exception.
- Why no separate read-then-write step was inserted (the temptation is
  always there — the rewrite is non-trivial).

If single-entity: `N/A — single-entity write`.

---

## 5. Clock injection (if time-sensitive)

If any bool depends on time, the handler MUST read its clock from
`app.state.<entity>_clock` with a default of `datetime.now(UTC)`. The
attester injects a fixed clock + asserts boundary-band behavior at exact
instants.

Specify: where the clock is read, the fixed instant the attester uses,
and the expected mapping (e.g. `slot=past → clock() - 1d`).

If no time dependency: `N/A — no time-sensitive contracts`.

---

## 6. Explicit deferral checklist

Enumerate **every mock element / future surface NOT translated this slice**
with the owning future IOA action. Implicit gaps get flagged as theater.

| Mock element / surface | Why deferred | Owning future action |
|---|---|---|
| ... | ... | ... |

If nothing deferred: `N/A — full surface translated`.

---

## 7. Shared-package discipline (services/_shared/, ADR-0014)

If the slice touches a cross-service module, edit the single copy in
`services/_shared/`. NEVER create a same-relpath file under
`services/api/` or `services/webhook/` - it silently shadows the shared
module for that one service (the post-extraction drift class; guarded by
`services/webhook/tests/test_shared_no_shadowing.py` and
`infra/scripts/attest_mirror_policy_consistency.py`).

If a shared module needs service-specific behavior, parameterize it
(env read / argument), or - for whole modules only one service executes -
keep the module in `_shared/` with an `# API-ONLY` / `# WEBHOOK-ONLY`
line-1 marker and lazy imports (the user-store / trial_* pattern).

If no shared modules touched: `N/A — no shared surface`.

---

## 8. Storage-side scope of mutation (peer-review CRITICAL 4x, PR #151)

If the slice writes/deletes a DDB row, audit which fields the operation
actually touches. **`delete_item` removes the entire row** — destroying
sibling fields like `role`, `tier`, `allowlisted` that are orthogonal to
the slice's concern. Default to `update_item` with explicit `SET` /
`REMOVE` clauses naming only the slice's fields.

For every new mutation:
- [ ] Lists every field the operation will touch.
- [ ] If `delete_item`, confirms the row carries NO orthogonal state.
- [ ] If `update_item`, names exactly which fields change + which
      `if_not_exists()` clauses preserve identity/audit state.

If no DDB mutation: `N/A — no DDB write`.

---

## 9. Sign-off

- [ ] All 8 sections above addressed (or explicit `N/A`).
- [ ] `temper verify -s specs/NNNN-<slug>/` passes locally.
- [ ] Each attester runs locally + exits 0.
- [ ] `make test` (or `uv run pytest`) passes for the affected services.
- [ ] Plan submitted to `/peer-review` (optional but recommended for first
      slice on a new IOA action).

If peer-review escalates to BLOCK on a pattern NOT in this template, add
the pattern as a §10+ to this file for the next slice. The template is a
living document.

---

## §10 — Sizing breakdown

| Sub-task | Estimate |
|---|---|
| Spec bool/action edit + temper verify | ~10 min |
| Adapter method + ownership condition + atomic update | ~25 min |
| Route handler | ~25 min |
| Grounding attester (AST walk or live probe) | ~30 min |
| Regression test for the CRIT class | ~20 min |
| CI wiring (workflow step + path filter) | ~10 min |
| Full test suite + commit + push | ~10 min |
| **Total** | **~130 min** |

---

## §11 — Recurring CRITs to pre-empt (grug, post PR #151 audit)

These are the patterns peer-review has BLOCK'd on. Address each (or
document explicit waiver) before submitting v1:

1. **Over-broad DELETE** — `delete_item` destroying orthogonal metadata.
   Default to targeted `update_item REMOVE`. (§8.)
2. **Lost-update via read-then-write** — `get_item` (eventual consistency)
   followed by `put_item` reverts concurrent admin writes. Use atomic
   `update_item` with `if_not_exists()`. (§4.)
3. **Bare side-effect calls without wrapping** — network IO (`httpx`,
   `post_check_run`) that propagates uncaught into the request handler.
   Always wrap in `try/except (HTTPStatusError, RequestError)` with a
   structured log + skip return.
4. **Denylist attesters** — `FORBIDDEN_CALL_NAMES` escapes via new IO
   surfaces. Use ALLOWLIST of permitted call roots. (§2.)
5. **Vacuous-PASS attesters** — empty path tuples cause `OK 0 verified`.
   Every attester `main()` starts with `if not PATHS: exit 1`. (§2.)
6. **Workflow path-filter omissions** — adding a spec attester that
   imports from `crypto/` / `github_app_auth/` / new module without
   adding the path to the workflow's `paths:` filter means renames in
   that module silently bypass the attester. (§9 sign-off.)

When a CRIT class becomes mechanically detectable, the rule should move
OUT of this template INTO a CI gate or grounding attester. Until then,
template + per-slice peer-review are the human-layer enforcement.

---

## How this file is enforced

This template is NOT CI-gated (plans are advisory documents). The rules
it codifies ARE enforced by:

- `temper verify` — IOA state-machine reachability + invariants
- The 12 grounding attesters under `infra/scripts/attest_*.py` — claim-
  proving against real source/runtime
- `services/webhook/tests/test_shared_no_shadowing.py` + the spec-0010
  attester — shared-package shadowing guard (ADR-0014)
- `services/{api,webhook}/tests/test_log_pii_guard.py` — PII surface
- The full pytest suite for each service

Reviewers can call out missed sections at `/peer-review` time. The
template gives plan authors the checklist they need to write a v1 plan
that passes the first review.
