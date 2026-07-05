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
from observability import configure_logging
from personas.code_reviewer.reactions import poll_and_annotate

# The CronJob entry is a bare `python -c "... handler({}, None)"` - unlike
# webhook/api there is no main.py to configure logging, so without this the
# root logger has NO handler and every INFO line (including the
# reaction_poll_cycle_complete summary this module's docstring promises the
# operator) is silently dropped; only WARNING+ leaked out via logging's
# lastResort stderr handler. Found at the #460 post-deploy verification.
configure_logging()

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


# Moved to services/_shared/aws_identity.py at #389 (fleet-wide rollout).
# Re-exported under the historical name: it is a live patch target in the
# test suite, and handler() resolves it through THIS module's namespace so
# monkeypatch.setattr(poller_handler, ...) keeps working.
from aws_identity import prove_roles_anywhere_identity as _prove_roles_anywhere_identity  # noqa: E402


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

    # Enforcement-gauge re-emission (#460): grug.enforcement.state was only
    # emitted on enforcement CONFIG events (dashboard toggle, repo-added
    # heal), so in steady state the enforcement-gap monitor sat in permanent
    # No Data - blind to exactly the out-of-band ruleset deletion it exists
    # to catch. The denominator comes from GITHUB (/installation/
    # repositories), NOT the store: REPO# rows are written only on explicit
    # config changes, so a defaults-only install has ZERO rows (verified
    # live at the #513 post-deploy check - the store-driven v1 of this pass
    # emitted nothing). The store overlays per-repo opt-OUTs
    # (tpm_enabled=false skips). Same best-effort shape as the passes
    # above, plus per-REPO best-effort so one repo's GitHub error can't
    # starve the rest of their gauge.
    enforcement_emitted = 0
    enforcement_failed = 0
    for install_id in installs:
        try:
            from adapters.install_store import get_repo_config
            from enforcement import GRUG_DOR_CHECK_NAME
            from github_rulesets_client import (
                detect_enforcement,
                list_installation_repos,
            )
            from observability import emit_enforcement_metric

            def _emit_for_install(token: str, iid=install_id) -> int:
                n = 0
                for r in list_installation_repos(token):
                    full = r.get("full_name", "")
                    owner, sep, name = full.partition("/")
                    if not sep or not name:
                        continue
                    try:
                        cfg = get_repo_config(iid, r["id"])
                        if cfg.get("tpm_enabled", True) is False:
                            continue
                        state = detect_enforcement(
                            token, owner, name,
                            r.get("default_branch") or "main",
                            GRUG_DOR_CHECK_NAME,
                        )
                        emit_enforcement_metric(full, state)
                        n += 1
                    except Exception as e:  # noqa: BLE001 - one repo must not
                        # starve the install's remaining repos of their gauge
                        log.warning(
                            "enforcement_emit_repo_failed",
                            extra={"install_id": iid, "repo": full,
                                   "kind": type(e).__name__},
                        )
                return n

            enforcement_emitted += (
                with_install_token_retry(install_id, _emit_for_install) or 0
            )
        except Exception as e:  # noqa: BLE001 - one install must not abort the cron
            log.warning(
                "enforcement_emit_install_failed",
                extra={"install_id": install_id, "kind": type(e).__name__},
            )
            enforcement_failed += 1

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
        "enforcement_emitted": enforcement_emitted,
        "enforcement_failed_installs": enforcement_failed,
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
