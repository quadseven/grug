"""Unit tests for the grug Datadog monitor query builders (#406).

These test the PURE query-string builders (return values, not source text),
so they need no Pulumi runtime. They guard the two failure modes that left
the 2026-06-14 outage unpaged:

  1. Monitors querying retired `aws.lambda.*` metrics -> permanent No Data.
  2. KSM monitors accidentally scoped by an `env` tag that doesn't propagate
     onto Kubernetes State Metric series -> also permanent No Data.

`pulumi preview` remains the resource-shape gate; these are the logic gate.
"""

from __future__ import annotations

import pathlib

import pulumi

from components.dd_monitors import (
    credential_acquisition_failure_query,
    all_ksm_monitor_queries,
    crashloop_query,
    enforcement_gap_query,
    github_api_error_rate_query,
    inbound_webhook_silence_query,
    poller_cronjob_unhealthy_query,
    restart_spike_query,
    stuck_check_run_query,
    workload_not_ready_query,
)


class _PulumiMocks(pulumi.runtime.Mocks):
    def new_resource(self, args):  # type: ignore[override]
        return [args.name + "_id", args.inputs]

    def call(self, args):  # type: ignore[override]
        return {}


pulumi.runtime.set_mocks(_PulumiMocks())


def test_no_ksm_or_workload_query_references_retired_aws_lambda() -> None:
    """Acceptance #1: no replacement monitor may reference aws.lambda.* —
    those metrics no longer exist post-k8s migration."""
    for q in all_ksm_monitor_queries():
        assert "aws.lambda." not in q, f"retired aws.lambda metric in: {q}"


def test_workload_not_ready_uses_namespace_scoped_ksm() -> None:
    q = workload_not_ready_query()
    assert "kubernetes_state.deployment.replicas_ready" in q
    assert "kube_namespace:grug" in q
    assert "by {kube_deployment}" in q
    assert "< 1" in q


def test_crashloop_query_detects_crashloopbackoff_per_pod() -> None:
    """Acceptance #2: a crash-looping workload (incl grug-consumer) fires."""
    q = crashloop_query()
    assert "kubernetes_state.container.status_report.count.waiting" in q
    assert "reason:crashloopbackoff" in q
    assert "kube_namespace:grug" in q
    assert "by {pod_name}" in q
    assert "> 0" in q
    # infra#2081: healthy must read OK, not No Data.
    assert q.startswith("max(last_5m):default_zero(max:")


def test_restart_spike_query_is_namespace_scoped() -> None:
    q = restart_spike_query()
    assert "kubernetes_state.container.restarts" in q
    assert "kube_namespace:grug" in q
    assert "by {pod_name}" in q


def test_no_replacement_query_uses_uncollected_aws_sqs() -> None:
    """aws.sqs.* is NOT collected by the DD AWS integration in this org, so a
    queue-age monitor would be permanent No Data — the trap this slice retires.
    Guard that no replacement query reintroduces it (the consumer queue-age
    monitor is deferred until the SQS integration namespace is enabled)."""
    for q in all_ksm_monitor_queries():
        assert "aws.sqs." not in q, f"uncollected aws.sqs metric in: {q}"


def test_poller_cronjob_unhealthy_uses_duration_since_last_successful() -> None:
    """#379 fold-in: poller CronJob stopped succeeding."""
    q = poller_cronjob_unhealthy_query()
    assert "kubernetes_state.cronjob.duration_since_last_successful" in q
    assert "kube_cronjob:grug-poller" in q


def test_ksm_queries_do_not_scope_by_env_tag() -> None:
    """KSM series do NOT carry the stack `env` tag; scoping a KSM query by
    `env:` would silently match nothing (No Data) — the exact trap that hid
    the outage. KSM monitors must scope by kube_namespace only."""
    for q in all_ksm_monitor_queries():
        if "kubernetes_state" in q:
            assert "env:" not in q, f"KSM query wrongly scoped by env tag: {q}"


