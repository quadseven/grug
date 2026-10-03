#!/usr/bin/env python3
"""Coherence attester: the backend persona registry, the API's writable
repo-config surface, and the dashboard's persona list must not drift.

Motivated by a live audit (2026-07-24, grug#743 follow-up) that found
THREE independent drifts, all silent, all in the "looks fine" direction:

  1. `sentinel` shipped registered + `enabled_default=True` + actively
     posting, but was absent from the dashboard's hardcoded 7-persona
     array - invisible to every operator since it shipped.
  2. `reopen_watch_enabled` was added to the STORE only. It was missing
     from `RepoConfigPayload` and from `update_repo_config`'s
     `set_repo_config(...)` call, so the persona reading it could never
     be enabled by any means - dead code on arrival. Worse, because the
     payload model sets `extra="forbid"`, an operator sending the flag
     got a 422 rather than any hint the field was unwired.
  3. `dep_watch_enabled` was writable through the API but had no UI.

The shared root cause: three surfaces (registry / API payload / SPA) each
hardcode their own list, and nothing forces them to agree. Discipline
cannot catch this - only a machine check that fails loudly can.

What this asserts (NECESSARY conditions, deliberately not sufficient):

  A. Every persona key in `personas/registry.py` whose `enabled_flag` is
     a real flag has that flag declared on `RepoConfigPayload` AND
     forwarded in `update_repo_config`'s `set_repo_config(...)` call.
  B. Every `_EXTRA_REPO_FLAGS` entry in the store (dep_watch_enabled,
     reopen_watch_enabled, ...) is likewise declared AND forwarded.
  C. Every registry persona appears in the dashboard's PERSONAS array,
     via an explicit registry-key -> UI-id map kept here (the UI uses
     display names: tpm->chief, code_reviewer->elder, walkthrough->teller).

Intentionally NOT asserted: that the dashboard's BLOCK/WARN/OFF controls
actually persist to the backend. They currently do NOT (they are
localStorage-only; only tpm_enabled/guard_enabled are wired to the API).
That is a real, separate gap tracked on its own - pinning it here would
either fail permanently or bless the current behavior. This attester
covers PRESENCE, not persistence.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

REGISTRY = REPO_ROOT / "services/_shared/personas/registry.py"
STORE = REPO_ROOT / "services/_shared/adapters/pg_install_store.py"
API = REPO_ROOT / "services/api/installations.py"
DASHBOARD = REPO_ROOT / "web/src/routes/Dashboard.tsx"

# Registry key -> the id the dashboard's PERSONAS array uses. The UI is
# named for the CHARACTER, the registry for the CAPABILITY, so these
# genuinely differ and the map has to be explicit. A registry key absent
# from this map is itself a failure (forces a conscious decision when a
# new persona lands, instead of silently shipping it invisible).
REGISTRY_KEY_TO_UI_ID: dict[str, str] = {
    "tpm": "chief",
    "code_reviewer": "elder",
    "guard": "guard",
    "warder": "warder",
    "sentinel": "sentinel",
    "smasher": "smasher",
    "walkthrough": "teller",
    "pulse": "pulse",
    "pulse_comment_nudge": "pulse_comment_nudge",
}


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)


def _registry_enabled_flags(tree: ast.Module) -> dict[str, str]:
    """{registry_key: enabled_flag} for every PersonaSpec(...) call."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "PersonaSpec"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        key_node, flag_node = kw.get("key"), kw.get("enabled_flag")
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            continue
        if isinstance(flag_node, ast.Constant) and isinstance(flag_node.value, str):
            out[key_node.value] = flag_node.value
    return out


def _extra_repo_flags(tree: ast.Module) -> set[str]:
    """The store's `_EXTRA_REPO_FLAGS = frozenset({...})` literal."""
    for stmt in tree.body:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == "_EXTRA_REPO_FLAGS"):
            continue
        for node in ast.walk(stmt.value):
            if isinstance(node, ast.Set):
                return {
                    e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
    return set()


def _payload_fields(tree: ast.Module) -> set[str]:
    """Annotated field names on `class RepoConfigPayload`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RepoConfigPayload":
            return {
                s.target.id for s in node.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            }
    return set()


def _set_repo_config_forwarding(tree: ast.Module) -> dict[str, str | None]:
    """{keyword: source_attr} for set_repo_config(...) inside
    update_repo_config - the call that actually persists the flag.

    Records the VALUE, not just the keyword (FLINT, PR #751): keyword
    presence alone would let `set_repo_config(sentinel_enabled=body.guard_enabled)`
    - or a hardcoded constant - pass CI while the flag stays effectively
    unsettable. `source_attr` is the attribute name for a `body.<attr>`
    expression, or None for anything else (which fails the match below).
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "update_repo_config"):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "set_repo_config"):
                continue
            out: dict[str, str | None] = {}
            for k in call.keywords:
                if not k.arg:
                    continue
                v = k.value
                out[k.arg] = (
                    v.attr
                    if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
                    and v.value.id == "body"
                    else None
                )
            return out
    return {}


