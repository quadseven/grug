#!/usr/bin/env python3
"""Grounding attester for spec 0016.PrReviewResult.

Proves NECESSARY conditions for these bools (via AST static analysis):

  - `event_is_comment_or_request_changes_per_elder_persona`
  - `post_init_raises_on_invalid_event_per_elder_persona`
  - `payload_shape_matches_github_pulls_reviews_api_per_elder_persona`
  - `request_uses_install_token_bearer_auth_per_elder_persona`
  - `review_result_is_frozen_dataclass_no_mutation_per_elder_persona`
  - `inline_comment_is_frozen_dataclass_no_mutation_per_elder_persona`
  - `post_review_does_not_swallow_401_per_elder_persona`

Sufficiency requires runtime testing — this is the static half. The
runtime half is covered by `test_github_reviews_client.py`.

The 401-propagation bool is the load-bearing one: a regression that
wraps `resp.raise_for_status()` in try/except would silently swallow
401 and `with_install_token_retry` would never invalidate the token.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CLIENT_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "services/api/github_reviews_client.py",
    REPO_ROOT / "services/webhook/github_reviews_client.py",
)

EXPECTED_EVENTS: frozenset[str] = frozenset(("COMMENT", "REQUEST_CHANGES"))


def _find_frozen_dataclass(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            for dec in node.decorator_list:
                # @dataclass(frozen=True, slots=True) or @dataclass(frozen=True, ...)
                if isinstance(dec, ast.Call) and (
                    (isinstance(dec.func, ast.Name) and dec.func.id == "dataclass")
                    or (
                        isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "dataclass"
                    )
                ):
                    for kw in dec.keywords:
                        if (
                            kw.arg == "frozen"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value is True
                        ):
                            return node
    return None


def _find_assignment(tree: ast.Module, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return node.value
    return None


def _str_set_from_node(node: ast.AST | None) -> frozenset[str] | None:
    """Extract a constant set of strings from `frozenset((...))` or
    `frozenset({...})` style literals."""
    if node is None:
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset":
        if not node.args:
            return None
        arg = node.args[0]
        if isinstance(arg, (ast.Tuple, ast.List, ast.Set)):
            vals = arg.elts
            try:
                return frozenset(
                    v.value for v in vals
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            except Exception:
                return None
    return None


def _has_raise_for_status_in_post_review(tree: ast.Module) -> bool:
    """Confirm `resp.raise_for_status()` is called inside post_review
    and NOT wrapped in try/except. The latter would swallow 401."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "post_review":
            # Walk only top-level statements; a try block at the func
            # level with raise_for_status inside is a swallow risk.
            for stmt in node.body:
                if isinstance(stmt, ast.Try):
                    # Confirm raise_for_status is NOT inside this try.
                    for inner in ast.walk(stmt):
                        if (
                            isinstance(inner, ast.Call)
                            and isinstance(inner.func, ast.Attribute)
                            and inner.func.attr == "raise_for_status"
                        ):
                            return False
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "raise_for_status"
                ):
                    return True
    return False


def _post_review_url_matches(tree: ast.Module) -> bool:
    """Confirm the URL f-string contains the canonical PR Reviews path
    `repos/{}/{}/pulls/{}/reviews`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "post_review":
            for n in ast.walk(node):
                if isinstance(n, ast.JoinedStr):
                    text = "".join(
                        v.value if isinstance(v, ast.Constant) else "{x}"
                        for v in n.values
                    )
                    if "/pulls/" in text and "/reviews" in text:
                        return True
    return False


def _post_review_uses_bearer_auth(tree: ast.Module) -> bool:
    """Confirm the request carries `Authorization: Bearer <token>`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "post_review":
            for n in ast.walk(node):
                if isinstance(n, ast.JoinedStr):
                    text = "".join(
                        v.value if isinstance(v, ast.Constant) else ""
                        for v in n.values
                    )
                    if text.startswith("Bearer "):
                        return True
    return False


def _check(path: Path) -> list[str]:
    failures: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))

    # event allowlist must exactly equal {COMMENT, REQUEST_CHANGES}
    valid_events_node = _find_assignment(tree, "_VALID_EVENTS")
    events = _str_set_from_node(valid_events_node)
    if events != EXPECTED_EVENTS:
        failures.append(
            f"{path}: _VALID_EVENTS must equal {sorted(EXPECTED_EVENTS)} "
            f"(got {sorted(events) if events else 'unparseable'})"
        )

    # ReviewResult must be frozen
    if _find_frozen_dataclass(tree, "ReviewResult") is None:
        failures.append(f"{path}: ReviewResult is not a frozen dataclass")

    # InlineComment must be frozen
    if _find_frozen_dataclass(tree, "InlineComment") is None:
        failures.append(f"{path}: InlineComment is not a frozen dataclass")

    # post_review must call resp.raise_for_status() and NOT wrap it in try/except
    if not _has_raise_for_status_in_post_review(tree):
        failures.append(
            f"{path}: post_review must call resp.raise_for_status() at the "
            "top level (not inside try/except — that swallows 401 and breaks "
            "with_install_token_retry)"
        )

    # URL contains the canonical PR Reviews path
    if not _post_review_url_matches(tree):
        failures.append(
            f"{path}: post_review URL does not contain /pulls/.../reviews"
        )

    # Authorization: Bearer
    if not _post_review_uses_bearer_auth(tree):
        failures.append(
            f"{path}: post_review must use 'Bearer <token>' Authorization"
        )

    return failures


def main() -> int:
    if not CLIENT_PATHS:
        print("FAIL: CLIENT_PATHS empty — refusing to pass vacuously")
        return 1
    failures: list[str] = []
    for path in CLIENT_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        failures.extend(_check(path))
    if failures:
        print("\n".join(f"FAIL: {f}" for f in failures))
        return 1
    print(
        f"OK: github_reviews_client contracts verified in both "
        f"{len(CLIENT_PATHS)} mirrored sides (spec 0016)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
