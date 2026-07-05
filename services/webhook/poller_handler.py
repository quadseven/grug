"""Scheduled reaction-poll entry point (#247b).

The `grug-poller` Kubernetes CronJob invokes this every ~15 min (NOT the
webhook HTTP path — there's no FastAPI, no signature check; it runs
as a batch job). Per allowlisted install it polls 👍/👎 reactions on Grug
review comments and submits `human_verdict` DD LLM Obs evals — the human
ground-truth that calibrates the LLM judge.

Reuses the webhook container image (same `reactions` / `install_store` /
`llm_client` / `github_app_auth` code); the CronJob runs
`ddtrace-run python -c "from poller_handler import handler; handler({}, None)"`
(the `(event, context)` signature is a legacy of the EventBridge-scheduled
Lambda this CronJob replaced at the #354 cutover).

Best-effort by construction: one install's failure (GH 5xx, token error) logs
and continues — a single bad install must never abort the whole poll cycle.
The reaction engine itself dedups via `CommentRecord.last_verdict`, so a
stale verdict isn't re-submitted every cycle.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import delivery_replay
from adapters.install_store import (  # type: ignore
    list_allowlisted_installs,
    list_comment_records,
)
from github_app_auth import with_install_token_retry
from personas.code_reviewer.reactions import poll_and_annotate

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.poller")

# How far back each cron tick scans App webhook deliveries for missed events
# (#407). Generous by default - the guid dedup makes re-scanning a window
# harmless (a delivery that later succeeds is skipped), and pagination is
# window-bounded so this can't run away.
_REPLAY_WINDOW_HOURS = int(os.getenv("GRUG_REPLAY_WINDOW_HOURS", "6"))


def _replay_missed_deliveries() -> dict[str, int | str]:
    """Auto-recovery (#407): redeliver App webhook deliveries that errored in
    the recent window, so a check dropped while grug was down re-posts without
    a human re-triggering. Best-effort - a replay failure must never abort the
    reaction-poll cron, so the caller wraps this and it also self-guards."""
    since = (
        datetime.now(timezone.utc) - timedelta(hours=_REPLAY_WINDOW_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    rep = delivery_replay.replay_since(since)
    return {
        "replay_scanned": rep.scanned,
        "replay_failed_guids": rep.failed_guids,
        "replay_redelivered": rep.redelivered,
        "replay_errors": rep.errors,
    }


def _prove_roles_anywhere_identity() -> None:
    """Fail-LOUD credential proof for the #388 tracer (audit stage-2
    CRITICAL). Gated on AWS_CONFIG_FILE - the marker the poller manifest
    sets for the Roles Anywhere credential_process path; local/test runs
    without it skip entirely.

    Two checks, both deliberately UNGUARDED (a failure crashes the Job,
    which the existing KSM duration_since_last_successful monitor pages
    on within the hour - the per-install best-effort contract does NOT
    apply to process-global credentials):

    1. Static env creds present alongside the RA config = the SDK chain
       is silently bypassing the cert path (env creds out-rank
       credential_process): a rotator patching the wrong Secret, a
       manifest revert, or a seed regression. Refuse to run.
    2. sts get-caller-identity - the positive proof. The logged ARN is a
       Roles Anywhere session ARN on the cert path; this line runs every
       15m tick, which also makes the poller the de facto expiry canary
       for a silently-stuck Certificate renewal (a stale leaf dies here,
       loudly, instead of inside the per-install swallow).
    """
    if not os.getenv("AWS_CONFIG_FILE"):
        return
    if os.getenv("AWS_ACCESS_KEY_ID"):
        raise RuntimeError(
            "static AWS creds present in the poller env - the Roles Anywhere "
            "path is being bypassed (#388); see RUNBOOK 'Roles Anywhere'"
        )
    import boto3

    ident = boto3.client("sts").get_caller_identity()
    arn = ident.get("Arn", "")
    # Assert the INTENDED identity, not merely a working one (peer review,
    # confirmed 3x): a wrong-but-valid SSM ARN, a swapped profile, or any
    # ambient credential source that wins the chain must FAIL here, not
    # pass observationally. GRUG_RA_ROLE_ARN is sed-pinned at deploy from
    # the same SSM value the ConfigMap uses.
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
    log.info(
        "roles_anywhere_identity_proven",
        extra={"assumed_arn": arn, "identity_asserted": bool(expected_role_arn)},
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, int | str]:
    """Poll reactions for every allowlisted install. Returns a summary
    dict (installs scanned, records polled, verdicts submitted) — also
    the structured-log payload an operator/DD reads to confirm the cron
    ran end-to-end."""
    _prove_roles_anywhere_identity()
    installs = list_allowlisted_installs()
    polled_records = 0
    submitted = 0
    failed_installs = 0

    for install_id in installs:
        # The ENTIRE per-install body — the CommentRecord listing AND the
        # poll — is inside this try, so a DDB listing failure for one install
        # can't abort the cron either (best-effort per install).
        # `with_install_token_retry` is used here for its token ACQUISITION;
        # its 401-refresh path is intentionally unreachable from the poller —
        # `poll_and_annotate` catches per-record GH 401s internally (best-
        # effort), so none propagates back to trigger a refresh. A revoked
        # cached token therefore self-heals on a later cron tick once the
        # token-cache TTL expires, not mid-cycle. Acceptable for best-effort
        # calibration data; surfacing first-call 401s would be #245a engine
        # surgery for marginal benefit.
        try:
            records = list_comment_records(install_id)
            if not records:
                continue
            polled_records += len(records)
            submitted += with_install_token_retry(
                install_id,
                lambda token: poll_and_annotate(
                    records,
                    install_id=install_id,
                    fetch_token=lambda: token,
                ),
            ) or 0
        except Exception as e:  # noqa: BLE001 — per-install best-effort: one
            # install's listing/GH/token failure must not abort the cron cycle.
            log.warning(
                "reaction_poll_install_failed",
                extra={"install_id": install_id, "kind": type(e).__name__},
            )
            failed_installs += 1

    # Pulse pass (#472): the first SCHEDULED persona rides the same
    # cadence as its OWN loop (the reactions loop `continue`s installs
    # with no comment records - Pulse must still run there). Best-effort
    # per install; everything inside run_pulse_for_install is capped +
    # per-repo best-effort + store-claim idempotent.
    nudges = 0
    pulse_failed = 0
    for install_id in installs:
        try:
            from adapters.install_store import list_pulse_enabled_repos
            from personas.pulse.nudge import run_pulse_for_install

            # Store-driven targeting (codex PR #489): only repos the
            # operator ENABLED - no /installation/repositories paging, so
            # a large install can never starve an enabled repo behind a
            # discovery-page prefix, and idle ticks cost zero GH calls.
            repos = list_pulse_enabled_repos(install_id)
            if not repos:
                continue
            nudges += with_install_token_retry(
                install_id,
                lambda token, iid=install_id, r=repos: run_pulse_for_install(token, iid, r),
            ) or 0
        except Exception as e:  # noqa: BLE001 — one install must not abort the cron
            log.warning(
                "pulse_install_failed",
                extra={"install_id": install_id, "kind": type(e).__name__},
            )
            pulse_failed += 1

    # Guard dependency watch (#491): the owned dependabot-class pass -
    # same store-driven, best-effort shape as Pulse.
    dep_reports = 0
    dep_watch_failed = 0
    for install_id in installs:
        try:
            from adapters.install_store import list_dep_watch_repos
            from personas.guard.dep_watch import run_dep_watch_for_install

            repos = list_dep_watch_repos(install_id)
            if not repos:
                continue
            filed_failed = with_install_token_retry(
                install_id,
                lambda token, iid=install_id, r=repos: run_dep_watch_for_install(token, iid, r),
            ) or (0, 0)
            dep_reports += filed_failed[0]
            dep_watch_failed += filed_failed[1]
        except Exception as e:  # noqa: BLE001 — one install must not abort the cron
            log.warning(
                "dep_watch_install_failed",
                extra={"install_id": install_id, "kind": type(e).__name__},
            )
            dep_watch_failed += 1

    # Auto-replay missed webhook deliveries (#407), best-effort: a replay
    # failure must never abort the cron, so it's wrapped here on TOP of
    # replay_since's own per-attempt best-effort.
    try:
        replay = _replay_missed_deliveries()
    except Exception as e:  # noqa: BLE001 - replay never aborts the poll cycle
        log.warning("delivery_replay_failed", extra={"kind": type(e).__name__})
        replay = {"replay_error": type(e).__name__}

    result: dict[str, int | str] = {
        "installs": len(installs),
        "records": polled_records,
        "submitted": submitted,
        "failed_installs": failed_installs,
        "pulse_nudges": nudges,
        "pulse_failed_installs": pulse_failed,
        "dep_watch_reports": dep_reports,
        "dep_watch_failed_installs": dep_watch_failed,
        **replay,
    }
    # Total failure (auth/config drift, GitHub down) errors EVERY install and
    # would otherwise look identical to a healthy idle cycle (submitted:0) —
    # both are `info`. Escalate the all-failed case to `error`. NOTE
    # (audit #388-2): no monitor queries this event today (the #261 arm-up
    # never happened), and record-less installs `continue` before counting,
    # so this fires only when every RECORD-BEARING install fails. The
    # process-global failure class (credentials) is covered fail-loud by
    # _prove_roles_anywhere_identity + the KSM Job monitor instead.
    if installs and failed_installs == len(installs):
        log.error("reaction_poll_all_installs_failed", extra=result)
    else:
        log.info("reaction_poll_cycle_complete", extra=result)
    return result
