"""Datadog monitor + synthetic factories for grug observability.

Per memory `reference_macro_chef_outage_2026_04_28`: the macro-chef
2026-04-28 outage was 0/20 monitors firing because the monitor `env`
tag didn't match the actual log/trace `env` tag (one said `dev`, one
`prod`). EVERY monitor here pulls `env` from the stack name on creation
so it can never drift. Tag matrix is identical across all monitors:

    env:<stack>          # dev | prod  (matches DD_ENV)
    service:<grug-svc>   # grug-webhook | grug-api
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
    webhook_5xx: datadog.Monitor
    api_5xx: datadog.Monitor
    sig_verify_fail: datadog.Monitor
    elder_offload_fail: datadog.Monitor
    elder_llm_degraded: datadog.Monitor
    cold_start_p99: datadog.Monitor
    enforcement_gap: datadog.Monitor
    cf_secret_mismatch: datadog.Monitor
    key_rotation_fail: datadog.Monitor
    uptime: datadog.SyntheticsTest


def _common_tags(env: str, service: str) -> list[str]:
    return [f"env:{env}", f"service:{service}", "team:grug"]


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

    # 1) Webhook 5xx > 1% over 5min — pages. GitHub will silently retry
    #    5xx but a sustained burn means EVERY install's PR check is broken.
    webhook_5xx = datadog.Monitor(
        "grug-webhook-5xx",
        type="metric alert",
        name="[grug-webhook] 5xx error-rate > 1% (5min)",
        message=(
            f"{notify_handle}\n"
            "grug-webhook is returning 5xx > 1% of requests. PRs will "
            "appear to silently miss their check-run.\n"
            "Runbook: docs/RUNBOOK.md#webhook-5xx"
        ),
        query=(
            "sum(last_5m):"
            "( sum:aws.lambda.errors{functionname:grug-webhook,env:" + env
            + "}.as_count() "
            "/ sum:aws.lambda.invocations{functionname:grug-webhook,env:" + env
            + "}.as_count() "
            ") * 100 > 1"
        ),
        tags=_common_tags(env, "grug-webhook"),
        notify_no_data=False,
        priority=2,
        opts=opts,
    )

    # 2) API 5xx > 5% over 5min — degraded UX but not fully broken.
    api_5xx = datadog.Monitor(
        "grug-api-5xx",
        type="metric alert",
        name="[grug-api] 5xx error-rate > 5% (5min)",
        message=(
            f"{notify_handle}\n"
            "grug-api is returning 5xx > 5% of requests. Dashboard + "
            "OAuth flows are degraded.\n"
            "Runbook: docs/RUNBOOK.md#api-5xx"
        ),
        query=(
            "sum(last_5m):"
            "( sum:aws.lambda.errors{functionname:grug-api,env:" + env
            + "}.as_count() "
            "/ sum:aws.lambda.invocations{functionname:grug-api,env:" + env
            + "}.as_count() "
            ") * 100 > 5"
        ),
        tags=_common_tags(env, "grug-api"),
        notify_no_data=False,
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

    # 3b) Async Elder offload failures (#272). The Elder review runs off
    #     the ACK path via self-invoke; if the enqueue throttles
    #     (`elder_enqueue_failed`) or the async worker crashes
    #     (`elder_job_unhandled`), that review is DROPPED — by design we
    #     don't sync-fall-back (it would re-block the <10s ACK) and rely on
    #     the next push re-triggering. That "drop + re-trigger" is only safe
    #     if the drop is VISIBLE, so alert on any occurrence. (The duplicate-
    #     skip + claim-fail-open paths are NOT errors and are excluded.)
    elder_offload_fail = datadog.Monitor(
        "grug-webhook-elder-offload-fail",
        type="log alert",
        name="[grug-webhook] Elder async-offload failures > 0 (15min)",
        message=(
            f"{notify_handle}\n"
            "An Elder code-review was DROPPED off the async path — either the "
            "self-invoke enqueue failed (Lambda throttle) or the async worker "
            "hit an unhandled error. The review will not post until the PR is "
            "pushed again. Check grug-webhook logs for the delivery_id.\n"
            "Runbook: docs/RUNBOOK.md#elder-async-offload"
        ),
        query=(
            f'logs("service:grug-webhook env:{env} '
            '(elder_enqueue_failed OR elder_job_unhandled)").index("*")'
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

    # 4) Cold-start p99 > 3s over 15min — informational. Container
    #    Lambda cold-starts spike on image-uri swaps (every deploy).
    #    Threshold tuned to ignore deploy-burst, catch sustained drift.
    cold_start_p99 = datadog.Monitor(
        "grug-cold-start-p99",
        type="metric alert",
        name="[grug] Lambda cold-start p99 > 3s (15min)",
        message=(
            f"{notify_handle}\n"
            "Sustained cold-start p99 > 3s. PR check-runs feel slow. "
            "Consider provisioned concurrency if growth demands.\n"
            "Runbook: docs/RUNBOOK.md#cold-start"
        ),
        # Greptile P1 PR #48 — `aws.lambda.duration` measures total
        # invocation time (warm + cold); cold-start spikes get drowned
        # by warm calls. DD-extension-instrumented Lambdas emit init
        # duration as `aws.lambda.enhanced.init_duration` (Codex P2
        # follow-up; AWS-native `aws.lambda.init_duration` is empty on
        # DD-extension Lambdas).
        query=(
            "avg(last_15m):p99:aws.lambda.enhanced.init_duration"
            "{functionname:grug-webhook,env:" + env + "} > 3000"
        ),
        tags=_common_tags(env, "grug-webhook"),
        notify_no_data=False,
        priority=4,
        opts=opts,
    )

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

    # 6b) Key-rotator failure (#386 interim). The CronJob rotates the
    #     grug-k8s-pod access key every 12h; a failure emits `key_rotation_failed`
    #     and the Job exits non-zero. On failure the OLD key is kept valid (fail
    #     safe-open), so pods keep working - but the rotation is stuck and the
    #     exposure window stops shrinking until a human looks. Page on any
    #     failure log in a 13h window (just over one cycle).
    key_rotation_fail = datadog.Monitor(
        "grug-key-rotator-fail",
        type="log alert",
        name="[grug] AWS key-rotation failed (interim rotator)",
        message=(
            f"{notify_handle}\n"
            "The interim grug-k8s-pod key rotation failed. The old key is kept "
            "valid so pods still work, but rotation is stuck - check the "
            "grug-key-rotator Job logs (a dangling new key may need manual "
            "cleanup; AWS caps the user at 2 keys).\n"
            "Runbook: docs/RUNBOOK.md#key-rotation"
        ),
        query=(
            f'logs("service:grug-key-rotator env:{env} key_rotation_failed")'
            '.index("*").rollup("count").last("13h") > 0'
        ),
        tags=_common_tags(env, "grug-key-rotator"),
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
        webhook_5xx=webhook_5xx,
        api_5xx=api_5xx,
        sig_verify_fail=sig_verify_fail,
        elder_offload_fail=elder_offload_fail,
        elder_llm_degraded=elder_llm_degraded,
        cold_start_p99=cold_start_p99,
        enforcement_gap=enforcement_gap,
        cf_secret_mismatch=cf_secret_mismatch,
        key_rotation_fail=key_rotation_fail,
        uptime=uptime,
    )