@pulumi.runtime.test
def test_continuous_ksm_monitors_page_on_no_data():
    """Regression guard (audit HIGH): a CONTINUOUS KSM metric (replicas_ready,
    duration_since_last_successful) going No Data means the whole k8s-telemetry
    pipeline broke — the same silent-can't-fire trap this slice exists to kill —
    so those monitors MUST notify_no_data=True. CONDITIONAL metrics (crashloop
    waiting-reason, restart change()) correctly stay False (No Data = healthy)."""
    import pulumi_datadog as datadog
    from components import dd_monitors

    provider = datadog.Provider("test-dd", api_key="x", app_key="y")
    bundle = dd_monitors.create_all(
        env="prod",
        notify_handle="@webhook-grug-discord-monitoring",
        webhook_public_url="https://webhook.example/webhook/github",
        api_public_url="https://api.example",
        provider=provider,
    )

    def _check(vals):
        workload_not_ready, poller, crashloop, restart = vals
        assert workload_not_ready is True, "workload_not_ready must page on No Data"
        assert poller is True, "poller_cronjob must page on No Data"
        assert crashloop is False, "crashloop is conditional — No Data is healthy"
        assert restart is False, "restart_spike is conditional — No Data is healthy"

    return pulumi.Output.all(
        bundle.workload_not_ready.notify_no_data,
        bundle.poller_cronjob.notify_no_data,
        bundle.crashloop.notify_no_data,
        bundle.restart_spike.notify_no_data,
    ).apply(_check)


def test_credential_acquisition_query_covers_fleet_and_both_signals() -> None:
    """#389: the one monitor must see every workload AND both failure
    shapes (boot-proof event + botocore's mid-run error class)."""
    q = credential_acquisition_failure_query("prod")
    # Wildcard on purpose: any future grug service must be covered
    # without editing the monitor.
    assert "service:grug-*" in q
    assert "roles_anywhere_identity_failed" in q
    assert "CredentialRetrievalError" in q
    # Mid-run non-retrieval classes (a pod flipped to env creds during
    # revert-recovery, mangled profile) must page too - audit #389 stage 2.
    assert "NoCredentialsError" in q and "InvalidClientTokenId" in q
    assert 'rollup("count")' in q and "> 0" in q
    assert "env:prod" in q


@pulumi.runtime.test
def test_credential_monitor_is_log_alert_and_not_no_data():
    """#389 audit stage-7: a make-everything-page-on-no-data sweep would
    turn this log monitor into a nightly flapper; pin its shape."""
    import pulumi_datadog as datadog

    from components import dd_monitors

    provider = datadog.Provider("test-dd-cred", api_key="x", app_key="y")
    bundle = dd_monitors.create_all(
        env="prod",
        notify_handle="@webhook-grug-discord-monitoring",
        webhook_public_url="https://webhook.example/webhook/github",
        api_public_url="https://api.example",
        provider=provider,
    )

    def check(args):
        mtype, no_data = args
        assert mtype == "log alert"
        assert no_data is False

    return pulumi.Output.all(
        bundle.credential_acquisition_fail.type,
        bundle.credential_acquisition_fail.notify_no_data,
    ).apply(check)



# --- #379: owned SQS depth gauges -------------------------------------

def test_no_owned_queue_query_references_uncollected_aws_sqs() -> None:
    """The DD AWS integration does not collect aws.sqs.* in this org - a
    monitor on it is permanently blind (the trap that shipped three blind
    monitors). Every queue monitor must ride an owned grug.sqs.* gauge."""
    from components.dd_monitors import all_owned_queue_queries

    for q in all_owned_queue_queries("prod"):
        assert "aws.sqs." not in q, f"uncollected aws.sqs metric in: {q}"
        assert "grug.sqs." in q


def test_owned_queue_queries_are_env_scoped() -> None:
    """Every owned query filters env explicitly (gemini peer review): the
    owned gauges carry a reliable env tag, and an unscoped query would let
    a healthy dev emitter mask a dead prod one."""
    from components.dd_monitors import all_owned_queue_queries

    for q in all_owned_queue_queries("prod"):
        assert "env:prod" in q, f"env-unscoped query: {q}"
    for q in all_owned_queue_queries("dev"):
        assert "env:dev" in q


def test_owned_queue_queries_tag_exact_queue_names() -> None:
    """Queue tags must match the consumer's emission exactly - the fixed
    Pulumi `name=` values with the .fifo suffix. One monitor per queue,
    simple filters (no OR/AND boolean filter syntax - an apply-time
    validation risk preview cannot catch)."""
    from components import dd_monitors as m

    assert "queue:grug-cave-jobs.fifo" in m.cave_jobs_backlog_query("prod")
    assert "queue:grug-rerun-jobs.fifo" in m.rerun_backlog_query("prod")
    assert "queue:grug-cave-results.fifo" in m.cave_results_backlog_query("prod")
    assert "queue:grug-rerun-jobs-dlq.fifo" in m.rerun_dlq_depth_query("prod")
    assert "queue:grug-cave-jobs-dlq.fifo" in m.cave_jobs_dlq_depth_query("prod")
    assert "queue:grug-cave-results-dlq.fifo" in m.cave_results_dlq_depth_query("prod")
    for q in m.all_owned_queue_queries("prod"):
        assert " OR " not in q and " AND " not in q


