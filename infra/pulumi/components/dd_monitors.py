"""Datadog monitor + synthetic factories for grug observability.

Per memory `reference_macro_chef_outage_2026_04_28`: the macro-chef
2026-04-28 outage was 0/20 monitors firing because the monitor `env`
tag didn't match the actual log/trace `env` tag (one said `dev`, one
`prod`). EVERY monitor here pulls `env` from the stack name on creation
so it can never drift. Tag matrix is identical across all monitors:

    env:<stack>          # dev | prod  (matches DD_ENV)
    service:<grug-svc>   # the service monitored: namespace-level `grug` for
                         #   cross-workload monitors, or a specific workload
                         #   (grug-webhook | grug-api | grug-poller | grug-consumer)
    team:grug

Notification handle = an SNS topic ARN OR a `@user@host`-style mention
that DD knows how to route. v1 uses a Discord webhook because that's the
operator's existing notify path.
"""

from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_datadog as datadog


@dataclass
class _MonitorBundle:
    workload_not_ready: datadog.Monitor
    crashloop: datadog.Monitor
    restart_spike: datadog.Monitor
    poller_cronjob: datadog.Monitor
    sig_verify_fail: datadog.Monitor
    elder_offload_fail: datadog.Monitor
    persona_dispatch_unhandled: datadog.Monitor
    elder_llm_degraded: datadog.Monitor
    enforcement_gap: datadog.Monitor
    cf_secret_mismatch: datadog.Monitor
    uptime: datadog.SyntheticsTest
    credential_acquisition_fail: datadog.Monitor


def _common_tags(env: str, service: str) -> list[str]:
    return [f"env:{env}", f"service:{service}", "team:grug"]


# --- k8s-native query builders (#406) ---------------------------------------
# Post-Lambda, error-rate/availability and crash detection come from Kubernetes
# State Metrics (KSM), which DO flow for the grug namespace. These are PURE
# string builders so they're unit-testable without a Pulumi runtime.
#
# CRITICAL: scope by `kube_namespace:grug` ONLY — the stack `env` tag does NOT
# propagate onto KSM series, so adding `env:<stack>` to a KSM query silently
# matches nothing (permanent No Data) — the exact trap that hid the 2026-06-14
# outage. (The monitor TAGS still carry env for routing/inventory; the QUERY
# must not.)
_NS = "kube_namespace:grug"


def workload_not_ready_query() -> str:
    """Any grug Deployment (api/webhook/consumer) with ZERO ready replicas for
    a full 10m. `max(last_10m) < 1` tolerates brief deploy blips (a Recreate
    consumer momentarily at 0 during rollout) and fires only on a sustained
    outage — the availability equivalent of the retired Lambda 5xx alert."""
    return (
        "max(last_10m):max:kubernetes_state.deployment.replicas_ready"
        "{" + _NS + "} by {kube_deployment} < 1"
    )


def crashloop_query() -> str:
    """Any grug pod (incl grug-consumer) in CrashLoopBackOff. DD lowercases
    tag values, so the waiting reason is `crashloopbackoff`."""
    return (
        "max(last_5m):max:kubernetes_state.container.status_report.count.waiting"
        "{" + _NS + ",reason:crashloopbackoff} by {pod_name} > 0"
    )


def restart_spike_query() -> str:
    """A container that gains >3 restarts in 10m — catches IN-PLACE flapping
    (kubelet restarting the same container: OOMKill loops, a crashing thread)
    that recovers before settling into a CrashLoopBackOff state. `change()`
    measures the increase in the cumulative restart counter.

    Scope/known limit: grouped `by {pod_name}` and the counter RESETS on pod
    recreation, so controller-driven pod CHURN (each new pod_name starts at 0)
    is under-counted here - the crashloop monitor is the backstop for that
    case. Bias is toward under-firing, never false-firing."""
    return (
        "change(max(last_10m),last_10m):max:kubernetes_state.container.restarts"
        "{" + _NS + "} by {pod_name} > 3"
    )


def credential_acquisition_failure_query(env: str) -> str:
    """#389: any workload failing to acquire (or prove) Roles Anywhere
    credentials. Keys on the shared aws_identity event name plus
    botocore's CredentialRetrievalError string so BOTH the boot-proof
    path and a mid-run SDK acquisition failure alert. All four grug
    services emit it; one monitor covers the fleet."""
    # service:grug-* on purpose (audit #389-1): auto-covers any future
    # grug service without editing the monitor.
    return (
        f'logs("service:grug-* env:{env} '
        '(roles_anywhere_identity_failed OR CredentialRetrievalError '
        'OR NoCredentialsError OR InvalidClientTokenId)")'
        '.index("*").rollup("count").last("15m") > 0'
    )


