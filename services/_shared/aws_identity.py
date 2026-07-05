"""Roles Anywhere identity proof - shared by every grug workload (#389).

Moved from poller_handler (#388) when the rollout took the remaining
workloads onto the cert path. Each entrypoint calls this ONCE at startup
(FastAPI mains at import, consumer in _startup_check, poller at handler
start). Gated on AWS_CONFIG_FILE - the marker the manifests set for the
credential_process path; local/test/image-smoke runs without it skip.

Deliberately UNGUARDED by callers (a failure crashes the pod/Job):
- services fail BEFORE serving, so the deploy's rollout gate catches a
  broken credential path at deploy time, and a steady-state break
  surfaces as CrashLoopBackOff -> the KSM monitors page;
- the poller Job fails -> duration_since_last_successful pages.

Every failure path logs `roles_anywhere_identity_failed` FIRST - the
greppable event the credential-acquisition DD monitor (#389) alerts on -
then raises.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.aws_identity")


def prove_roles_anywhere_identity() -> None:
    """Fail-LOUD credential proof (#388 tracer, #389 fleet-wide).

    1. Static env creds present alongside the RA config = the SDK chain
       is silently bypassing the cert path (env creds out-rank
       credential_process). Refuse to run.
    2. sts get-caller-identity, ASSERTED against GRUG_RA_ROLE_ARN (a
       wrong-but-valid credential source must fail, not pass
       observationally - peer review on #388, confirmed 3x).
    """
    if not os.getenv("AWS_CONFIG_FILE"):
        return
    try:
        if os.getenv("AWS_ACCESS_KEY_ID"):
            raise RuntimeError(
                "static AWS creds present in the pod env - the Roles Anywhere "
                "path is being bypassed (#388/#389); see RUNBOOK 'Roles Anywhere'"
            )
        import boto3

        ident = boto3.client("sts").get_caller_identity()
        arn = ident.get("Arn", "")
        expected_role_arn = os.getenv("GRUG_RA_ROLE_ARN", "")
        if expected_role_arn:
            account = expected_role_arn.split(":")[4]
            role_name = expected_role_arn.rsplit("/", 1)[-1]
            expected_prefix = f"arn:aws:sts::{account}:assumed-role/{role_name}/"
            if not arn.startswith(expected_prefix):
                raise RuntimeError(
                    f"wrong AWS identity on the Roles Anywhere path: got {arn!r}, "
                    f"expected an assumed-role session of {role_name!r} (#388)"
                )
    except Exception as e:
        # The monitorable event FIRST (the #389 credential-acquisition
        # monitor keys on this name), then fail loud.
        log.error(
            "roles_anywhere_identity_failed",
            extra={"kind": type(e).__name__},
            exc_info=True,
        )
        raise
    log.info(
        "roles_anywhere_identity_proven",
        extra={"assumed_arn": arn, "identity_asserted": bool(expected_role_arn)},
    )