def test_backlog_queries_use_stalled_semantics_and_health_is_intermittent_proof() -> None:
    """Backlog monitors require a stalled signal across the FULL window;
    DLQ monitors are any-message (max > 0). The health query is avg-of-avg
    per queue < 0.5 so an INTERMITTENT partial failure cannot hide behind
    one good sweep (codex peer review) and a single blip does not flap."""
    from components import dd_monitors as m

    assert m.cave_jobs_backlog_query("prod").startswith("min(last_15m):")
    assert m.rerun_backlog_query("prod").startswith("min(last_15m):")
    assert m.cave_results_backlog_query("prod").startswith("min(last_15m):")
    assert all(
        "grug.sqs.stalled" in query
        for query in (
            m.cave_jobs_backlog_query("prod"),
            m.rerun_backlog_query("prod"),
            m.cave_results_backlog_query("prod"),
        )
    )
    assert m.rerun_dlq_depth_query("prod").startswith("max(last_15m):")
    assert m.cave_jobs_dlq_depth_query("prod").startswith("max(last_15m):")
    assert m.cave_results_dlq_depth_query("prod").startswith("max(last_15m):")
    hq = m.telemetry_health_query("prod")
    assert hq.startswith("avg(last_15m):avg:")
    assert "grug.sqs.telemetry_queue_ok" in hq
    assert " by {queue}" in hq
    assert "< 0.5" in hq