def poller_cronjob_unhealthy_query() -> str:
    """#379: grug-poller CronJob (every 15m) has not SUCCEEDED in >60m (4
    missed cycles) — it stopped reconciling reactions/stuck PRs silently."""
    return (
        "max(last_15m):max:kubernetes_state.cronjob.duration_since_last_successful"
        "{" + _NS + ",kube_cronjob:grug-poller} > 3600"
    )


def all_ksm_monitor_queries() -> list[str]:
    """Every NEW k8s-runtime monitor query (all KSM-based). The
    lambda-retirement guard test iterates this set."""
    return [
        workload_not_ready_query(),
        crashloop_query(),
        restart_spike_query(),
        poller_cronjob_unhealthy_query(),
    ]


# --- owned SQS depth gauges (#379) ------------------------------------
# aws.sqs.* is not collected by the DD AWS integration in this org, so any
# monitor on it is permanent No Data with notify_no_data=false - silently
# blind (three shipped that way). The consumer emits owned gauges instead;
# full rationale + semantics live in specs/DESIGN.md ("Owned queue-depth
# telemetry"). Backlog monitors use SUSTAINED depth (min over the window,
# per queue) because age-of-oldest is CloudWatch-only; at grug's sparse
# volumes that approximates the age signal (a continuously-busy-but-
# draining queue would false-positive, and a lone poison message cycling
# through its visibility timeout is caught by the DLQ monitors after
# redrive, not here).

_QUEUE_GAUGE = "grug.sqs.messages_visible"
_TELEMETRY_OK_GAUGE = "grug.sqs.telemetry_queue_ok"

# Every query filters env explicitly (gemini peer review, PR #516): the
# owned gauges carry a reliable env tag (emit_gauge appends it), and an
# unscoped query would let a healthy dev emitter mask a dead prod one.
# One monitor per queue with a simple {env,queue} filter - no OR/AND
# boolean filter syntax, which is an apply-time validation risk the
# preview cannot catch.


def _backlog_query(env: str, queue: str) -> str:
    """SUSTAINED depth: the queue held messages across the FULL window
    (min > 0 = never drained once). Per-queue simple filter."""
    return f"min(last_15m):max:{_QUEUE_GAUGE}{{env:{env},queue:{queue}}} > 0"


def _dlq_query(env: str, queue: str) -> str:
    """Any-message DLQ depth: a single poison message is worth a look."""
    return f"max(last_15m):max:{_QUEUE_GAUGE}{{env:{env},queue:{queue}}} > 0"


def cave_jobs_backlog_query(env: str) -> str:
    """Cave fallback jobs queue not draining for 15m - the connector is
    down or can't reach the Cave. (Was: age-of-oldest > 10m on aws.sqs.*.)"""
    return _backlog_query(env, "grug-cave-jobs.fifo")


def rerun_backlog_query(env: str) -> str:
    """The consumer's rerun queue not draining for 15m - a stuck/poisoned
    consumer loop the pod watchdog can't see (#379's original ask)."""
    return _backlog_query(env, "grug-rerun-jobs.fifo")


def cave_results_backlog_query(env: str) -> str:
    """The consumer's cave-results queue not draining for 15m."""
    return _backlog_query(env, "grug-cave-results.fifo")


def rerun_dlq_depth_query(env: str) -> str:
    """Any message in the re-run DLQ - an operator's re-run burned its
    retries and did not complete."""
    return _dlq_query(env, "grug-rerun-jobs-dlq.fifo")


def cave_jobs_dlq_depth_query(env: str) -> str:
    """Any message in the cave-jobs DLQ - a poison fallback job."""
    return _dlq_query(env, "grug-cave-jobs-dlq.fifo")


def cave_results_dlq_depth_query(env: str) -> str:
    """Any message in the cave-results DLQ - a poison fallback result."""
    return _dlq_query(env, "grug-cave-results-dlq.fifo")


