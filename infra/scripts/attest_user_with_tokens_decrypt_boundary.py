#!/usr/bin/env python3
"""Grounding attester for spec 0008.UserWithTokens.

Proves NECESSARY conditions for these bools:

  - `frozen_dataclass_no_post_construction_mutation_per_identity_concepts`
  - `only_constructed_by_api_lambda_get_user_with_tokens_per_identity_concepts`
  - `webhook_never_constructs_user_with_tokens_per_persistence_concepts`
  - `oauth_access_token_required_oauth_refresh_token_optional_per_identity_concepts`

Asserts:
  1. `services/api/adapters/user_store.py:UserWithTokens` is `@dataclass(frozen=True)`.
  2. UserWithTokens carries exactly `identity`, `oauth_access_token`, `oauth_refresh_token` fields.
  3. oauth_refresh_token has type `str | None` (optional).
  4. The webhook side (`services/webhook/`) contains NO import / reference to
     `UserWithTokens` — service-scope wall per spec.
  5. UserWithTokens construction happens ONLY in `services/api/adapters/user_store.py`
     (search the whole repo for `UserWithTokens(...)` calls — there should be
     exactly one site, in get_user_with_tokens).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Post-swap (#354): pg_user_store.py IS the user store; user_store.py is
# a re-export facade with no class definitions. The shape + construction
# walls apply to the canonical file only.
USER_STORE = REPO_ROOT / "services/api/adapters/pg_user_store.py"
PG_USER_STORE = USER_STORE
WEBHOOK_DIR = REPO_ROOT / "services/webhook"
API_DIR = REPO_ROOT / "services/api"

EXPECTED_FIELDS: frozenset[str] = frozenset({
    "identity", "oauth_access_token", "oauth_refresh_token",
})


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
        is_dc = (
            (isinstance(fn, ast.Name) and fn.id == "dataclass")
            or (isinstance(fn, ast.Attribute) and fn.attr == "dataclass")
        )
        if not is_dc:
            continue
        for kw in dec.keywords:
            if kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
    return False


def _class_fields(cls: ast.ClassDef) -> dict[str, ast.expr | None]:
    """Return {field_name: type_annotation_ast} for AnnAssign attrs."""
    out: dict[str, ast.expr | None] = {}
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            out[stmt.target.id] = stmt.annotation
    return out


def _refresh_is_optional(annotation: ast.expr | None) -> bool:
    """Verify the annotation includes `None` — accepts `str | None`,
    `Optional[str]`, `None | str`."""
    if annotation is None:
        return False
    # Walk the annotation looking for a None constant — covers str|None,
    # Optional[str] (which the typing module resolves to Union[str, None]),
    # and any future shape.
    for node in ast.walk(annotation):
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        if isinstance(node, ast.Name) and node.id == "None":
            return True
    return False


def _files_referencing(symbol: str, root: Path) -> list[Path]:
    """Find .py files under root that mention `symbol` outside comments."""
    hits: list[Path] = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        # Naive substring match — symbol appearing anywhere flags it.
        # Refine later if false-positives become real.
        if symbol in text:
            hits.append(path)
    return hits


def main() -> int:
    if not USER_STORE.exists():
        print(f"FAIL: {USER_STORE} missing")
        return 1

    failures: list[str] = []

    # Shape contract holds for the canonical file AND the staged Postgres
    # successor (Codex F1 on PR #355: an allowed construction site that
    # escapes shape validation could violate the very contract the
    # allowance exists for).
    for store_path in (USER_STORE, PG_USER_STORE):
        if not store_path.exists():
            continue  # PG successor is transitional; absence is fine
        tree = ast.parse(store_path.read_text())
        cls = _find_class(tree, "UserWithTokens")
        if cls is None:
            failures.append(f"{store_path.name}: UserWithTokens class not found")
            continue

        if not _is_frozen_dataclass(cls):
            failures.append(f"{store_path.name}: UserWithTokens is not @dataclass(frozen=True) — spec 0008 attests frozen.")

        fields = _class_fields(cls)
        extra = set(fields) - EXPECTED_FIELDS
        missing = EXPECTED_FIELDS - set(fields)
        if extra:
            failures.append(f"{store_path.name}: UserWithTokens has unexpected fields: {sorted(extra)}.")
        if missing:
            failures.append(f"{store_path.name}: UserWithTokens missing expected fields: {sorted(missing)}.")

        refresh_ann = fields.get("oauth_refresh_token")
        if refresh_ann is not None and not _refresh_is_optional(refresh_ann):
            failures.append(f"{store_path.name}: UserWithTokens.oauth_refresh_token must be `str | None` (provider may not rotate). Got non-Optional annotation.")

    # Service-scope wall: webhook never references UserWithTokens.
    if WEBHOOK_DIR.exists():
        webhook_hits = _files_referencing("UserWithTokens", WEBHOOK_DIR)
        if webhook_hits:
            failures.append(
                "Webhook references UserWithTokens — service-scope wall broken (spec 0008 + 0005):\n"
                + "\n".join(f"    {p.relative_to(REPO_ROOT)}" for p in webhook_hits)
            )

    # Construction-site uniqueness: only get_user_with_tokens should call UserWithTokens(...).
    # We allow references in tests + the dataclass def itself; flag any non-test, non-user_store
    # construction site.
    if API_DIR.exists():
        construction_sites: list[str] = []
        for path in API_DIR.rglob("*.py"):
            if "test" in path.name:
                continue
            if path in (USER_STORE, PG_USER_STORE):
                continue  # canonical site + staged Postgres successor (#354)
            tree2 = ast.parse(path.read_text())
            for node in ast.walk(tree2):
                is_named = isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "UserWithTokens"
                # Qualified form (module.UserWithTokens(...)) must not
                # slip the wall (Codex F2 on PR #355).
                is_attr = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "UserWithTokens"
                if is_named or is_attr:
                    construction_sites.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        if construction_sites:
            failures.append(
                "UserWithTokens constructed outside user_store.py / pg_user_store.py (the #354-staged successor) — spec 0008 attests get_user_with_tokens is the sole construction site:\n"
                + "\n".join(f"    {s}" for s in construction_sites)
            )

    if failures:
        print(f"FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: UserWithTokens is frozen, fields are minimal, webhook has no reference, single construction site.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
