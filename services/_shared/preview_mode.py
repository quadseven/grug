"""Preview-environment mode gate (#500, ADR-0018).

A preview pod (namespace grug-pr-<n>) runs the real image but must have
NO access to production secrets or the Roles Anywhere identity - it
exercises the running code against its own throwaway Postgres schema.
preview_mode() lets the app skip the AWS-dependent boot/readiness paths
in that case.

SECURITY INVARIANT: preview mode activates ONLY when BOTH the
GRUG_PREVIEW flag is set AND the pod's own namespace (downward API,
POD_NAMESPACE) starts with `grug-pr-`. The namespace check is the
load-bearing guard: even if GRUG_PREVIEW leaked into the prod `grug`
namespace's env, preview mode would NOT engage there, so the RA identity
proof + SSM/KMS readiness that #388/#389 hardened can never be silently
disabled in production.
"""

from __future__ import annotations

import os

_PREVIEW_NS_PREFIX = "grug-pr-"


def preview_mode() -> bool:
    if os.getenv("GRUG_PREVIEW", "").lower() not in ("1", "true", "yes"):
        return False
    ns = os.getenv("POD_NAMESPACE", "")
    return ns.startswith(_PREVIEW_NS_PREFIX)
