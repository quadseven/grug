#!/usr/bin/env python3
"""Grounding attester for spec 0013.RumInstrumentation.

Proves NECESSARY conditions for the bools:

  - `rum_application_declared_in_pulumi_per_iac_invariant`
  - `rum_credentials_exported_to_ssm_per_secret_handling_invariant`

Asserts that `infra/pulumi/__main__.py` AND `infra/pulumi/components/dd_rum.py`
together:

  1. Define a `datadog.RumApplication` resource.
  2. Pass `name="grug-web"` (the canonical service tag — spec 0013).
  3. Use `type="browser"` (catches accidental drift to ios/android/etc).
  4. Set `rum_event_processing_state="ALL"` (anything else drops event
     types we want to capture).
  5. Export `app.id` to SSM at name `/grug/dd-rum-application-id`.
  6. Export `app.client_token` to SSM at name `/grug/dd-rum-client-token`.
  7. Both SSM params wrap their value in `pulumi.Output.secret(...)`
     (per `feedback_pulumi_preview_secret_leak_guard` — unwrapped
     secret reads leaked the DD APP key in preview 2026-05-17).

Stdlib only — AST parse of the two Python modules.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "infra/pulumi/__main__.py"
COMPONENT_PY = REPO_ROOT / "infra/pulumi/components/dd_rum.py"

CANONICAL_SERVICE_NAME = "grug-web"
CANONICAL_TYPE = "browser"
CANONICAL_PROCESSING_STATE = "ALL"
SSM_APP_ID_PATH = "/grug/dd-rum-application-id"
SSM_CLIENT_TOKEN_PATH = "/grug/dd-rum-client-token"


def _kwarg_value(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _str_value(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # `f"/grug/dd-rum-application-id"` parses as ast.JoinedStr with one Str.
    if isinstance(node, ast.JoinedStr) and len(node.values) == 1:
        return _str_value(node.values[0])
    return None


def _is_output_secret(node: ast.AST | None) -> bool:
    """True if `node` is a call to `pulumi.Output.secret(...)`."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if (
        isinstance(fn, ast.Attribute)
        and fn.attr == "secret"
        and isinstance(fn.value, ast.Attribute)
        and fn.value.attr == "Output"
    ):
        return True
    return False


def _walk_calls(tree: ast.AST, target_attr: str) -> list[ast.Call]:
    """All calls of shape `<module>.<target_attr>(...)`."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == target_attr:
            out.append(node)
    return out


def _attest_rum_application(failures: list[str]) -> None:
    if not COMPONENT_PY.exists():
        failures.append(f"{COMPONENT_PY.relative_to(REPO_ROOT)} missing — dd_rum component absent")
        return
    tree = ast.parse(COMPONENT_PY.read_text())
    apps = _walk_calls(tree, "RumApplication")
    if not apps:
        failures.append(
            f"{COMPONENT_PY.relative_to(REPO_ROOT)}: no `datadog.RumApplication(...)` call found"
        )
        return
    if len(apps) > 1:
        failures.append(
            f"{COMPONENT_PY.relative_to(REPO_ROOT)}: {len(apps)} RumApplication calls — expected exactly 1"
        )
        return
    app = apps[0]

    name = _str_value(_kwarg_value(app, "name"))
    if name != CANONICAL_SERVICE_NAME:
        # The component takes name as a parameter; the canonical value
        # must be passed at the call site in __main__.py. Defer to the
        # __main__ check below.
        pass

    type_arg = _str_value(_kwarg_value(app, "type"))
    if type_arg != CANONICAL_TYPE:
        failures.append(
            f"{COMPONENT_PY.relative_to(REPO_ROOT)}: RumApplication type={type_arg!r}, "
            f"expected {CANONICAL_TYPE!r} (other types capture wrong event shape)"
        )

    state = _str_value(_kwarg_value(app, "rum_event_processing_state"))
    if state != CANONICAL_PROCESSING_STATE:
        failures.append(
            f"{COMPONENT_PY.relative_to(REPO_ROOT)}: rum_event_processing_state={state!r}, "
            f"expected {CANONICAL_PROCESSING_STATE!r} (anything else drops captured event types)"
        )


def _attest_ssm_exports(failures: list[str]) -> None:
    """Both SSM params present + both wrapped in pulumi.Output.secret()."""
    tree = ast.parse(COMPONENT_PY.read_text())
    ssm_calls = _walk_calls(tree, "Parameter")
    seen: dict[str, ast.Call] = {}
    for call in ssm_calls:
        name = _str_value(_kwarg_value(call, "name"))
        if name in (SSM_APP_ID_PATH, SSM_CLIENT_TOKEN_PATH):
            seen[name] = call

    for path in (SSM_APP_ID_PATH, SSM_CLIENT_TOKEN_PATH):
        if path not in seen:
            failures.append(
                f"{COMPONENT_PY.relative_to(REPO_ROOT)}: no `aws.ssm.Parameter(name={path!r}, ...)` call"
            )
            continue
        call = seen[path]
        # type=SecureString
        type_arg = _str_value(_kwarg_value(call, "type"))
        if type_arg != "SecureString":
            failures.append(
                f"{COMPONENT_PY.relative_to(REPO_ROOT)}: SSM param {path!r} type={type_arg!r}, "
                f"expected 'SecureString' (RUM credentials, masking matters)"
            )
        # value=pulumi.Output.secret(...)
        if not _is_output_secret(_kwarg_value(call, "value")):
            failures.append(
                f"{COMPONENT_PY.relative_to(REPO_ROOT)}: SSM param {path!r} value is not wrapped in "
                f"`pulumi.Output.secret(...)` — secret would leak to preview output "
                f"(per feedback_pulumi_preview_secret_leak_guard)"
            )


def _attest_main_callsite(failures: list[str]) -> None:
    """__main__.py must call dd_rum.create(name='grug-web', ...)."""
    if not MAIN_PY.exists():
        failures.append(f"{MAIN_PY.relative_to(REPO_ROOT)} missing")
        return
    tree = ast.parse(MAIN_PY.read_text())
    creates = _walk_calls(tree, "create")
    rum_creates = [
        c for c in creates
        if isinstance(c.func, ast.Attribute)
        and isinstance(c.func.value, ast.Name)
        and c.func.value.id == "dd_rum"
    ]
    if not rum_creates:
        failures.append(
            f"{MAIN_PY.relative_to(REPO_ROOT)}: no `dd_rum.create(...)` call found"
        )
        return
    if len(rum_creates) > 1:
        failures.append(
            f"{MAIN_PY.relative_to(REPO_ROOT)}: {len(rum_creates)} dd_rum.create calls — expected exactly 1"
        )
    call = rum_creates[0]
    name = _str_value(_kwarg_value(call, "name"))
    if name != CANONICAL_SERVICE_NAME:
        failures.append(
            f"{MAIN_PY.relative_to(REPO_ROOT)}: dd_rum.create name={name!r}, "
            f"expected {CANONICAL_SERVICE_NAME!r} (canonical service tag, "
            f"spec 0013 rum_service_tag_is_grug_web_canonical_per_dd_naming_canon)"
        )


def main() -> int:
    failures: list[str] = []
    _attest_rum_application(failures)
    _attest_ssm_exports(failures)
    _attest_main_callsite(failures)

    if failures:
        print(f"FAIL: RUM Pulumi registration drift ({len(failures)} issues):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"OK: dd_rum.create() registered + name={CANONICAL_SERVICE_NAME!r} + "
        f"type={CANONICAL_TYPE!r} + state={CANONICAL_PROCESSING_STATE!r} + "
        f"both SSM params (Output.secret-wrapped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