def _dashboard_persona_ids(text: str) -> set[str]:
    """Ids in the dashboard's PERSONAS array. The array is a TSX literal,
    so this is a regex over `{ id: "...", code: "F-NN"` entries - the
    `code:` neighbour anchors it to the persona array specifically and
    keeps it from matching the skins/notifications arrays."""
    return set(re.findall(r'\{\s*id:\s*"([a-z_]+)",\s*code:\s*"F-\d+"', text))


def _check_sources_parsed(
    registry_flags: dict[str, str], extra_flags: set[str], payload: set[str],
    forwarded: dict[str, str | None], ui_ids: set[str],
) -> list[str]:
    """Every extractor must have found SOMETHING. An extractor silently
    returning empty (refactor moved a symbol, regex stopped matching) would
    otherwise make every downstream loop vacuously pass."""
    empties = [
        (not registry_flags, f"no PersonaSpec(key=..., enabled_flag=...) in {REGISTRY}"),
        (not extra_flags, f"no _EXTRA_REPO_FLAGS frozenset in {STORE}"),
        (not payload, f"no RepoConfigPayload fields in {API}"),
        (not forwarded, f"no set_repo_config(...) call in update_repo_config in {API}"),
        (not ui_ids, f"no PERSONAS array entries in {DASHBOARD}"),
    ]
    return [msg for is_empty, msg in empties if is_empty]


def _check_flags_reachable(
    flags: set[str], payload: set[str], forwarded: dict[str, str | None],
) -> list[str]:
    """Every store-backed flag must be declared on the payload AND forwarded
    to set_repo_config from the matching body field."""
    out: list[str] = []
    api_rel = API.relative_to(REPO_ROOT)
    for flag in sorted(flags):
        if flag not in payload:
            out.append(
                f"repo-config flag `{flag}` is not declared on RepoConfigPayload "
                f"({api_rel}) - it cannot be set through the API (and `extra=forbid` "
                f"makes sending it a 422)"
            )
        if flag not in forwarded:
            out.append(
                f"repo-config flag `{flag}` is not forwarded to set_repo_config(...) "
                f"in update_repo_config ({api_rel}) - the API would accept it and "
                f"silently drop it"
            )
        elif forwarded[flag] != flag:
            src = forwarded[flag]
            out.append(
                f"repo-config flag `{flag}` is forwarded from "
                f"`{'body.' + src if src else 'a non-body expression'}` rather than "
                f"`body.{flag}` ({api_rel}) - the field would be accepted but the "
                f"wrong value (or a constant) persisted"
            )
    return out


def _check_persona_visibility(
    registry_flags: dict[str, str], ui_ids: set[str],
) -> list[str]:
    """Registry <-> dashboard, both directions."""
    out: list[str] = []
    dash_rel = DASHBOARD.relative_to(REPO_ROOT)
    for key in sorted(registry_flags):
        ui_id = REGISTRY_KEY_TO_UI_ID.get(key)
        if ui_id is None:
            out.append(
                f"registry persona `{key}` has no REGISTRY_KEY_TO_UI_ID entry in this "
                f"attester - add the mapping AND the dashboard entry, or the persona "
                f"ships invisible to operators"
            )
        elif ui_id not in ui_ids:
            out.append(
                f"registry persona `{key}` (ui id `{ui_id}`) is missing from the "
                f"PERSONAS array in {dash_rel} - it runs but no operator can see it"
            )
    # Reverse: a UI entry with no LIVE registry backing is a ghost. Built from
    # keys actually discovered in the registry, NOT from the whole mapping -
    # a stale mapping entry for a deleted persona would otherwise keep
    # blessing its orphaned dashboard card (FLINT, PR #751).
    live_ui_ids = {
        REGISTRY_KEY_TO_UI_ID[k] for k in registry_flags if k in REGISTRY_KEY_TO_UI_ID
    }
    for ui_id in sorted(ui_ids - live_ui_ids):
        out.append(
            f"dashboard persona `{ui_id}` has no live registry backing - it is shown "
            f"to operators but nothing dispatches it"
        )
    return out


def main() -> int:
    registry_flags = _registry_enabled_flags(ast.parse(REGISTRY.read_text()))
    extra_flags = _extra_repo_flags(ast.parse(STORE.read_text()))
    api_tree = ast.parse(API.read_text())
    payload = _payload_fields(api_tree)
    forwarded = _set_repo_config_forwarding(api_tree)
    ui_ids = _dashboard_persona_ids(DASHBOARD.read_text())

    failures = _check_sources_parsed(
        registry_flags, extra_flags, payload, forwarded, ui_ids,
    )
    all_flags = set(registry_flags.values()) | extra_flags
    failures += _check_flags_reachable(all_flags, payload, forwarded)
    failures += _check_persona_visibility(registry_flags, ui_ids)

    if failures:
        for f in failures:
            _fail(f)
        return 1

    print(
        f"OK: {len(registry_flags)} registry personas, {len(all_flags)} repo-config "
        f"flags - registry, API payload, store forwarding, and dashboard all coherent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