def telemetry_health_query(env: str) -> str:
    """The telemetry family's ONE heartbeat, per queue: the consumer emits
    a 1/0 success boolean per queue per sweep; a queue whose success rate
    drops below half over the window pages AND names itself. avg-of-avg
    (not max) so an INTERMITTENT partial failure cannot hide behind one
    good sweep (codex peer review, PR #516); a single throttle blip in an
    otherwise-healthy window does not flap. The paired monitor is the
    family's only notify_no_data=TRUE: a queue vanishing from emission
    (rename) no-datas its group, and total silence == consumer down."""
    return (
        f"avg(last_15m):avg:{_TELEMETRY_OK_GAUGE}{{env:{env}}}"
        " by {queue} < 0.5"
    )


def deploy_rollback_query(env: str) -> str:
    """#499: the deploy's post-apply synthetic failed and last-good
    digests were re-applied (or a manual rollback dispatch ran). The
    runner emits grug.deploy.rollback as a COUNT via DogStatsD to a
    node's agent hostPort; any occurrence pages - a rollback is always
    operator-relevant. notify_no_data=false (fires rarely by design)."""
    return (
        f"sum(last_30m):sum:grug.deploy.rollback{{env:{env}}}.as_count() > 0"
    )


def all_owned_queue_queries(env: str = "prod") -> list[str]:
    """Every owned-gauge queue monitor query - the aws.sqs retirement
    guard test iterates this set."""
    return [
        cave_jobs_backlog_query(env),
        rerun_backlog_query(env),
        cave_results_backlog_query(env),
        rerun_dlq_depth_query(env),
        cave_jobs_dlq_depth_query(env),
        cave_results_dlq_depth_query(env),
        telemetry_health_query(env),
    ]


@dataclass(frozen=True)
class _DeployMonitorBundle:
    rollback_fired: datadog.Monitor


def create_deploy_monitors(
    *,
    env: str,
    notify_handle: str,
    provider: datadog.Provider,
) -> _DeployMonitorBundle:
    """#499 release-chain monitors. In the component so the synth test
    pins (query, notify_no_data) like the queue family."""
    rollback_fired = datadog.Monitor(
        "grug-deploy-rollback-fired",
        type="metric alert",
        name="[grug] Deploy auto-rollback fired",
        message=(
            f"{notify_handle}\n"
            "A grug deploy's post-apply synthetic FAILED and the last-good "
            "digests were re-applied (or a manual rollback dispatch ran). "
            "Prod is on the PREVIOUS images - the bad merge is still on "
            "main. Read the failed deploy run's summary, revert or fix "
            "forward, and re-deploy.\n"
            "Runbook: docs/RUNBOOK.md#deploy-rollback"
        ),
        query=deploy_rollback_query(env),
        tags=[f"env:{env}", "service:grug-webhook", "team:grug"],
        notify_no_data=False,
        require_full_window=False,
        priority=2,
        opts=pulumi.ResourceOptions(provider=provider),
    )
    return _DeployMonitorBundle(rollback_fired=rollback_fired)


@dataclass(frozen=True)
class _QueueMonitorBundle:
    cave_jobs_backlog: datadog.Monitor
    rerun_backlog: datadog.Monitor
    cave_results_backlog: datadog.Monitor
    rerun_dlq: datadog.Monitor
    cave_jobs_dlq: datadog.Monitor
    cave_results_dlq: datadog.Monitor
    telemetry_health: datadog.Monitor


