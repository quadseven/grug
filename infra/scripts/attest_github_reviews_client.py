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


def _str_set_from_node(
    node: ast.AST | None, tree: ast.Module,
) -> frozenset[str] | None:
    """Extract a constant set of strings from `frozenset((...))`,
    `frozenset({...})`, or `frozenset(get_args(<Literal>))` style
    expressions. The `get_args(Literal[...])` form is what we recommend
    in code-reviewer fixes, so the attester must follow it back to the
    Literal's args."""
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
        # `get_args(<Name>)` — resolve to the Literal's args at the
        # named alias (e.g. `ReviewEvent = Literal["COMMENT", ...]`).
        if (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id == "get_args"
            and arg.args
            and isinstance(arg.args[0], ast.Name)
        ):
            return _literal_args(tree, arg.args[0].id)
    return None


def _literal_args(tree: ast.Module, name: str) -> frozenset[str] | None:
    """Resolve `<name> = Literal["a", "b"]` to {"a", "b"}."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    val = node.value
                    if (
                        isinstance(val, ast.Subscript)
                        and isinstance(val.value, ast.Name)
                        and val.value.id == "Literal"
                    ):
                        slice_node = val.slice
                        elts = (
                            slice_node.elts if isinstance(slice_node, ast.Tuple)
                            else [slice_node]
                        )
                        try:
                            return frozenset(
                                e.value for e in elts
                                if isinstance(e, ast.Constant)
                                and isinstance(e.value, str)
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


# GitHub's create-review API requires these top-level keys (event +
# commit_id + body + comments) plus inline-comment objects with
# {path, line, body}. The dataclass field names must match exactly —
# `asdict(result)` only produces the right shape if the field names
# are exactly these.
EXPECTED_REVIEW_FIELDS: frozenset[str] = frozenset(
    ("commit_id", "event", "body", "comments")
)
EXPECTED_INLINE_COMMENT_FIELDS: frozenset[str] = frozenset(
    ("path", "line", "body")
)


def _dataclass_field_names(class_def: ast.ClassDef) -> frozenset[str]:
    """Extract the field names declared as `name: type [= default]` in a
    dataclass body — skips methods and class-level constants."""
    names: set[str] = set()
    for node in class_def.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(names)


def _post_review_uses_asdict(tree: ast.Module) -> bool:
    """Confirm `httpx.post(... json=asdict(result) ...)` — the codex
    peer-review finding pointed out that the URL check alone doesn't
    prove the payload shape. Verify `asdict(...)` is the value passed
    to `json=` on the httpx.post call inside post_review."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "post_review":
            for n in ast.walk(node):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "post"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "httpx"
                ):
                    for kw in n.keywords:
                        if (
                            kw.arg == "json"
                            and isinstance(kw.value, ast.Call)
                            and isinstance(kw.value.func, ast.Name)
                            and kw.value.func.id == "asdict"
                        ):
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
    events = _str_set_from_node(valid_events_node, tree)
    if events != EXPECTED_EVENTS:
        failures.append(
            f"{path}: _VALID_EVENTS must equal {sorted(EXPECTED_EVENTS)} "
            f"(got {sorted(events) if events else 'unparseable'})"
        )

    # ReviewResult must be frozen + carry the GH-required field names.
    # `asdict()` only produces a valid GH payload if these names match
    # the API's keys exactly. A field rename (e.g. `commit_id` → `sha`)
    # would silently 422 in production — catch at PR time.
    review_class = _find_frozen_dataclass(tree, "ReviewResult")
    if review_class is None:
        failures.append(f"{path}: ReviewResult is not a frozen dataclass")
    else:
        fields = _dataclass_field_names(review_class)
        if fields != EXPECTED_REVIEW_FIELDS:
            failures.append(
                f"{path}: ReviewResult fields must equal "
                f"{sorted(EXPECTED_REVIEW_FIELDS)} for asdict() to produce "
                f"the GH-required payload (got {sorted(fields)})"
            )

    inline_class = _find_frozen_dataclass(tree, "InlineComment")
    if inline_class is None:
        failures.append(f"{path}: InlineComment is not a frozen dataclass")
    else:
        fields = _dataclass_field_names(inline_class)
        if fields != EXPECTED_INLINE_COMMENT_FIELDS:
            failures.append(
                f"{path}: InlineComment fields must equal "
                f"{sorted(EXPECTED_INLINE_COMMENT_FIELDS)} for asdict() to "
                f"produce GH's comments[] shape (got {sorted(fields)})"
            )

    # `httpx.post` must pass `json=asdict(result)` — the URL+auth check
    # alone doesn't prove the payload shape. A regression that swapped
    # to manual dict marshalling could rename a key undetected.
    if not _post_review_uses_asdict(tree):
        failures.append(
            f"{path}: post_review must call httpx.post(... json=asdict(result) ...) "
            "so the payload shape is locked to the dataclass field names. "
            "Manual marshalling reintroduces the drift class this attester guards against."
        )

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
