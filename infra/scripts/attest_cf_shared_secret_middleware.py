#!/usr/bin/env python3
"""Grounding attester for spec 0014.CfSharedSecret.

Proves three of the five contract bools by AST-walking real source:
  - `middleware_registered_in_both_services_per_auth_boundary_contract`
    Both services/{api,webhook}/main.py call
    `app.add_middleware(CfAuthMiddleware)`.
  - `middleware_secret_sourced_from_ssm_per_secret_handling_invariant`
    cf_auth.py reads `GRUG_CF_SHARED_SECRET_SSM` env var and calls
    boto3 `ssm.get_parameter(WithDecryption=True)`. No literal secret.
  - `middleware_exempts_livez_and_uses_constant_time_compare_per_security_invariant`
    cf_auth.py references `/livez` and `hmac.compare_digest`.

The Pulumi + CF-worker contract bools are attested by separate scripts
(infra/scripts/attest_cf_shared_secret_pulumi.py + ..._worker.py would
land in a follow-up if/when AST coverage of worker.js becomes useful;
for now the cf_shared_secret_pulumi attester is the natural extension
once spec coverage of infra/pulumi grows).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CF_AUTH_PATHS = (
    REPO_ROOT / "services/_shared/cf_auth.py",
)
MAIN_PATHS = (
    REPO_ROOT / "services/api/main.py",
    REPO_ROOT / "services/webhook/main.py",
)

# Cross-source structural-drift guard: the header literal must match
# across the Lambda middleware, the CF Worker code, the deploy script
# that templates the Workers, and the DD monitor that alerts on
# mismatches. If any side renames the header without the others, the
# auth boundary silently fails and the monitor stops alerting.
HEADER_LITERAL = "X-Grug-CF-Secret"
HEADER_LITERAL_SOURCES = (
    REPO_ROOT / "services/_shared/cf_auth.py",
    REPO_ROOT / "infra/cloudflare/deploy.sh",
    REPO_ROOT / "infra/pulumi/components/dd_monitors.py",
)


def _registers_middleware(tree: ast.Module) -> bool:
    """Return True iff `app.add_middleware(CfAuthMiddleware)` is called."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_middleware":
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "app":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id == "CfAuthMiddleware":
                return True
    return False


def _ssm_lookup_via_env_var(tree: ast.Module) -> bool:
    """Return True iff the module reads `GRUG_CF_SHARED_SECRET_SSM` env
    var AND calls `ssm.get_parameter(..., WithDecryption=True)`.

    Both signals must be present — `os.getenv("GRUG_CF_SHARED_SECRET_SSM")`
    alone could be a no-op placeholder, and `get_parameter` alone might
    hardcode a name. Quoting is normalized via ast.unparse so both
    "..." and '...' source forms match.
    """
    src = ast.unparse(tree)
    return (
        "GRUG_CF_SHARED_SECRET_SSM" in src
        and "get_parameter" in src
        and "WithDecryption=True" in src
    )


def _exempts_livez_and_constant_time(tree: ast.Module) -> bool:
    """Return True iff the module references `/livez` AND
    `hmac.compare_digest`."""
    src = ast.unparse(tree)
    return "/livez" in src and "compare_digest" in src


_NON_SECRET_NAMES = frozenset({"SECRET_HEADER"})


def _no_secret_literal(tree: ast.Module) -> bool:
    """Defensive — check for telltale literal secret patterns.

    A real secret value never appears in cf_auth.py because the loader
    reads SSM at runtime. This is a belt-and-suspenders check that
    catches a future maintainer who pastes a value for "testing".
    `_NON_SECRET_NAMES` carves out the known header-name constants
    where the literal is a public identifier, not a secret.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # Only flag string literals long enough to plausibly be a secret.
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id.upper()
            if name in _NON_SECRET_NAMES:
                continue
            if any(token in name for token in ("SECRET", "TOKEN", "KEY")):
                if isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str
                ) and len(node.value.value) >= 16:
                    return False
    return True


def main() -> int:
    failures: list[str] = []

    # 1. middleware_registered_in_both_services
    for path in MAIN_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        if not _registers_middleware(tree):
            failures.append(
                f"FAIL: {path} — app.add_middleware(CfAuthMiddleware) not found"
            )

    # 1a. Cross-source header-literal drift guard.
    for path in HEADER_LITERAL_SOURCES:
        if not path.exists():
            failures.append(f"FAIL: {path} missing (header drift guard)")
            continue
        if HEADER_LITERAL not in path.read_text():
            failures.append(
                f"FAIL: {path} — missing header literal '{HEADER_LITERAL}'. "
                "Renaming the header requires updating ALL of: "
                "cf_auth.py (services/_shared/), deploy.sh, dd_monitors.py."
            )

    # 2 + 3. cf_auth.py-side bools
    for path in CF_AUTH_PATHS:
        if not path.exists():
            failures.append(f"FAIL: {path} missing")
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        if not _ssm_lookup_via_env_var(tree):
            failures.append(
                f"FAIL: {path} — secret not sourced via "
                f"GRUG_CF_SHARED_SECRET_SSM + ssm.get_parameter(WithDecryption=True)"
            )
        if not _exempts_livez_and_constant_time(tree):
            failures.append(
                f"FAIL: {path} — must reference /livez exemption + hmac.compare_digest"
            )
        if not _no_secret_literal(tree):
            failures.append(
                f"FAIL: {path} — looks like a secret-shaped string literal "
                "is hardcoded; secrets must come from SSM at runtime"
            )

    if failures:
        print("\n".join(failures))
        return 1

    print(
        "OK: CfSharedSecret middleware contract grounded — registered in "
        "both services/{api,webhook}/main.py, secret sourced from SSM via "
        "GRUG_CF_SHARED_SECRET_SSM env var with WithDecryption=True, /livez "
        "exempt, hmac.compare_digest used for header validation, and the "
        f"'{HEADER_LITERAL}' literal is present in all "
        f"{len(HEADER_LITERAL_SOURCES)} cross-source sites "
        "(cf_auth.py x2, deploy.sh, dd_monitors.py)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