def create_owned_queue_monitors(
    *,
    env: str,
    notify_handle: str,
    provider: datadog.Provider,
) -> _QueueMonitorBundle:
    """The owned-gauge queue monitor family (#379): one monitor per queue
    with a simple {env,queue} filter, plus the per-queue telemetry-health
    heartbeat. Lives in the component (not the composition root) so the
    synth test can pin each monitor's (query, notify_no_data) pair - the
    no-data pager placement is the load-bearing bit of the family design
    and a silent flip re-creates the blind-monitor trap.

    require_full_window=False everywhere: a 60s-cadence DogStatsD gauge
    is exactly the sparse metric DD warns about; the provider default
    happens to be False today, but the safety should not rest on a
    cross-version provider default.
    """
    opts = pulumi.ResourceOptions(provider=provider)
    runbook = "Runbook: docs/RUNBOOK.md#elder-async-offload"

    def _monitor(
        resource_name: str,
        display_name: str,
        body: str,
        query: str,
        service: str,
        *,
        priority: int = 3,
        notify_no_data: bool = False,
        no_data_timeframe: int | None = None,
    ) -> datadog.Monitor:
        return datadog.Monitor(
            resource_name,
            type="metric alert",
            name=display_name,
            message=f"{notify_handle}\n{body}\n{runbook}",
            query=query,
            tags=[f"env:{env}", f"service:{service}", "team:grug"],
            notify_no_data=notify_no_data,
            no_data_timeframe=no_data_timeframe,
            require_full_window=False,
            priority=priority,
            opts=opts,
        )

    cave_jobs_backlog = _monitor(
        "grug-cave-jobs-age",  # historical URN, kept for in-place update
        "[grug-webhook] Cave fallback jobs queue backing up",
        "grug-cave-jobs (Elder cave fallback) has not drained for 15min - "
        "the grug-cave-connector isn't draining (down, or can't reach the "
        "Cave). Fallback reviews stay `errored` until it recovers.",
        cave_jobs_backlog_query(env),
        "grug-webhook",
        priority=4,
    )

    rerun_backlog = _monitor(
        "grug-rerun-backlog",
        "[grug-consumer] rerun queue not draining (15min)",
        "grug-rerun-jobs has had messages sitting for a full 15min window "
        "- the consumer is stuck (poisoned loop, IAM regression) even if "
        "its pod reads healthy.",
        rerun_backlog_query(env),
        "grug-consumer",
    )

    cave_results_backlog = _monitor(
        "grug-cave-results-backlog",
        "[grug-consumer] cave-results queue not draining (15min)",
        "grug-cave-results has had messages sitting for a full 15min "
        "window - the consumer is stuck (poisoned loop, IAM regression) "
        "even if its pod reads healthy.",
        cave_results_backlog_query(env),
        "grug-consumer",
    )

    rerun_dlq = _monitor(
        "grug-rerun-dlq-depth",
        "[grug] Re-run DLQ has messages",
        "grug-rerun-jobs-dlq has messages - a re-run job exhausted its "
        "retries (GitHub fetch failing, or a malformed job). The "
        "operator's re-run did not complete; inspect the DLQ message.",
        rerun_dlq_depth_query(env),
        "grug-api",
    )

    cave_jobs_dlq = _monitor(
        "grug-cave-jobs-dlq-depth",
        "[grug] Cave jobs DLQ has messages",
        "grug-cave-jobs-dlq has messages - a poison fallback job exhausted "
        "its retries. The fallback review for that PR did not complete; "
        "inspect the DLQ message.",
        cave_jobs_dlq_depth_query(env),
        "grug-webhook",
    )

    cave_results_dlq = _monitor(
        "grug-cave-results-dlq-depth",
        "[grug] Cave results DLQ has messages",
        "grug-cave-results-dlq has messages - a poison fallback result "
        "exhausted its retries; inspect the DLQ message.",
        cave_results_dlq_depth_query(env),
        "grug-webhook",
    )

    telemetry_health = _monitor(
        "grug-queue-telemetry-health",
        "[grug-consumer] Queue telemetry degraded (15min)",
        "A queue's depth-telemetry success rate fell below 50% over 15min "
        "({{queue.name}}) - its depth monitor is partially BLIND. Check "
        "queue_depth_probe_failed warnings for the botocore error code "
        "(AccessDenied = the ra-grug IAM grant regressed). NO DATA here "
        "means the queue vanished from emission (rename) or the consumer/"
        "telemetry thread is down entirely - treat as consumer down.",
        telemetry_health_query(env),
        "grug-consumer",
        notify_no_data=True,
        no_data_timeframe=30,
    )

    return _QueueMonitorBundle(
        cave_jobs_backlog=cave_jobs_backlog,
        rerun_backlog=rerun_backlog,
        cave_results_backlog=cave_results_backlog,
        rerun_dlq=rerun_dlq,
        cave_jobs_dlq=cave_jobs_dlq,
        cave_results_dlq=cave_results_dlq,
        telemetry_health=telemetry_health,
    )