def _consumer_module_ast():
    """Parse the consumer module WITHOUT importing it (it builds a boto3
    client at import; this test env has no AWS runtime)."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[3]
        / "services" / "webhook" / "consumer.py"
    ).read_text()
    return ast.parse(src)


def _consumer_telemetry_queue_names() -> list[str]:
    import ast

    for node in ast.walk(_consumer_module_ast()):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", "") == "_TELEMETRY_QUEUE_NAMES":
                    return [el.value for el in node.value.elts]
    raise AssertionError("_TELEMETRY_QUEUE_NAMES not found in consumer.py")


def test_monitor_queue_tags_subset_of_consumer_emission() -> None:
    """Cross-package name-drift guard (audit stage-8): a queue name in a
    monitor query that the consumer does not emit = that monitor is
    permanently blind (No Data, notify_no_data=false) - the exact trap
    this family exists to retire. The emitter tuple is the source of
    truth; monitors may watch a subset."""
    import re

    from components.dd_monitors import all_owned_queue_queries

    emitted = set(_consumer_telemetry_queue_names())
    queried = set()
    for q in all_owned_queue_queries("prod"):
        queried.update(re.findall(r"queue:([a-z0-9.-]+)", q))
    assert queried, "no queue tags found in owned queries"
    missing = queried - emitted
    assert not missing, f"monitors query queues the consumer never emits: {missing}"


def test_every_consumer_queue_has_a_depth_monitor() -> None:
    """The inverse guard: every queue the consumer sweeps is watched by at
    least one depth monitor - an emitted-but-unmonitored queue is quiet
    coverage rot."""
    import re

    from components.dd_monitors import all_owned_queue_queries

    emitted = set(_consumer_telemetry_queue_names())
    queried = set()
    for q in all_owned_queue_queries("prod"):
        queried.update(re.findall(r"queue:([a-z0-9.-]+)", q))
    unmonitored = emitted - queried
    assert not unmonitored, f"consumer sweeps unmonitored queues: {unmonitored}"


@pulumi.runtime.test
def test_owned_queue_monitors_no_data_pager_placement():
    """Pin each monitor's (query, notify_no_data) pair (audit stage-7):
    the telemetry-health monitor is the family's ONLY no-data pager - a
    silent flip (or a copy-paste swapping builder queries between
    monitors) re-creates the blind-monitor trap with green tests."""
    import pulumi_datadog as datadog

    from components import dd_monitors

    provider = datadog.Provider("test-dd-queues", api_key="x", app_key="y")
    bundle = dd_monitors.create_owned_queue_monitors(
        env="prod",
        provider=provider,
    )

    expected = {
        "cave_jobs_backlog": (dd_monitors.cave_jobs_backlog_query("prod"), False),
        "rerun_backlog": (dd_monitors.rerun_backlog_query("prod"), False),
        "cave_results_backlog": (dd_monitors.cave_results_backlog_query("prod"), False),
        "rerun_dlq": (dd_monitors.rerun_dlq_depth_query("prod"), False),
        "cave_jobs_dlq": (dd_monitors.cave_jobs_dlq_depth_query("prod"), False),
        "cave_results_dlq": (dd_monitors.cave_results_dlq_depth_query("prod"), False),
        "telemetry_health": (dd_monitors.telemetry_health_query("prod"), True),
    }

    checks = []
    for field, (want_query, want_no_data) in expected.items():
        monitor = getattr(bundle, field)

        def check(args, wq=want_query, wnd=want_no_data, f=field):
            query, no_data, full_window = args
            assert query == wq, f"{f}: query mismatch: {query}"
            assert bool(no_data) is wnd, f"{f}: notify_no_data={no_data}, want {wnd}"
            assert full_window is False, f"{f}: require_full_window must be False"

        checks.append(
            pulumi.Output.all(
                monitor.query, monitor.notify_no_data, monitor.require_full_window,
            ).apply(check)
        )
    return pulumi.Output.all(*checks)


def test_deploy_rollback_query_shape() -> None:
    """#499: any rollback occurrence pages; env-scoped; count semantics
    per the DD monitor-syntax gotchas (.as_count on a dogstatsd count)."""
    from components.dd_monitors import deploy_rollback_query

    q = deploy_rollback_query("prod")
    assert "grug.deploy.rollback" in q and "env:prod" in q
    assert q.endswith("> 0") and ".as_count()" in q
    # infra#2081: a quiet window must read OK, not No Data.
    assert "default_zero(sum:grug.deploy.rollback{" in q


@pulumi.runtime.test
def test_deploy_monitor_pin():
    import pulumi_datadog as datadog

    from components import dd_monitors

    provider = datadog.Provider("test-dd-deploy", api_key="x", app_key="y")
    bundle = dd_monitors.create_deploy_monitors(
        env="prod",
        provider=provider,
    )

    def check(args):
        query, no_data = args
        assert query == dd_monitors.deploy_rollback_query("prod")
        assert bool(no_data) is False

    return pulumi.Output.all(
        bundle.rollback_fired.query, bundle.rollback_fired.notify_no_data,
    ).apply(check)


@pulumi.runtime.test
def test_only_three_monitors_can_page_and_every_handle_is_recovery_gated():
    """Pin the alert tiering, because its failure mode is silence, not noise.

    All 21 grug monitors used to carry a bare handle. That meant two pings per
    incident (broke, then fixed itself) and a page for things like "deploy
    auto-rollback fired", which is the rollback SUCCEEDING. Applying infra's
    ALERTING-STANDARD bar leaves exactly three that qualify.

    Both halves matter and neither is visible in review:
    - a monitor that quietly regains a handle starts paging again
    - a handle outside {{#is_alert}} notifies on RECOVERY, which is the
      specific behaviour that made the Discord channel unreadable
    """
    import pulumi_datadog as datadog

    from components import dd_monitors

    handle = "@webhook-grug-discord-monitoring"
    provider = datadog.Provider("test-dd-tiers", api_key="x", app_key="y")
    bundle = dd_monitors.create_all(
        env="prod",
        notify_handle=handle,
        webhook_public_url="https://webhook.example/webhook/github",
        api_public_url="https://api.example",
        provider=provider,
    )

    paging = {
        "workload_not_ready": bundle.workload_not_ready,
        "elder_llm_degraded": bundle.elder_llm_degraded,
    }
    silent = {
        "crashloop": bundle.crashloop,
        "restart_spike": bundle.restart_spike,
        "poller_cronjob": bundle.poller_cronjob,
        "sig_verify_fail": bundle.sig_verify_fail,
        "elder_offload_fail": bundle.elder_offload_fail,
        "persona_dispatch_unhandled": bundle.persona_dispatch_unhandled,
        "enforcement_gap": bundle.enforcement_gap,
        "github_api_errors": bundle.github_api_errors,
        "check_run_stuck": bundle.check_run_stuck,
        "inbound_webhook_silence": bundle.inbound_webhook_silence,
        "cf_secret_mismatch": bundle.cf_secret_mismatch,
        "credential_acquisition_fail": bundle.credential_acquisition_fail,
    }

    names = list(paging) + list(silent)
    messages = [m.message for m in list(paging.values()) + list(silent.values())]

    def _check(vals):
        for name, msg in zip(names, vals):
            msg = msg or ""
            if name in paging:
                assert handle in msg, f"{name} is page tier but carries no handle"
                # The handle must sit INSIDE the alert block, or Datadog
                # renders it on the recovery notification too.
                gated = f"{{{{#is_alert}}}}{handle}" in msg or (
                    "{{#is_alert}}" in msg and "{{/is_alert}}" in msg
                )
                assert gated, f"{name} handle is not recovery-gated"
            else:
                assert handle not in msg, (
                    f"{name} is digest tier and must notify nobody; "
                    "it regained a recipient"
                )

    return pulumi.Output.all(*messages).apply(_check)


def test_composition_root_cannot_smuggle_an_ungated_recipient() -> None:
    """A monitor defined in __main__ bypasses the component's tiering entirely.

    #789 moved page/digest and recovery-gating into dd_monitors, which covers
    every monitor the component builds. It could not cover
    `grug-cave-fallback-fired`, which is constructed directly in __main__ and
    therefore kept a bare, ungated handle: 21 monitors became 4 rather than 3,
    and the survivor pinged on recovery. That was only caught by querying
    Datadog AFTER the deploy reported success.

    So pin the structure instead of the outcome: the handle may be referenced
    exactly twice in the composition root, to define it and to hand it to
    create_all. Any third use is a monitor building its own message, which
    means it opted out of the tiering without anyone deciding that.
    """
    root = pathlib.Path(__file__).resolve().parent.parent / "__main__.py"
    src = root.read_text()
    uses = [
        line.strip()
        for line in src.splitlines()
        if "_dd_notify" in line and not line.strip().startswith("#")
    ]
    assert len(uses) == 2, (
        "_dd_notify is used %d times in __main__.py, expected 2 "
        "(the definition and the create_all argument). A monitor built here "
        "does not go through _page()/_DIGEST, so it can page ungated:\n  %s"
        % (len(uses), "\n  ".join(uses))
    )
    assert uses[0].startswith("_dd_notify ="), uses[0]
    assert uses[1] == "notify_handle=_dd_notify,", uses[1]


# --- #716: the enforcement-gap monitor must not latch -----------------------


def test_enforcement_gap_query_thresholds_on_value_not_the_state_tag() -> None:
    """#716 / ADR-0022: filtering on `enforcement_type` is what lets a series
    vanish. A repo whose state CHANGES must report a healthy value into its
    existing group, not drop out of the query - Datadog holds a silent
    multi-alert group in its last state for 24h.

    `< 0.5` catches `none` (0.0) and `error` (-1.0); `grug_managed` and
    `opted_out` (1.0) and `external` (0.5) stay quiet. The authoritative value
    map lives in `observability.emit_enforcement_metric`.
    """
    q = enforcement_gap_query("prod")
    assert "enforcement_type" not in q, (
        "no state-tag filter at all: filtering by state is what makes a "
        "series vanish when the state changes, which latches the alert"
    )
    assert "grug.enforcement.state" in q
    assert "by {repo}" in q
    assert "< 0.5" in q


def test_enforcement_gap_query_is_env_scoped() -> None:
    """Each stack alerts only on its own repos."""
    assert "env:dev" in enforcement_gap_query("dev")
    assert "env:prod" not in enforcement_gap_query("dev")


# --- #818: a dead LLM backend must reach an operator ------------------------


def test_backend_unusable_query_targets_the_terminal_token_only() -> None:
    """A 402 removed Elder's whole fallback and nobody was told.

    Measured 2026-07-27..08-03: OpenRouter `http_402` x4 (unpaid) and
    Poolside `http_404` x3 (dead endpoint) - BOTH halves of the SaaS
    overload valve down at once, so a Cave blip went straight to "Grug
    could not review this" and the only person informed was the PR author,
    who cannot pay a bill.

    Must key on `llm_backend_unusable`, never `llm_backend_http_failed`:
    the latter also fires for ordinary 429/5xx overload, which is the
    system working as designed.
    """
    from components.dd_monitors import backend_unusable_query

    q = backend_unusable_query("prod")
    assert "llm_backend_unusable" in q
    assert "llm_backend_http_failed" not in q, (
        "alerting on the overload token would be pure noise"
    )
    assert "env:prod" in q


def test_backend_unusable_monitor_does_not_page_on_free_tier_refusals() -> None:
    """Operator decision 2026-09-23: the OpenRouter key runs only on the free
    tier, which refuses intermittently with `403 Key limit exceeded`. grug logs
    that as `llm_backend_rate_limited`, and the paging monitor must not match
    it, or it pages on a dependency known to be flaky."""
    from components.dd_monitors import backend_unusable_query

    q = backend_unusable_query("prod")
    assert "llm_backend_rate_limited" not in q
    assert "rate_limited" not in q


# --- #948: observing GitHub reachability from the outside -------------------


def test_github_api_error_rate_query_is_env_scoped_and_thresholded():
    q = github_api_error_rate_query("prod")
    assert "grug.github_api.error" in q
    assert "env:prod" in q
    assert "env:dev" not in q
    assert "> 0.2" in q


def test_github_api_error_rate_query_has_no_state_tag_to_latch_on():
    """Same ADR-0022 shape as enforcement_gap_query: no tag filter that
    could make the series vanish - only the dense metric's own value."""
    q = github_api_error_rate_query("prod")
    assert "outcome:" not in q and "status:" not in q


def test_stuck_check_run_query_is_env_scoped_and_thresholded():
    q = stuck_check_run_query("prod")
    assert "grug.check_run.stuck_count" in q
    assert "env:prod" in q
    assert "env:dev" not in q
    assert "> 0" in q


# --- 2026-09-17: GitHub traffic not reaching grug at all --------------------


def test_inbound_webhook_silence_query_is_env_scoped_and_thresholded():
    q = inbound_webhook_silence_query("prod")
    assert "service:grug-webhook" in q
    assert "webhook_received" in q
    assert "env:prod" in q
    assert "env:dev" not in q
    assert "< 1" in q


def test_inbound_webhook_silence_query_uses_a_generous_window():
    """A short window would page on ordinary quiet stretches (nights,
    weekends) across every repo the App watches - the whole point of this
    monitor is to catch a SUSTAINED silence, not normal gaps between PRs."""
    q = inbound_webhook_silence_query("prod")
    assert '.last("4h")' in q


# --- every grug-owned monitor must self-identify as Pulumi-owned -----------

_ALL_MONITOR_FIELDS = (
    "workload_not_ready",
    "crashloop",
    "restart_spike",
    "poller_cronjob",
    "sig_verify_fail",
    "elder_offload_fail",
    "persona_dispatch_unhandled",
    "elder_llm_degraded",
    "enforcement_gap",
    "github_api_errors",
    "check_run_stuck",
    "inbound_webhook_silence",
    "backend_unusable",
    "cf_secret_mismatch",
    "uptime",
    "credential_acquisition_fail",
)

_ALL_QUEUE_MONITOR_FIELDS = (
    "cave_jobs_backlog",
    "rerun_backlog",
    "cave_results_backlog",
    "rerun_dlq",
    "cave_jobs_dlq",
    "cave_results_dlq",
    "telemetry_health",
)


@pulumi.runtime.test
def test_every_monitor_carries_managed_by_pulumi_tag():
    """The infrastructure org's fleet-wide 'hand-made monitor' audit flags any
    Datadog monitor without a `managed_by:pulumi` tag as not Pulumi-owned,
    even when it demonstrably is. Every monitor this stack creates - across
    all three builders, not just the ones routed through `_common_tags` -
    must carry it, or the audit keeps false-flagging grug's own monitors."""
    import pulumi_datadog as datadog

    from components import dd_monitors

    provider = datadog.Provider("test-dd-managed-by", api_key="x", app_key="y")

    all_bundle = dd_monitors.create_all(
        env="prod",
        notify_handle="@webhook-grug-discord-monitoring",
        webhook_public_url="https://webhook.example/webhook/github",
        api_public_url="https://api.example",
        provider=provider,
    )
    deploy_bundle = dd_monitors.create_deploy_monitors(env="prod", provider=provider)
    queue_bundle = dd_monitors.create_owned_queue_monitors(env="prod", provider=provider)

    resources = (
        [(f, getattr(all_bundle, f)) for f in _ALL_MONITOR_FIELDS]
        + [("rollback_fired", deploy_bundle.rollback_fired)]
        + [(f, getattr(queue_bundle, f)) for f in _ALL_QUEUE_MONITOR_FIELDS]
    )

    def _check(all_tags):
        for (name, _), tags in zip(resources, all_tags, strict=True):
            assert "managed_by:pulumi" in (tags or []), (
                f"{name}: missing managed_by:pulumi tag, got {tags}"
            )

    return pulumi.Output.all(*[r.tags for _, r in resources]).apply(_check)
