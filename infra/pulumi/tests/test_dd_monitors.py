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

import pulumi

from components.dd_monitors import (
    all_ksm_monitor_queries,
    consumer_queue_age_query,
    crashloop_query,
    poller_cronjob_unhealthy_query,
    restart_spike_query,
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


def test_restart_spike_query_is_namespace_scoped() -> None:
    q = restart_spike_query()
    assert "kubernetes_state.container.restarts" in q
    assert "kube_namespace:grug" in q
    assert "by {pod_name}" in q


def test_consumer_queue_age_covers_both_consumed_fifos() -> None:
    """#379 fold-in: consumer that is 'ready' but silently not draining."""
    q = consumer_queue_age_query()
    assert "aws.sqs.approximate_age_of_oldest_message" in q
    assert "grug-rerun-jobs.fifo" in q
    assert "grug-cave-results.fifo" in q
    assert "> 600" in q


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
