#!/usr/bin/env python3
"""Grounding attester for spec 0007.UserIdentity.

Proves NECESSARY conditions for these bools:

  - `frozen_dataclass_no_post_construction_mutation_per_identity_concepts`
  - `identity_only_projection_has_no_token_fields_per_identity_concepts`
  - `defaults_role_user_tier_free_allowlisted_false_per_identity_concepts`

Asserts that `services/api/adapters/pg_user_store.py:UserIdentity`:
  1. Is decorated with `@dataclass(frozen=True)` — no post-construction mutation.
  2. Has NO field whose name contains `oauth_`, `token`, `password`, `secret`,
     or `blob` — token material lives ONLY on UserWithTokens (spec 0008).
  3. Defines exactly the identity fields documented in CONTEXT.md:
     github_user_id, login, role, tier, allowlisted, created_at,
     allowlisted_at, allowlisted_by.

The closed-set field list is intentional: adding a new identity field
should be a deliberate spec change, not an accident. If you want to add
one, edit both the spec AND this attester's EXPECTED_FIELDS in the same
PR.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
USER_STORE = REPO_ROOT / "services/_shared/adapters/pg_user_store.py"

EXPECTED_FIELDS: frozenset[str] = frozenset({
    "github_user_id", "login", "role", "tier",
    "allowlisted", "created_at", "allowlisted_at", "allowlisted_by",
})

# Substring patterns that indicate token material — must not appear in
# UserIdentity field names (those belong on UserWithTokens per spec 0008).
FORBIDDEN_FIELD_SUBSTRINGS: tuple[str, ...] = (
    "oauth_", "token", "password", "secret", "blob",
)


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _is_frozen_dataclass(cls: ast.ClassDef) -> bool:
    for dec in cls.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        fn = dec.func
        is_dataclass = (
            (isinstance(fn, ast.Name) and fn.id == "dataclass")
            or (isinstance(fn, ast.Attribute) and fn.attr == "dataclass")
        )
        if not is_dataclass:
            continue
        for kw in dec.keywords:
            if kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
    return False


def _class_fields(cls: ast.ClassDef) -> list[str]:
    """Return AnnAssign field names (the dataclass-style typed attrs)."""
    out: list[str] = []
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            out.append(stmt.target.id)
    return out


def main() -> int:
    if not USER_STORE.exists():
        print(f"FAIL: {USER_STORE} missing")
        return 1

    tree = ast.parse(USER_STORE.read_text())
    cls = _find_class(tree, "UserIdentity")
    if cls is None:
        print(f"FAIL: {USER_STORE}: UserIdentity class not found")
        return 1

    failures: list[str] = []

    if not _is_frozen_dataclass(cls):
        failures.append("UserIdentity is not @dataclass(frozen=True) — spec 0007 attests frozen.")

    fields = _class_fields(cls)
    fieldset = set(fields)

    # No token material allowed.
    for field in fields:
        lower = field.lower()
        for forbidden in FORBIDDEN_FIELD_SUBSTRINGS:
            if forbidden in lower:
                failures.append(
                    f"UserIdentity has forbidden field `{field}` — token material belongs on UserWithTokens (spec 0008)."
                )

    # Closed-set: exactly the expected fields, no more no less.
    extra = fieldset - EXPECTED_FIELDS
    missing = EXPECTED_FIELDS - fieldset
    if extra:
        failures.append(f"UserIdentity has unexpected fields: {sorted(extra)}. Update spec 0007 + EXPECTED_FIELDS together.")
    if missing:
        failures.append(f"UserIdentity is missing expected fields: {sorted(missing)}. Spec 0007 lists them as identity-required.")

    if failures:
        print(f"FAIL: {USER_STORE.name}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: UserIdentity is frozen + identity-only ({len(EXPECTED_FIELDS)} fields, zero token surface)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