def create_all(
    *,
    env: str,
    notify_handle: str,
    webhook_public_url: str,
    api_public_url: str,
    provider: datadog.Provider,
) -> _MonitorBundle:
    """Build the v1 monitor set + synthetic. Returns the bundle so the
    composition root can pulumi.export their IDs for runbook lookup."""

    opts = pulumi.ResourceOptions(provider=provider)

    # Workload availability (#406) — any grug Deployment with ZERO ready
    #    replicas for a full 10m. Replaces the retired aws.lambda 5xx monitors:
    #    on k8s, "the service is broken" = "no pod is serving". Multi-alert by
    #    deployment so webhook/api/consumer each page independently.
    workload_not_ready = datadog.Monitor(
        "grug-workload-not-ready",
        type="metric alert",
        name="[grug] Workload has zero ready replicas (10min)",
        message=(
            f"{notify_handle}\n"
            "A grug workload (grug-webhook / grug-api / grug-consumer) has had "
            "ZERO ready replicas for 10 minutes — that service is down "
            "(crashloop, image pull, scheduling, or a failing readiness probe). "
            "PR check-runs / dashboard / queue draining are broken for it.\n"
            "Runbook: docs/RUNBOOK.md#workload-not-ready"
        ),
        query=workload_not_ready_query(),
        # service:grug (namespace-level) NOT grug-webhook — this monitor covers
        # all three workloads, so an operator filtering by service:grug-consumer
        # must still find it.
        tags=_common_tags(env, "grug"),
        # replicas_ready is a CONTINUOUS KSM gauge (always present for a live
        # Deployment), so No Data here is NOT benign - it means the agent/KSM
        # check stopped reporting, i.e. we've gone blind on every k8s signal
        # (the same silent-can't-fire trap that hid the 2026-06-14 outage).
        # Page on it, with a timeframe well above the 10m window so a brief
        # agent restart doesn't false-page.
        notify_no_data=True,
        no_data_timeframe=30,
        priority=2,
        opts=opts,
    )

    # CrashLoopBackOff (#406) — directly catches the failure mode the
    #    2026-06-14 outage hit (grug-consumer crash-looping, unmonitored).
    #    Covers EVERY grug pod incl the no-HTTP consumer.
    crashloop = datadog.Monitor(
        "grug-crashloop",
        type="metric alert",
        name="[grug] Pod in CrashLoopBackOff (5min)",
        message=(
            f"{notify_handle}\n"
            "A grug pod is in CrashLoopBackOff — it is repeatedly failing to "
            "start. Check the pod's recent logs and `kubectl describe`.\n"
            "Runbook: docs/RUNBOOK.md#crashloop"
        ),
        query=crashloop_query(),
        tags=_common_tags(env, "grug"),  # all-workload (see workload-not-ready)
        # CONDITIONAL metric: the waiting-reason series only exists while a pod
        # is actually in CrashLoopBackOff, so No Data == healthy here. (Unlike
        # the continuous gauges above, which page on No Data.)
        notify_no_data=False,
        priority=2,
        opts=opts,
    )

    # Restart spike (#406) — flapping that recovers before settling into a
    #    CrashLoopBackOff state still signals trouble (OOMKill, transient dep).
    restart_spike = datadog.Monitor(
        "grug-restart-spike",
        type="metric alert",
        name="[grug] Pod restarting repeatedly (>3 in 10min)",
        message=(
            f"{notify_handle}\n"
            "A grug pod gained more than 3 restarts in 10 minutes — it is "
            "flapping (OOMKill, a crashing thread, or a transient dependency). "
            "Check the pod's restart reason and recent logs.\n"
            "Runbook: docs/RUNBOOK.md#crashloop"
        ),
        query=restart_spike_query(),
        tags=_common_tags(env, "grug"),  # all-workload (see workload-not-ready)
        # CONDITIONAL: change() of the restart counter only registers when
        # restarts are climbing, so No Data == healthy here too.
        notify_no_data=False,
        priority=3,
        opts=opts,
    )

    # (The consumer queue monitors now exist on the OWNED grug.sqs gauges -
    #  see create_owned_queue_monitors above; aws.sqs.* remains uncollected
    #  in this org, which is why they are owned.)

    # Poller CronJob health (#379 fold-in) — grug-poller (every 15m) has not
    #    SUCCEEDED in >60m. The poller reconciles reactions / stuck PRs; if it
    #    stops, that backlog rots with no other signal.
    poller_cronjob = datadog.Monitor(
        "grug-poller-cronjob-unhealthy",
        type="metric alert",
        name="[grug-poller] CronJob has not succeeded in 60min",
        message=(
            f"{notify_handle}\n"
            "The grug-poller CronJob (runs every 15m) has not completed "
            "successfully in over an hour — reaction/stuck-PR reconciliation "
            "has stopped. Check the most recent grug-poller Job's logs.\n"
            "Runbook: docs/RUNBOOK.md#poller-cronjob"
        ),
        query=poller_cronjob_unhealthy_query(),
        tags=_common_tags(env, "grug-poller"),
        # duration_since_last_successful is CONTINUOUS once the CronJob has run,
        # so No Data = the CronJob vanished or KSM stopped scraping it - a real
        # "poller is gone" signal worth paging (not benign). Conditional KSM
        # monitors above (crashloop/restart-spike) correctly keep this False.
        notify_no_data=True,
        no_data_timeframe=30,
        priority=3,
        opts=opts,
    )

    # 3) Webhook signature-verify failure rate > 0.1/min over 10min —
    #    legit GitHub deliveries always verify; sustained failures mean
    #    either someone's probing OR the secret rotated incorrectly.
    sig_verify_fail = datadog.Monitor(
        "grug-webhook-sig-verify-fail",
        type="log alert",
        name="[grug-webhook] HMAC signature-verify failures > 0.1/min (10min)",
        message=(
            f"{notify_handle}\n"
            "Webhook is rejecting GitHub deliveries on signature mismatch. "
            "Either the App webhook secret rotated and SSM is stale, OR "
            "an outside party is probing /webhook/github.\n"
            "Runbook: docs/RUNBOOK.md#sig-verify-fail"
        ),
        query=(
            f'logs("service:grug-webhook env:{env} '
            'webhook_signature_invalid").index("*").rollup("count").last("10m") > 1'
        ),
        tags=_common_tags(env, "grug-webhook"),
        notify_no_data=False,
        priority=3,
        opts=opts,
    )

    # 3c) Roles Anywhere credential-acquisition failures (#389 AC): the
    #     cert path is fleet-wide now; a broken chain/cert/trust surfaces
    #     as roles_anywhere_identity_failed (boot proof) or botocore
    #     CredentialRetrievalError (mid-run). Zero tolerance - one event
    #     pages (single-operator scale, realistic pager).
    credential_acquisition_fail = datadog.Monitor(
        "grug-roles-anywhere-credential-fail",
        type="log alert",
        name="[grug] Roles Anywhere credential acquisition failing (15min)",
        message=(
            f"{notify_handle}\n"
            "A grug workload cannot acquire (or prove) Roles Anywhere "
            "credentials - broken cert chain, stuck renewal, wrong "
            "identity, or Roles Anywhere outage. Pods fail loud at boot; "
            "the poller fails per tick.\n"
            "Runbook: docs/RUNBOOK.md#roles-anywhere-credential-path-grug-poller-tracer-388"
        ),
        query=credential_acquisition_failure_query(env),
        tags=_common_tags(env, "grug"),
        notify_no_data=False,
        priority=2,
        opts=opts,
    )

    # 3b) Async persona offload failures (#272 Elder, #466 Guard, #469 Smasher).
    #     The async personas run off the ACK path; if an enqueue throttles
    #     (`*_enqueue_failed`) or an async worker crashes (`*_job_unhandled`),
    #     local async run is dropped. Durable Elder enqueue failures make the
    #     webhook delivery fail so the 15-minute replay poller can recover it;
    #     the alert still needs the historical elder_enqueue_failed event name.
    #     Other local async personas rely on their self-recovery/next event.
    elder_offload_fail = datadog.Monitor(
        "grug-webhook-elder-offload-fail",
        type="log alert",
        name="[grug-webhook] Async persona offload failures > 0 (15min)",
        message=(
            f"{notify_handle}\n"
            "An async persona handoff failed (Elder/Guard enqueue or worker) OR "
            "the in-cluster Cave secret judge failed closed (#439 - secret "
            "candidates suppressed this pass, never sent to SaaS) - "
            "the async path - either the enqueue failed or the async worker "
            "hit an unhandled error. Durable Elder enqueue failures are replayed "
            "by the 15-minute delivery poller; local async failures use their "
            "self-recovery path or the next PR event. Check grug-webhook logs "
            "for the delivery_id.\n"
            "Runbook: docs/RUNBOOK.md#elder-async-offload"
        ),
        query=(
            f'logs("service:grug-webhook env:{env} '
            '(elder_enqueue_failed OR elder_job_unhandled OR guard_enqueue_failed '
            'OR guard_job_unhandled OR smasher_enqueue_failed OR smasher_job_unhandled '
            'OR cave_judge_failed_secrets_suppressed)").index("*")'
            '.rollup("count").last("15m") > 0'
        ),
        tags=_common_tags(env, "grug-webhook"),
        notify_no_data=False,
        priority=2,
        opts=opts,
    )

    # 3c) Registry dispatch loop unhandled persona failure (#465, ADR-0010).
    #     The per-persona isolation guard 200s an INLINE persona's failure
    #     (Chief - a retry would duplicate its publish), so this log-line
    #     is the primary signal for a broken inline persona module (bad
    #     deploy, import failure, escaped exception). An ASYNC persona's
    #     handoff failure (Elder) is re-raised instead and 500s (covered by
    #     the workload / uptime monitors), but it ALSO logs this event, so
    #     alerting on any occurrence catches both classes.
    persona_dispatch_unhandled = datadog.Monitor(
        "grug-webhook-persona-dispatch-unhandled",
        type="log alert",
        name="[grug-webhook] Persona dispatch unhandled failure > 0 (15min)",
        message=(
            f"{notify_handle}\n"
            "A persona blew through its webhook dispatch entry - the "
            "delivery was ACKed 200 with result=unhandled_error, so GitHub "
            "and the replay sweep both consider it delivered (ADR-0010 "
            "replay-invisibility trade). The log carries persona, "
            "delivery_id, head_sha and kind. If it is Elder, the review "
            "was dropped and re-triggers on the next push; if it is Chief, "
            "the DoR check-run never posted and the PR may sit ungated.\n"
            "Runbook: docs/RUNBOOK.md#elder-async-offload"
        ),
        query=(
            f'logs("service:grug-webhook env:{env} '
            'persona_dispatch_unhandled").index("*")'
            '.rollup("count").last("15m") > 0'
        ),
        tags=_common_tags(env, "grug-webhook"),
        notify_no_data=False,
        priority=2,
        opts=opts,
    )

    # 3b) Elder fallback FAILED — a review was dropped and the owned backstop
    #     did not save it. Post-cutover semantics (2026-06-10, the cave
    #     fallback is LIVE — #310/#316/#313, flag ON, E2E verified on
    #     infra#1142): the SaaS backends are unfunded BY DESIGN, so
    #     `code_review_llm_degraded` (clouds down) now fires on essentially
    #     every review and then the Cave heals it — alerting on it would be a
    #     permanent storm. The page-worthy signal is the fallback chain
    #     breaking: the Cave answered DEGRADED (`elder_fallback_result_degraded`
    #     — the webhook leaves the verdict errored, "no lies"), the enqueue
    #     failed, the queue URL was missing, or a big diff couldn't spill.
    #     Awareness that the backstop is firing at all = the P4
    #     cave-fallback-fired monitor (#312); the DLQ-depth + queue-age
    #     monitors cover the stuck-airlock cases.
    elder_llm_degraded = datadog.Monitor(
        "grug-webhook-elder-llm-degraded",
        type="log alert",
        name="[grug-webhook] Elder fallback failed — review dropped for real (30m)",
        message=(
            f"{notify_handle}\n"
            "A PR review was dropped AND Elder's owned cave fallback did not "
            "heal it — the Cave answered degraded, the fallback enqueue failed, "
            "or a large diff couldn't spill to S3. (Clouds-down alone is "
            "expected — the SaaS backends are unfunded by design; do NOT top up "
            "OpenRouter/Poolside.) Check the grug-cave-connector pod on the LAN "
            "worker, the cave egress proxy, the Cave itself, and the cave DLQs. "
            "The errored Activity row can be re-run from the dashboard once "
            "the Cave recovers.\n"
            "Runbook: docs/RUNBOOK.md#elder-async-offload"
        ),
        query=(
            f'logs("service:grug-webhook env:{env} (elder_fallback_result_degraded '
            "OR elder_fallback_enqueue_failed OR elder_fallback_no_queue_url "
            'OR elder_fallback_diff_too_large_no_bucket OR elder_fallback_diff_spill_failed)")'
            '.index("*").rollup("count").last("30m") > 0'
        ),
        tags=_common_tags(env, "grug-webhook"),
        notify_no_data=False,
        # P2: the backstop itself failed — reviews are being lost for real.
        priority=2,
        opts=opts,
    )

    # (The Lambda cold-start p99 monitor — aws.lambda.enhanced.init_duration —
    # was RETIRED with the k8s migration (#406): there is no Lambda cold start
    # on k8s. Pod-start latency is covered by the workload-not-ready /
    # CrashLoopBackOff monitors above; request latency by the /livez synthetic.)

    # 5) Enforcement gap detector — any repo with enforcement_type:none
    #    for >1h means a TPM-enabled repo has no merge gate. DogStatsD
    #    metric emitted by enforcement.py on every state change.
    enforcement_gap = datadog.Monitor(
        "grug-enforcement-gap",
        type="metric alert",
        name="[grug] Enforcement gap — repo with enforcement_type:none > 1h",
        message=(
            f"{notify_handle}\n"
            "A TPM-enabled repo has had no enforcement for >1 hour. "
            "PRs can merge without passing the DoR check.\n"
            "Runbook: docs/RUNBOOK.md#enforcement-gap"
        ),
        query=(
            "min(last_1h):min:grug.enforcement.state"
            "{enforcement_type:none,env:" + env + "} by {repo} < 0.5"
        ),
        tags=_common_tags(env, "grug-webhook") + ["enforcement:gap"],
        notify_no_data=False,
        priority=2,
        opts=opts,
    )

    # 6) CF→AWS auth-boundary header-mismatch rate. A burst means the
    #    secret got out of sync between the CF Worker binding and the
    #    SSM param the Lambda middleware reads — usually a rotation
    #    where deploy.sh ran but the Lambda hasn't cold-cycled yet, OR
    #    a real attacker probing the Function URL directly. Catches
    #    both. Excludes /livez since that path is always-exempt.
    cf_secret_mismatch = datadog.Monitor(
        "grug-cf-secret-mismatch",
        type="log alert",
        name="[grug] CF auth-boundary mismatch > 10 in 10min",
        message=(
            f"{notify_handle}\n"
            "X-Grug-CF-Secret mismatches detected on non-/livez requests. "
            "Either CF Worker binding ↔ SSM param drifted (rotation in "
            "progress?) or someone is probing the Function URL directly.\n"
            "Runbook: docs/RUNBOOK.md#cf-secret-mismatch"
        ),
        # Parenthesize the OR explicitly so DD scopes the env filter to
        # both services. Without parens, DD reads "service:grug-api OR
        # (service:grug-webhook AND env:<env> AND ...)" and grug-api
        # alerts fire across ALL envs.
        query=(
            f'logs("(service:grug-api OR service:grug-webhook) env:{env} '
            'cf_shared_secret_mismatch").index("*").rollup("count").last("10m") > 10'
        ),
        tags=_common_tags(env, "grug-api") + ["auth:cf-boundary"],
        notify_no_data=False,
        priority=2,
        opts=opts,
    )

    # 7) Synthetic uptime — hit GET /livez (no IO, returns 200). Earlier
    #    design POSTed a fake-sig body expecting 401, but that triggered
    #    webhook_signature_invalid every 5 min → false-positive infinite
    #    alert loop on monitor #3 (Codex P1, Slice 9).
    livez_url = webhook_public_url.replace("/webhook/github", "/livez")
    uptime = datadog.SyntheticsTest(
        "grug-webhook-uptime",
        type="api",
        subtype="http",
        name=f"[grug-webhook][{env}] uptime — GET /livez → 200",
        status="live",
        locations=["aws:us-east-1"],
        message=(
            f"{notify_handle}\n"
            f"Synthetic uptime check on {livez_url} "
            "failing. GitHub webhook deliveries may be dropping.\n"
            "Runbook: docs/RUNBOOK.md#uptime-fail"
        ),
        request_definition=datadog.SyntheticsTestRequestDefinitionArgs(
            method="GET",
            url=livez_url,
        ),
        assertions=[
            datadog.SyntheticsTestAssertionArgs(
                type="statusCode", operator="is", target="200",
            ),
            datadog.SyntheticsTestAssertionArgs(
                type="responseTime", operator="lessThan", target="5000",
            ),
        ],
        options_list=datadog.SyntheticsTestOptionsListArgs(
            tick_every=300,  # 5 min
            min_failure_duration=600,  # alert after 2 consecutive fails
            min_location_failed=1,
            retry=datadog.SyntheticsTestOptionsListRetryArgs(count=2, interval=1000),
        ),
        tags=_common_tags(env, "grug-webhook") + ["check:uptime"],
        opts=opts,
    )

    _ = api_public_url  # reserved for future api-uptime synthetic

    return _MonitorBundle(
        workload_not_ready=workload_not_ready,
        crashloop=crashloop,
        restart_spike=restart_spike,
        poller_cronjob=poller_cronjob,
        sig_verify_fail=sig_verify_fail,
        elder_offload_fail=elder_offload_fail,
        persona_dispatch_unhandled=persona_dispatch_unhandled,
        elder_llm_degraded=elder_llm_degraded,
        enforcement_gap=enforcement_gap,
        cf_secret_mismatch=cf_secret_mismatch,
        uptime=uptime,
        credential_acquisition_fail=credential_acquisition_fail,
    )
