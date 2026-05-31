"""Datadog dashboard factory for grug observability.

Spec note: dashboards are observability CONFIG, not a behavioral domain
contract — like `dd_monitors.py` / `dd_rum.py`, they carry no temper spec.

`create_elder_health(...)` builds "Grug Elder — Code Review Health" via
`datadog.DashboardJson` (raw DD dashboard spec). The typed
`datadog.Dashboard` resource models widget args as deeply-nested Pulumi
objects that lag DD's newer query shapes; the raw-JSON resource takes the
exact spec the DD API consumes, so it's both more robust and diffs cleanly.
It is fully Pulumi-managed (reproducible per the all-Pulumi rule).

Data sources are grounded against what the Elder ACTUALLY emits (verified
against services/webhook/llm_client.py + personas/code_reviewer/dispatch.py
and the live DD metric catalog), NOT guessed:

  - LLM Obs span metrics `ml_obs.span.*` (duration, llm.*.tokens,
    llm.total.cost) — scoped `ml_app:grug-elder`, split by the standard
    `model_provider` tag (the poolside-vs-openrouter backend distinction;
    verified against live span tags — there is NO `model` tag). Duration is
    nanoseconds + cost is nanodollars, scaled in-formula to ms / USD.
  - The `code_reviewer_dispatched` structured log (service:grug-webhook)
    whose `extra={...}` fields surface as `@backend`, `@findings_count`,
    `@result`, `@dropped_hallucinations`, `@degraded_reason`.

KNOWN LIMITATION (documented, not hidden): the LLM-as-judge `is_real_bug`
and human `human_verdict` evaluations are LLM Obs EVALUATIONS, which are
not exposed as dashboard-queryable metrics — they live in the LLM Obs
evaluations explorer. The judge-accuracy / false-positive / annotation-
backlog surfaces are therefore a deep-link note widget, not a metric graph,
until an eval→metric export exists. Widgets populate once Elder LLM traffic
flows (the OpenRouter/Poolside keys were only loaded recently).
"""

from __future__ import annotations

import json

import pulumi
import pulumi_datadog as datadog

# Scope every LLM Obs query to the Elder ML app (DD_LLMOBS_ML_APP on the
# webhook Lambda). The standard `model_provider` tag distinguishes backends
# (it's `backend.value` — poolside vs openrouter); there is no `model` tag.
_ML_APP = "grug-elder"
_WEBHOOK_SERVICE = "grug-webhook"


def _metric_q(name: str, env: str, *, agg: str = "avg",
              by: str = "model_provider") -> str:
    # `model_provider` IS the backend distinction (poolside vs openrouter) —
    # verified against live `ml_obs.span.duration{ml_app:grug-elder}` tags
    # (there is no `model` tag; LLM Obs to-metrics emits `model_name` +
    # `model_provider`). env-scoped so the dev stack doesn't graph prod data.
    return f"{agg}:{name}{{ml_app:{_ML_APP},env:{env}}} by {{{by}}}"


def _dispatch_log_query(env: str, extra: str = "") -> str:
    base = f'service:{_WEBHOOK_SERVICE} env:{env} "code_reviewer_dispatched"'
    return f"{base} {extra}".strip()


def _ts_metric(title: str, formulas: list[dict], queries: list[dict]) -> dict:
    return {
        "definition": {
            "type": "timeseries",
            "title": title,
            "show_legend": True,
            "requests": [{
                "response_format": "timeseries",
                "queries": queries,
                "formulas": formulas,
                "style": {"palette": "dog_classic"},
                "display_type": "line",
            }],
        }
    }


