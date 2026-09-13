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
from typing import Any, Callable

import delivery_replay
from adapters.install_store import (  # type: ignore
    list_allowlisted_installs,
    list_comment_records,
    list_dep_watch_repos,
    list_hygiene_watch_repos,
    list_pulse_enabled_repos,
    list_reopen_watch_repos,
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


def _run_repo_scoped_pass(
    installs: list[int],
    lister: Callable[[int], list[dict[str, Any]]],
    runner: Callable[[str, int, list[dict[str, Any]]], tuple[int, int]],
    failed_log_event: str,
) -> tuple[int, int]:
    """The shared best-effort per-install pass shape (grug#767): list this
    install's opted-in repos (store-driven targeting), skip installs with
    none, run the install-scoped pass through the standard token-retry
    wrapper, and accumulate (done, failed) across every install. One
    install's listing/GH/token failure must never abort the pass for the
    rest.

    Extracted from four near-identical inline copies (Elder #766/#767):
    `handler` measured cyclomatic 30 / cognitive 53 on main, well over the
    15/25 caps, purely from this shape repeated per scheduled pass.
    `failed_log_event` keeps each call site's own structured-log key,
    unchanged from before extraction - existing monitors keep matching on
    the same event names."""
    done = 0
    failed = 0
    for install_id in installs:
        try:
            repos = lister(install_id)
            if not repos:
                continue
            result = with_install_token_retry(
                install_id,
                lambda token, iid=install_id, r=repos: runner(token, iid, r),
            ) or (0, 0)
            done += result[0]
            failed += result[1]
        except Exception as e:  # noqa: BLE001 — one install must not abort the cron
            log.warning(failed_log_event, extra={"install_id": install_id, "kind": type(e).__name__})
            failed += 1
    return done, failed


def _pulse_runner(token: str, install_id: int, repos: list[dict[str, Any]]) -> tuple[int, int]:
    """Pulse (#472) returns a single nudge count, not (done, failed) - it
    does its own per-repo best-effort internally with no separate failure
    tally, so the second slot is always 0 on a successful call (an
    install-level exception is still counted by the shared helper)."""
    from personas.pulse.nudge import run_pulse_for_install

    return run_pulse_for_install(token, install_id, repos), 0


def _dep_watch_runner(token: str, install_id: int, repos: list[dict[str, Any]]) -> tuple[int, int]:
    from personas.guard.dep_watch import run_dep_watch_for_install

    return run_dep_watch_for_install(token, install_id, repos)


def _reopen_watch_runner(token: str, install_id: int, repos: list[dict[str, Any]]) -> tuple[int, int]:
    from personas.guard.reopen_watch import run_reopen_watch_for_install

    return run_reopen_watch_for_install(token, install_id, repos)


def _hygiene_watch_runner(token: str, install_id: int, repos: list[dict[str, Any]]) -> tuple[int, int]:
    from personas.guard.hygiene_watch import run_hygiene_watch_for_install

    return run_hygiene_watch_for_install(token, install_id, repos)


def _reaction_poll_pass(installs: list[int]) -> tuple[int, int, int]:
    """The reactions poll itself (#247b) - the ORIGINAL scheduled pass,
    predating the store-driven shape `_run_repo_scoped_pass` covers
    (it lists CommentRecords, not opted-in repos, and polls/annotates
    rather than running a persona), so it stays its own function instead
    of being forced into that helper (grug#767: "convert only what
    genuinely matches").

    Returns (records_polled, verdicts_submitted, installs_failed)."""
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
    return polled_records, submitted, failed_installs


def _emit_enforcement_for_install(token: str, install_id: int) -> int:
    """Per-repo body of the enforcement re-emission pass (#460), pulled out
    of `_enforcement_reemission_pass` into its own top-level function
    (grug#767): a nested closure adds its OWN nesting depth to the
    enclosing function's cognitive-complexity score on every branch inside
    it, which alone pushed the outer pass over cap even though this body's
    intrinsic complexity (cyclomatic 9, cognitive 13) is comfortably under
    it - extraction removes that penalty without changing any behavior.

    Per-REPO best-effort so one repo's GitHub error can't starve the rest
    of their gauge. Returns the count of repos emitted for."""
    from adapters.install_store import get_repo_config
    from enforcement import GRUG_DOR_CHECK_NAME
    from github_rulesets_client import detect_enforcement, list_installation_repos
    from observability import emit_enforcement_metric

    n = 0
    for r in list_installation_repos(token):
        full = r.get("full_name", "")
        owner, sep, name = full.partition("/")
        if not sep or not name:
            continue
        try:
            cfg = get_repo_config(install_id, r["id"])
            # #716: EMIT for an opted-out repo, never `continue`. Dropping
            # it from the gauge is what latched the monitor: Datadog holds
            # a silent multi-alert group in its last state for 24h, so a
            # repo that was red when it left stayed red long after the
            # decision. Reporting an explicit healthy state resolves the
            # group on the next evaluation, and the gauge shows the whole
            # fleet including the repos we chose not to gate.
            #
            # Both opt-outs count. `tpm_enabled=false` skips the persona
            # entirely; `force_disable_enforcement` stops the self-healing
            # loop re-creating a deleted ruleset (CONTEXT.md). The second
            # one used to keep polling and emit `none` forever, so a repo
            # using the documented escape hatch pinned this monitor red
            # permanently.
            if (
                cfg.get("tpm_enabled", True) is False
                or cfg.get("force_disable_enforcement", False)
            ):
                emit_enforcement_metric(full, "opted_out")
                n += 1
                continue
            state = detect_enforcement(
                token, owner, name,
                r.get("default_branch") or "main",
                GRUG_DOR_CHECK_NAME,
                stored_ruleset_id=cfg.get("enforcement_ruleset_id"),
            ).state
            emit_enforcement_metric(full, state)
            n += 1
        except Exception as e:  # noqa: BLE001 - one repo must not starve
            # the install's remaining repos of their gauge
            log.warning(
                "enforcement_emit_repo_failed",
                extra={"install_id": install_id, "repo": full, "kind": type(e).__name__},
            )
            # #518: a detection FAILURE must be visible as its own gauge
            # state, never a silent gap next to real "none" rows - an
            # auth/rate-limit outage across the install would otherwise
            # read as "nothing enforced anywhere" going dark, which nobody
            # pages on.
            try:
                emit_enforcement_metric(full, "error")
            except Exception:  # noqa: BLE001 - best-effort gauge
                pass
    return n


def _enforcement_reemission_pass(installs: list[int]) -> tuple[int, int]:
    """Enforcement-gauge re-emission (#460): grug.enforcement.state was only
    emitted on enforcement CONFIG events (dashboard toggle, repo-added
    heal), so in steady state the enforcement-gap monitor sat in permanent
    No Data - blind to exactly the out-of-band ruleset deletion it exists
    to catch. The denominator comes from GITHUB (/installation/
    repositories), NOT the store: REPO# rows are written only on explicit
    config changes, so a defaults-only install has ZERO rows (verified
    live at the #513 post-deploy check - the store-driven v1 of this pass
    emitted nothing). The store overlays per-repo opt-OUTs
    (tpm_enabled=false skips).

    Nested per-install-then-per-repo shape (the repo list itself is a
    GitHub call, not a store lister), so unlike the four passes above it
    does not fit `_run_repo_scoped_pass` - own function instead (grug#767:
    "convert only what genuinely matches").

    Returns (repos_emitted, installs_failed)."""
    enforcement_emitted = 0
    enforcement_failed = 0
    for install_id in installs:
        try:
            enforcement_emitted += (
                with_install_token_retry(
                    install_id,
                    lambda token, iid=install_id: _emit_enforcement_for_install(token, iid),
                ) or 0
            )
        except Exception as e:  # noqa: BLE001 - one install must not abort the cron
            log.warning(
                "enforcement_emit_install_failed",
                extra={"install_id": install_id, "kind": type(e).__name__},
            )
            enforcement_failed += 1
    return enforcement_emitted, enforcement_failed


def _install_reconciliation_pass() -> tuple[int, int, int]:
    """Compare GitHub's live installation list against the store's INST#
    rows (grug#842): an install present at GitHub but missing its row is
    completely silent - webhooks drop at `is_install_allowlisted`'s
    `allowlist_miss_no_install`, an INFO log nobody watches. An INST# row
    for an install GitHub no longer has is stale in the other direction.
    Both directions are fleet-wide, not per-install, so this does its own
    top-level GitHub + store fetch rather than looping the
    already-allowlist-filtered `installs` the other passes share.

    Repairs the missing-from-store direction via the existing idempotent
    `record_installation`. GitHub's app-level listing has no field
    identifying WHO installed it (only `installation.created`'s webhook
    `sender` does) - a repaired row's `installed_by_user_id` is the
    account's own id, a documented placeholder that unblocks the
    allowlist gate but will never resolve a real human in
    `list_user_installations`'s dashboard lookup. That's acceptable: this
    repair exists to stop webhooks from silently dropping, not to
    backfill dashboard ownership. The reverse direction (a stale row) is
    reported only, never auto-deleted - the issue asks for visibility,
    not an unattended delete of state whose downstream effects (REPO#
    rows, etc.) are out of scope here.

    Returns (repaired, stale_flagged, failed). `failed` is 0 or 1: this is
    one top-level GitHub/store comparison, not a per-install loop, so a
    single failure aborts this pass (never the cron) rather than needing
    its own per-item counter.
    """
    try:
        from adapters.install_store import list_all_install_ids, record_installation
        from github_app_auth import list_app_installations
        from observability import emit_gauge

        gh_installs = list_app_installations()
        gh_ids = {inst["id"] for inst in gh_installs if "id" in inst}
        store_ids = set(list_all_install_ids())
        missing_from_store = gh_ids - store_ids
        stale_in_store = store_ids - gh_ids

        repaired = 0
        for inst in gh_installs:
            iid = inst.get("id")
            if iid not in missing_from_store:
                continue
            account = inst.get("account") or {}
            try:
                record_installation(
                    install_id=iid,
                    account_login=account.get("login", ""),
                    account_type=account.get("type", ""),
                    installed_by_user_id=account.get("id") or 0,
                )
                repaired += 1
                log.warning(
                    "install_reconciliation_repaired",
                    extra={"install_id": iid, "account_login": account.get("login", "")},
                )
            except Exception as e:  # noqa: BLE001 — one bad row must not drop the rest
                log.warning(
                    "install_reconciliation_repair_failed",
                    extra={"install_id": iid, "kind": type(e).__name__},
                )

        for iid in stale_in_store:
            log.warning("install_reconciliation_stale_store_row", extra={"install_id": iid})

        emit_gauge("grug.install_reconciliation.missing_from_store", float(len(missing_from_store)))
        emit_gauge("grug.install_reconciliation.stale_store_rows", float(len(stale_in_store)))
        return repaired, len(stale_in_store), 0
    except Exception as e:  # noqa: BLE001 — this pass must never abort the cron
        log.warning("install_reconciliation_pass_failed", extra={"kind": type(e).__name__})
        return 0, 0, 1


def handler(event: dict[str, Any], context: Any) -> dict[str, int | str]:
    """Poll reactions for every allowlisted install. Returns a summary
    dict (installs scanned, records polled, verdicts submitted) — also
    the structured-log payload an operator/DD reads to confirm the cron
    ran end-to-end."""
    _prove_roles_anywhere_identity()
    installs = list_allowlisted_installs()
    polled_records, submitted, failed_installs = _reaction_poll_pass(installs)

    # Pulse pass (#472): the first SCHEDULED persona rides the same
    # cadence as its OWN loop (the reactions loop `continue`s installs
    # with no comment records - Pulse must still run there). Store-driven
    # targeting (codex PR #489): only repos the operator ENABLED - no
    # /installation/repositories paging, so a large install can never
    # starve an enabled repo behind a discovery-page prefix, and idle
    # ticks cost zero GH calls. Everything inside run_pulse_for_install is
    # capped + per-repo best-effort + store-claim idempotent.
    nudges, pulse_failed = _run_repo_scoped_pass(
        installs, list_pulse_enabled_repos, _pulse_runner, "pulse_install_failed",
    )

    # Guard dependency watch (#491): the owned dependabot-class pass -
    # same store-driven, best-effort shape as Pulse.
    dep_reports, dep_watch_failed = _run_repo_scoped_pass(
        installs, list_dep_watch_repos, _dep_watch_runner, "dep_watch_install_failed",
    )

    # Guard reopen watch (2026-07-24 audit finding): the close-completeness
    # guard reopens an issue once and never follows up - this closes that
    # loop by escalating stale (untouched) guard-reopens. Same store-driven,
    # best-effort shape as dep_watch.
    reopen_escalated, reopen_watch_failed = _run_repo_scoped_pass(
        installs, list_reopen_watch_repos, _reopen_watch_runner, "reopen_watch_install_failed",
    )

    # Guard hygiene watch (#655, epic #654): the fleet lints CI hygiene at
    # DIFF time, so a violation that merged before the linter existed sits
    # silent forever - nothing re-reads the default branch. Weekly
    # default-branch scan, one refreshed report issue per repo. Same
    # store-driven, best-effort shape as dep_watch.
    hygiene_reports, hygiene_watch_failed = _run_repo_scoped_pass(
        installs, list_hygiene_watch_repos, _hygiene_watch_runner, "hygiene_watch_install_failed",
    )

    # Check-run sweep/reconcile (grug#947, epic #887): same store-driven,
    # best-effort shape as the passes above. Own module rather than
    # inlined - this file's `handler` is already well over the persona
    # complexity caps (see _hygiene_watch_pass's docstring).
    from check_run_reconciler import reconcile_installs

    check_run_reconciled, check_run_reconcile_failed = reconcile_installs(installs)

    install_repaired, install_stale, install_reconciliation_failed = (
        _install_reconciliation_pass()
    )

    # Enforcement-gauge re-emission (#460): same best-effort shape as the
    # passes above, plus per-REPO best-effort so one repo's GitHub error
    # can't starve the rest of their gauge - see _enforcement_reemission_pass.
    enforcement_emitted, enforcement_failed = _enforcement_reemission_pass(installs)

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
        "reopen_watch_escalated": reopen_escalated,
        "reopen_watch_failed_installs": reopen_watch_failed,
        "hygiene_watch_reports": hygiene_reports,
        "hygiene_watch_failed_installs": hygiene_watch_failed,
        "check_run_reconciled": check_run_reconciled,
        "check_run_reconcile_failed_installs": check_run_reconcile_failed,
        "install_reconciliation_repaired": install_repaired,
        "install_reconciliation_stale": install_stale,
        "install_reconciliation_failed": install_reconciliation_failed,
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