def create_elder_health(
    *, env: str, provider: datadog.Provider,
) -> datadog.DashboardJson:
    """Build + return the Elder code-review health dashboard. Returns the
    DashboardJson so the composition root can export its URL/id."""

    llmobs_explorer = (
        "https://app.datadoghq.com/llm/evaluations?query=%40ml_app%3A"
        f"{_ML_APP}"
    )

    widgets: list[dict] = [
        # Header note — orients the operator + records the data-source caveat.
        {"definition": {
            "type": "note",
            "content": (
                "# Grug Elder — Code Review Health\n"
                "Operational health of the code-reviewer (Elder) persona. "
                "Span metrics scoped to `ml_app:grug-elder`, split by "
                "`model_provider` (poolside vs openrouter). Volume/outcome "
                "widgets from the "
                "`code_reviewer_dispatched` webhook log.\n\n"
                "**Judge accuracy / false-positive rate / annotation backlog** "
                "use LLM Obs *evaluations* (`is_real_bug`, `human_verdict`), "
                "which are not dashboard metrics — see the deep-link widget."
            ),
            "background_color": "gray",
            "font_size": "14",
            "text_align": "left",
        }},
        # Reviews over time, split by backend (volume) — from the dispatch log.
        {"definition": {
            "type": "timeseries",
            "title": "Reviews over time (by backend)",
            "show_legend": True,
            "requests": [{
                "response_format": "timeseries",
                "queries": [{
                    "data_source": "logs",
                    "name": "reviews",
                    "search": {"query": _dispatch_log_query(env)},
                    "indexes": ["*"],
                    "group_by": [{"facet": "@backend", "limit": 5,
                                  "sort": {"order": "desc",
                                           "aggregation": "count"}}],
                    "compute": {"aggregation": "count"},
                }],
                "formulas": [{"formula": "reviews"}],
                "display_type": "bars",
            }],
        }},
        # Findings per review (distribution) — anti-noise health signal.
        {"definition": {
            "type": "timeseries",
            "title": "Findings per review — p50 / p95",
            "show_legend": True,
            "requests": [{
                "response_format": "timeseries",
                "queries": [
                    # Events Platform (logs) percentile aggregators are the
                    # fixed enum median/pc75/pc90/pc95/pc98/pc99 — NOT a
                    # numeric `percentile` field (that's a Metrics-only shape).
                    {"data_source": "logs", "name": "p50",
                     "search": {"query": _dispatch_log_query(env)},
                     "indexes": ["*"],
                     "compute": {"aggregation": "median",
                                 "metric": "@findings_count"}},
                    {"data_source": "logs", "name": "p95",
                     "search": {"query": _dispatch_log_query(env)},
                     "indexes": ["*"],
                     "compute": {"aggregation": "pc95",
                                 "metric": "@findings_count"}},
                ],
                "formulas": [{"formula": "p50"}, {"formula": "p95"}],
                "display_type": "line",
            }],
        }},
        # Prompt latency p50/p95 by backend — LLM Obs span metric. Duration is
        # emitted in NANOSECONDS; /1e6 → ms to match the title.
        _ts_metric(
            "Prompt latency p50 / p95 by backend (ms)",
            formulas=[{"formula": "p50 / 1000000"}, {"formula": "p95 / 1000000"}],
            queries=[
                {"data_source": "metrics", "name": "p50",
                 "query": _metric_q("ml_obs.span.duration", env, agg="p50")},
                {"data_source": "metrics", "name": "p95",
                 "query": _metric_q("ml_obs.span.duration", env, agg="p95")},
            ],
        ),
        # Token usage by backend — input + output (unitless counts).
        _ts_metric(
            "Token usage by backend (input + output)",
            formulas=[{"formula": "in"}, {"formula": "out"}],
            queries=[
                {"data_source": "metrics", "name": "in",
                 "query": _metric_q("ml_obs.span.llm.input.tokens", env, agg="sum")},
                {"data_source": "metrics", "name": "out",
                 "query": _metric_q("ml_obs.span.llm.output.tokens", env, agg="sum")},
            ],
        ),
        # LLM cost by backend — operational $ visibility. Cost is emitted in
        # NANODOLLARS; /1e9 → USD to match the title.
        _ts_metric(
            "LLM cost by backend (USD)",
            formulas=[{"formula": "cost / 1000000000"}],
            queries=[
                {"data_source": "metrics", "name": "cost",
                 "query": _metric_q("ml_obs.span.llm.total.cost", env, agg="sum")},
            ],
        ),
        # Review outcomes — pass / fail / neutral / publish_failed / degraded.
        {"definition": {
            "type": "toplist",
            "title": "Review outcomes (by result)",
            "requests": [{
                "response_format": "scalar",
                "queries": [{
                    "data_source": "logs",
                    "name": "outcomes",
                    "search": {"query": _dispatch_log_query(env)},
                    "indexes": ["*"],
                    "group_by": [{"facet": "@result", "limit": 10,
                                  "sort": {"order": "desc",
                                           "aggregation": "count"}}],
                    "compute": {"aggregation": "count"},
                }],
                "formulas": [{"formula": "outcomes"}],
            }],
        }},
        # Dropped hallucinations over time — false-positive PROXY (findings the
        # evaluate_diff filter dropped as outside-diff). Real FP-rate needs the
        # judge/human evaluations below.
        {"definition": {
            "type": "timeseries",
            "title": "Dropped hallucinations (sum)",
            "show_legend": False,
            "requests": [{
                "response_format": "timeseries",
                "queries": [{
                    "data_source": "logs",
                    "name": "dropped",
                    "search": {"query": _dispatch_log_query(env)},
                    "indexes": ["*"],
                    "compute": {"aggregation": "sum",
                                "metric": "@dropped_hallucinations"},
                }],
                "formulas": [{"formula": "dropped"}],
                "display_type": "bars",
            }],
        }},
        # Judge accuracy + human-verdict + annotation backlog — LLM Obs
        # evaluations are not dashboard metrics; deep-link to the explorer.
        {"definition": {
            "type": "note",
            "content": (
                "### Judge accuracy · false-positive rate · annotation backlog\n"
                "`is_real_bug` (LLM-as-judge) and `human_verdict` (developer "
                "👍/👎) are LLM Obs **evaluations**, queryable in the evaluations "
                f"explorer — [open for ml_app:{_ML_APP}]({llmobs_explorer}). "
                "Pending labels = the annotation backlog. A future eval→metric "
                "export would let these render as native widgets here."
            ),
            "background_color": "yellow",
            "font_size": "14",
            "text_align": "left",
        }},
    ]

    dashboard_spec = {
        "title": "Grug Elder — Code Review Health",
        "description": (
            "Operational health of the code-reviewer (Elder) persona "
            "(spec 0015/0016/0017). Managed by Pulumi — edits here are "
            "overwritten on the next `pulumi up`."
        ),
        "layout_type": "ordered",
        "reflow_type": "auto",
        "tags": [f"env:{env}", "team:grug", "service:grug-webhook"],
        "widgets": widgets,
    }

    return datadog.DashboardJson(
        "grug-elder-health",
        dashboard=json.dumps(dashboard_spec),
        opts=pulumi.ResourceOptions(provider=provider),
    )
