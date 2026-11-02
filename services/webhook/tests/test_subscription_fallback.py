"""Tests for Elder's last resort subscription fallback (grug#1276, part of #1411).

Acceptance criteria verified:
1. Routing logic attempts self-hosted -> free provider pool -> subscription
   in that fixed order, with no reordering by load or latency.
2. Subscription fallback uses operator's own headless subscription lanes
   (Claude Code / Codex) and never pay-per-token API keys.
3. Daily call cap stops processing when reached and review ends degraded
   with reason subscription_limit_reached (never paid).
4. Attribution in review body and metrics indicates which tier served.
5. Subscription is never called while self-hosted or free provider tiers
   can still serve.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import observability
import llm_client as lc
from llm_client import (
    Backend,
    BackendConfig,
    Hunk,
    review_diff,
    route_elder_model_call,
)
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer import persona as cr_persona
from review_evidence import assemble_evidence

_CAVE_URL = "http://cave.test/v1/chat/completions"
_FREE_GATEWAY_URL = "http://cave.test/free/v1/chat/completions"
_POOLSIDE_URL = "https://inference.poolside.ai/v1/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_SUBSCRIPTION_URL = "http://cave.test/subscription/v1/chat/completions"


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    """Set up clean fallback environment."""
    lc._subscription_daily_calls.clear()
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    monkeypatch.setattr(lc, "_opencode_go_blocked_until", 0.0, raising=False)
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    monkeypatch.setattr(
        lc, "acquire_free_tier_slot",
        lambda *a, **k: type("Admit", (), {"admitted": True})(),
    )
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.setenv("GRUG_SUBSCRIPTION_FALLBACK", "true")
    monkeypatch.setenv("GRUG_SUBSCRIPTION_DAILY_CAP", "50")
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", "true")
    yield
    lc._subscription_daily_calls.clear()


def _finding_body(file_path: str = "src/x.py", line: int = 1) -> str:
    return json.dumps({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "findings": [{
                        "path": file_path,
                        "line": line,
                        "rule": "unhandled-error",
                        "severity": "high",
                        "message": "check error return",
                    }],
                }),
                "role": "assistant",
            },
        }],
        "model": "test-model",
    })


def _hunk(path: str = "src/x.py") -> Hunk:
    return Hunk(path=path, body="@@ -1 +1 @@\n-old\n+new")


class _Recorder:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.called_urls: list[str] = []
        self.call_headers: list[dict[str, str]] = []

    def __call__(self, url: str, **kwargs):
        self.called_urls.append(url)
        self.call_headers.append(kwargs.get("headers") or {})
        for pattern, resp in self.responses.items():
            if pattern in url:
                if isinstance(resp, BaseException):
                    raise resp
                if isinstance(resp, str):
                    return httpx.Response(200, text=resp)
                if isinstance(resp, httpx.Response):
                    return resp
                if callable(resp):
                    return resp(url, **kwargs)
        raise httpx.ConnectError(f"no recorded response for {url}")


def test_subscription_fallback_opt_in_behavior(monkeypatch) -> None:
    """Subscription fallback is enabled when GRUG_SUBSCRIPTION_FALLBACK is true or URL set."""
    monkeypatch.delenv("GRUG_SUBSCRIPTION_FALLBACK", raising=False)
    monkeypatch.delenv("GRUG_SUBSCRIPTION_URL", raising=False)
    assert not lc._subscription_fallback_enabled()
    assert lc._subscription_fallback_config() is None

    monkeypatch.setenv("GRUG_SUBSCRIPTION_FALLBACK", "true")
    assert lc._subscription_fallback_enabled()
    cfg = lc._subscription_fallback_config()
    assert cfg is not None
    assert cfg.backend == Backend.SUBSCRIPTION
    assert cfg.url == "http://cave.test/subscription/v1/chat/completions"
    assert cfg.model == "claude-code"


def test_subscription_never_called_while_self_hosted_can_serve() -> None:
    """AC: prove subscription is never called while self-hosted tier can serve."""
    recorder = _Recorder({
        _CAVE_URL: _finding_body(),
        "free": httpx.ConnectError("free pool should not be called"),
        "subscription": httpx.ConnectError("subscription should not be called"),
    })
    evidence = assemble_evidence([_hunk()])
    with patch.object(httpx, "post", side_effect=recorder):
        resp = review_diff(evidence, installation_id=1)

    assert resp.kind == "reviewed"
    assert resp.backend_used == Backend.CAVE
    assert resp.tier_served == "self-hosted"
    assert len(resp.findings) == 1
    assert _CAVE_URL in recorder.called_urls
    assert not any("subscription" in u for u in recorder.called_urls)
    assert not any("/free" in u for u in recorder.called_urls)


def test_subscription_never_called_while_free_provider_pool_can_serve() -> None:
    """AC: prove subscription is never called while free provider pool can serve."""
    recorder = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _FREE_GATEWAY_URL: _finding_body(),
        "subscription": httpx.ConnectError("subscription should not be called"),
    })
    evidence = assemble_evidence([_hunk()])
    with patch.object(httpx, "post", side_effect=recorder):
        resp = review_diff(evidence, installation_id=1)

    assert resp.kind == "reviewed"
    assert resp.backend_used == Backend.FREE_POOL
    assert resp.tier_served == "free-pool"
    assert len(resp.findings) == 1
    assert any("free" in u for u in recorder.called_urls)
    assert not any("subscription" in u for u in recorder.called_urls)


def test_subscription_never_called_while_free_saas_can_serve(monkeypatch) -> None:
    """AC: prove subscription is never called while free SaaS fallback can serve."""
    # Free pool behind gateway fails; Poolside succeeds.
    recorder = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _FREE_GATEWAY_URL: httpx.ConnectTimeout("free gateway down"),
        _POOLSIDE_URL: _finding_body(),
        "subscription": httpx.ConnectError("subscription should not be called"),
    })
    evidence = assemble_evidence([_hunk()])
    with patch.object(httpx, "post", side_effect=recorder):
        resp = review_diff(evidence, installation_id=1)

    assert resp.kind == "reviewed"
    assert resp.backend_used == Backend.POOLSIDE
    assert resp.tier_served == "free-pool"
    assert len(resp.findings) == 1
    assert not any("subscription" in u for u in recorder.called_urls)


def test_subscription_called_only_after_self_hosted_and_free_tiers_fail() -> None:
    """AC: Subscription is used as last resort when self-hosted and free tiers all fail."""
    recorder = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _FREE_GATEWAY_URL: httpx.ConnectTimeout("gateway free down"),
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OPENROUTER_URL: httpx.ConnectTimeout("openrouter down"),
        _SUBSCRIPTION_URL: _finding_body(),
    })
    evidence = assemble_evidence([_hunk()])
    with patch.object(httpx, "post", side_effect=recorder):
        resp = review_diff(evidence, installation_id=1)

    assert resp.kind == "reviewed"
    assert resp.backend_used == Backend.SUBSCRIPTION
    assert resp.tier_served == "subscription"
    assert len(resp.findings) == 1

    # Verify attempt order in called URLs
    cave_idx = next(i for i, u in enumerate(recorder.called_urls) if "cave.test/v1" in u)
    free_idx = next(i for i, u in enumerate(recorder.called_urls) if "/free" in u)
    sub_idx = next(i for i, u in enumerate(recorder.called_urls) if "subscription" in u)
    assert cave_idx < free_idx < sub_idx


def test_order_is_fixed_never_reordered_by_load_or_latency() -> None:
    """AC: routing logic attempts self-hosted -> free provider pool -> subscription
    in that fixed order, with no reordering by load or latency."""
    call_sequence: list[str] = []

    def mock_post(url, **kwargs):
        if "subscription" in url:
            call_sequence.append("subscription")
            return httpx.Response(200, text=_finding_body())
        if "/free" in url or "poolside" in url or "openrouter" in url:
            call_sequence.append("free-pool")
            raise httpx.ReadTimeout("free provider slow/down")
        if "cave.test/v1" in url:
            call_sequence.append("self-hosted")
            raise httpx.ConnectTimeout("cave overloaded")
        raise httpx.ConnectError("unexpected url")

    evidence = assemble_evidence([_hunk()])
    with patch.object(httpx, "post", side_effect=mock_post):
        resp = route_elder_model_call(evidence, installation_id=1)

    assert resp.kind == "reviewed"
    assert resp.tier_served == "subscription"
    # Order must strictly start with self-hosted, then free-pool attempts, then subscription
    assert call_sequence[0] == "self-hosted"
    assert "free-pool" in call_sequence
    assert call_sequence[-1] == "subscription"


def test_subscription_lane_headers_and_no_pay_per_token_keys(monkeypatch) -> None:
    """AC: Subscription fallback uses operator's headless lanes and never pay-per-token API keys."""
    # Ensure even if ANTHROPIC_API_KEY is in env, it is not used
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-dummy-secret-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy-openai-key")

    recorder = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _FREE_GATEWAY_URL: httpx.ConnectTimeout("free gateway down"),
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OPENROUTER_URL: httpx.ConnectTimeout("openrouter down"),
        _SUBSCRIPTION_URL: _finding_body(),
    })
    evidence = assemble_evidence([_hunk()])
    with patch.object(httpx, "post", side_effect=recorder):
        resp = review_diff(evidence, installation_id=1)

    assert resp.kind == "reviewed"
    assert resp.backend_used == Backend.SUBSCRIPTION
    sub_headers = next(
        h for u, h in zip(recorder.called_urls, recorder.call_headers, strict=True)
        if "subscription" in u
    )
    assert sub_headers.get("X-Loom-Lane-Type") == "subscription-headless"
    assert sub_headers.get("X-Spark-Priority") == "interactive"
    assert sub_headers.get("X-Spark-Caller") == "grug-elder-subscription"
    # Key loader gave "in-cluster", not the pay-per-token API keys
    assert sub_headers.get("Authorization") == "Bearer in-cluster"


def test_subscription_refuses_pay_per_token_api_key(monkeypatch) -> None:
    """AC: If a pay-per-token key is configured on subscription, it is rejected."""
    bad_config = BackendConfig(
        backend=Backend.SUBSCRIPTION,
        url="http://cave.test/subscription/v1/chat/completions",
        model="claude-code",
        key_loader=lambda: "sk-ant-api03-prohibited-pay-per-token-key",
        timeout_seconds=10.0,
        retry_attempts=1,
        transport_retry_attempts=1,
    )
    with pytest.raises(Exception) as exc_info:
        lc._call_backend(bad_config, [{"role": "user", "content": "hi"}])
    assert "pay-per-token" in str(exc_info.value).lower()


def test_subscription_daily_call_cap_stops_processing(monkeypatch) -> None:
    """AC: configured daily call cap stops processing and review ends degraded with that reason."""
    monkeypatch.setenv("GRUG_SUBSCRIPTION_DAILY_CAP", "2")
    lc._subscription_daily_calls.clear()

    recorder = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _FREE_GATEWAY_URL: httpx.ConnectTimeout("free gateway down"),
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OPENROUTER_URL: httpx.ConnectTimeout("openrouter down"),
        _SUBSCRIPTION_URL: _finding_body(),
    })
    evidence = assemble_evidence([_hunk()])

    # First call succeeds and increments count to 1
    with patch.object(httpx, "post", side_effect=recorder):
        resp1 = review_diff(evidence, installation_id=1)
    assert resp1.kind == "reviewed"
    assert lc._subscription_calls_today() == 1

    # Second call succeeds and increments count to 2 (cap reached)
    with patch.object(httpx, "post", side_effect=recorder):
        resp2 = review_diff(evidence, installation_id=1)
    assert resp2.kind == "reviewed"
    assert lc._subscription_calls_today() == 2

    # Third call hits daily cap: stops processing, ends degraded
    with patch.object(httpx, "post", side_effect=recorder):
        resp3 = review_diff(evidence, installation_id=1)

    assert resp3.kind == "subscription_limit_reached"
    assert resp3.error == "subscription limit reached"
    assert resp3.tier_served == "subscription"

    # Evaluates to degraded reason subscription_limit_reached with neutral conclusion (never paid)
    eval_result = cr_persona.evaluate_diff(tuple(), resp3)
    assert eval_result.conclusion == "neutral"
    assert eval_result.degraded_reason == "subscription_limit_reached"
    assert eval_result.tier_served == "subscription"


def test_subscription_quota_429_ends_degraded() -> None:
    """When subscription returns 429 quota exhaustion, review ends degraded with subscription_limit_reached."""
    recorder = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _FREE_GATEWAY_URL: httpx.ConnectTimeout("free gateway down"),
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OPENROUTER_URL: httpx.ConnectTimeout("openrouter down"),
        _SUBSCRIPTION_URL: httpx.Response(429, json={"error": {"message": "daily quota exceeded"}}),
    })
    evidence = assemble_evidence([_hunk()])
    with patch.object(httpx, "post", side_effect=recorder):
        resp = review_diff(evidence, installation_id=1)

    assert resp.kind == "subscription_limit_reached"
    assert resp.tier_served == "subscription"
    eval_result = cr_persona.evaluate_diff(tuple(), resp)
    assert eval_result.degraded_reason == "subscription_limit_reached"


def test_attribution_in_review_body() -> None:
    """AC: Log each last-resort call in review body (summary and timeline stack)."""
    # 1. Clean evaluation with tier_served = "subscription"
    clean_eval = cr_persona.CodeReviewEvaluation(
        findings=(),
        conclusion="success",
        tier_served="subscription",
    )
    title, summary = cr_dispatch._summary_markdown(clean_eval)
    assert "Tier served: `subscription`." in summary

    detail = cr_dispatch._stack_detail_lines(
        review_phase="tier1", living_range="", suppressed_count=0,
        tier_served="subscription",
    )
    assert "- Tier served: `subscription`" in detail

    # 2. Free-pool tier evaluation
    free_eval = cr_persona.CodeReviewEvaluation(
        findings=(),
        conclusion="success",
        tier_served="free-pool",
    )
    _, free_summary = cr_dispatch._summary_markdown(free_eval)
    assert "Tier served: `free-pool`." in free_summary

    free_detail = cr_dispatch._stack_detail_lines(
        review_phase="tier1", living_range="", suppressed_count=0,
        tier_served="free-pool",
    )
    assert "- Tier served: `free-pool`" in free_detail

    # 3. Degraded evaluation when subscription cap reached
    degraded_eval = cr_persona.CodeReviewEvaluation(
        findings=(),
        conclusion="neutral",
        degraded_reason="subscription_limit_reached",
        tier_served="subscription",
    )
    deg_title, deg_summary = cr_dispatch._summary_markdown(degraded_eval)
    assert "subscription_limit_reached" in deg_title
    assert "Tier served: `subscription`." in deg_summary


def test_attribution_in_metrics(monkeypatch) -> None:
    """AC: Metrics emitted indicating which tier served and last resort calls."""
    emitted_counts: list[tuple[str, float, dict]] = []
    monkeypatch.setattr(
        observability, "emit_count",
        lambda metric, value=1, tags=None: emitted_counts.append((metric, value, tags or {})),
    )

    recorder = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _FREE_GATEWAY_URL: httpx.ConnectTimeout("free gateway down"),
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OPENROUTER_URL: httpx.ConnectTimeout("openrouter down"),
        _SUBSCRIPTION_URL: _finding_body(),
    })
    evidence = assemble_evidence([_hunk()])
    with patch.object(httpx, "post", side_effect=recorder):
        resp = review_diff(evidence, installation_id=1)

    assert resp.kind == "reviewed"
    assert resp.tier_served == "subscription"

    # Check metrics
    tier_metrics = [c for c in emitted_counts if c[0] == "grug.elder.tier_served"]
    assert any(tags.get("tier") == "subscription" for _, _, tags in tier_metrics)

    last_resort_metrics = [c for c in emitted_counts if c[0] == "grug.elder.last_resort_call"]
    assert any(tags.get("tier") == "subscription" for _, _, tags in last_resort_metrics)


def test_the_subscription_timeout_leaves_room_for_a_queued_call(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_SUBSCRIPTION_URL", _SUBSCRIPTION_URL)
    monkeypatch.delenv("GRUG_SUBSCRIPTION_TIMEOUT_S", raising=False)
    assert lc._subscription_fallback_config().timeout_seconds == 120.0


@pytest.mark.parametrize(("raw", "expected"), [
    ("90", 90.0), ("", 120.0), ("soon", 120.0), ("0", 120.0), ("-5", 120.0), ("nan", 120.0), ("inf", 120.0),
])
def test_the_subscription_timeout_env_is_read_and_a_bad_value_is_ignored(
    monkeypatch, raw: str, expected: float,
) -> None:
    monkeypatch.setenv("GRUG_SUBSCRIPTION_URL", _SUBSCRIPTION_URL)
    monkeypatch.setenv("GRUG_SUBSCRIPTION_TIMEOUT_S", raw)
    assert lc._subscription_fallback_config().timeout_seconds == expected


def test_the_lane_bearer_comes_from_ssm_when_a_path_is_set(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_SUBSCRIPTION_URL", _SUBSCRIPTION_URL)
    monkeypatch.setattr(lc, "get_subscription_lane_token", lambda: "lane-token")
    cfg = lc._subscription_fallback_config()
    assert lc._merged_headers(cfg, cfg.key_loader())["Authorization"] == "Bearer lane-token"


def test_the_author_family_reaches_the_lane_only_inside_a_gated_walk(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_SUBSCRIPTION_URL", _SUBSCRIPTION_URL)
    cfg = lc._subscription_fallback_config()
    assert "X-Review-Author-Family" not in lc._merged_headers(cfg, "k")
    token = lc._FAMILY_GATE.set(("gpt", False))
    try:
        assert lc._merged_headers(cfg, "k")["X-Review-Author-Family"] == "gpt"
    finally:
        lc._FAMILY_GATE.reset(token)


def test_a_lane_named_for_no_family_is_not_gated_off_a_reviewed_family(monkeypatch) -> None:
    token = lc._FAMILY_GATE.set(("gpt", False))
    try:
        assert not lc._family_gated("subscription-lane")
    finally:
        lc._FAMILY_GATE.reset(token)


def test_the_egress_policy_opens_the_lane_the_manifests_name() -> None:
    """A manifest URL means nothing if the namespace's egress fence drops it:
    the first rollout timed out on every call this way (#1546)."""
    from pathlib import Path
    from urllib.parse import urlsplit

    k8s = Path(__file__).resolve().parents[3] / "k8s"
    policy = (k8s / "networkpolicy.yaml").read_text()
    consumer = (k8s / "consumer-deployment.yaml").read_text()
    url_line = next(line for line in consumer.splitlines() if "GRUG_SUBSCRIPTION_URL" in line)
    url = url_line.split("value:")[1].strip(" }")
    assert urlsplit(url).hostname == "forward.subscription-lane.svc"
    assert "kubernetes.io/metadata.name: subscription-lane" in policy
    assert "matchLabels: {app: forward}" in policy


def _subscription_reply(served_model: str | None) -> str:
    body = json.loads(_finding_body())
    if served_model is None:
        del body["model"]
    else:
        body["model"] = served_model
    return json.dumps(body)


def _review_through_subscription(reply: str) -> lc.LlmReviewResponse:
    recorder = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _FREE_GATEWAY_URL: httpx.ConnectTimeout("free gateway down"),
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OPENROUTER_URL: httpx.ConnectTimeout("openrouter down"),
        _SUBSCRIPTION_URL: reply,
    })
    with patch.object(httpx, "post", side_effect=recorder):
        return review_diff(assemble_evidence([_hunk()]), installation_id=1)


@pytest.mark.parametrize("served", ["gpt-6-luna", "claude-haiku-5-5"])
def test_models_used_is_the_model_the_lane_replied_with(monkeypatch, served) -> None:
    """The lane picks its model per call, so the request names neither."""
    monkeypatch.setenv("GRUG_SUBSCRIPTION_MODEL", "subscription-lane")
    resp = _review_through_subscription(_subscription_reply(served))

    assert resp.models_used == (served,)
    evaluation = cr_persona.evaluate_diff((), resp)
    assert evaluation.models_used == (served,)
    _, summary = cr_dispatch._summary_markdown(evaluation)
    assert f"Tier served: `subscription` (`{served}`)." in summary


def test_a_reply_naming_no_model_never_reports_the_requested_one(monkeypatch) -> None:
    """A wrong name is worse than none: the configured name is a request."""
    monkeypatch.setenv("GRUG_SUBSCRIPTION_MODEL", "subscription-lane")
    resp = _review_through_subscription(_subscription_reply(None))

    assert resp.kind == "reviewed"
    assert resp.model_name == "subscription-lane"
    assert resp.models_used == ()
    _, summary = cr_dispatch._summary_markdown(
        cr_persona.evaluate_diff((), resp),
    )
    assert "Tier served: `subscription`." in summary
    assert "subscription-lane" not in summary


def test_a_cloud_arm_reply_naming_no_model_reports_none(monkeypatch) -> None:
    """The same rule on the free tier, which reaches models_used through an arm."""
    recorder = _Recorder({
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        _FREE_GATEWAY_URL: _subscription_reply(None),
    })
    with patch.object(httpx, "post", side_effect=recorder):
        resp = review_diff(assemble_evidence([_hunk()]), installation_id=1)

    assert resp.tier_served == "free-pool"
    assert resp.model_name
    assert resp.models_used == ()


def test_summary_and_detail_name_the_served_models() -> None:
    clean = cr_persona.CodeReviewEvaluation(
        findings=(), conclusion="success", tier_served="subscription",
        models_used=("gpt-6-luna", "claude-haiku-5-5"),
    )
    _, summary = cr_dispatch._summary_markdown(clean)
    assert "Tier served: `subscription` (`gpt-6-luna`, `claude-haiku-5-5`)." in summary

    detail = cr_dispatch._stack_detail_lines(
        review_phase="tier1", living_range="", suppressed_count=0,
        tier_served="subscription", models_used=("gpt-6-luna",),
    )
    assert "- Tier served: `subscription` (`gpt-6-luna`)" in detail


def test_a_backend_supplied_model_name_cannot_break_the_code_span() -> None:
    clean = cr_persona.CodeReviewEvaluation(
        findings=(), conclusion="success", tier_served="free-pool",
        models_used=("bad`name",),
    )
    _, summary = cr_dispatch._summary_markdown(clean)
    assert "`bad`name`" not in summary


def test_every_rebuild_keeps_the_models_with_their_tier() -> None:
    finding = cr_persona.CodeReviewEvaluation(
        findings=(), conclusion="success",
        tier_served="subscription", models_used=("gpt-6-luna",),
    )
    assert cr_persona.with_extra_findings(
        finding, (lc.Finding(path="a.py", line=1, severity="low", rule="r", message="m"),),
    ).models_used == ("gpt-6-luna",)
    assert cr_persona.with_findings(finding, ()).models_used == ("gpt-6-luna",)

    other = cr_persona.CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="partial_review",
        tier_served="free-pool", models_used=("other-model",),
    )
    assert cr_persona.with_degradation(finding, other).models_used == ("gpt-6-luna",)


def test_a_degradation_takes_the_models_only_with_their_tier() -> None:
    """The name must never sit beside another pass's tier."""
    other = cr_persona.CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="partial_review",
        tier_served="free-pool", models_used=("other-model",),
    )
    untiered = cr_persona.CodeReviewEvaluation(
        findings=(), conclusion="success", models_used=("cave-model",),
    )
    merged = cr_persona.with_degradation(untiered, other)
    assert merged.tier_served == "free-pool"
    assert merged.models_used == ("other-model",)


_GARBLED = json.dumps({
    "choices": [{"message": {"content": "no json here, only prose", "role": "assistant"}}],
    "model": "free-model",
})


def test_a_garbled_cave_answer_still_reaches_the_subscription_lane() -> None:
    """A Cave parse failure is a miss, not the review's end. It kept every
    public cohort off the lane while the Cave spilled to a free model that
    spent its output on reasoning (2026-11-01)."""
    recorder = _Recorder({
        _CAVE_URL: _GARBLED,
        _FREE_GATEWAY_URL: _GARBLED,
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OPENROUTER_URL: httpx.ConnectTimeout("openrouter down"),
        _SUBSCRIPTION_URL: _finding_body(),
    })
    with patch.object(httpx, "post", side_effect=recorder):
        resp = route_elder_model_call(assemble_evidence([_hunk()]), installation_id=1)

    assert resp.kind == "reviewed"
    assert resp.backend_used == Backend.SUBSCRIPTION
    assert resp.tier_served == "subscription"
    assert len(resp.findings) == 1


def test_every_tier_garbled_or_down_stays_a_parse_failure() -> None:
    """Fail-safe: with the lane down too, no tier reviewed, so the result
    stays the specific parse failure and can never pass the check."""
    recorder = _Recorder({
        _CAVE_URL: _GARBLED,
        _FREE_GATEWAY_URL: httpx.ConnectTimeout("gateway free down"),
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OPENROUTER_URL: httpx.ConnectTimeout("openrouter down"),
        _SUBSCRIPTION_URL: httpx.ConnectTimeout("lane down"),
    })
    with patch.object(httpx, "post", side_effect=recorder):
        resp = route_elder_model_call(assemble_evidence([_hunk()]), installation_id=1)

    assert resp.kind == "parse_failed"
    assert resp.backend_used == Backend.CAVE


def test_a_cancelled_review_after_a_garbled_cave_answer_asks_no_one_else() -> None:
    """A superseded review spends nothing on a snapshot about to be dropped."""
    cancel = threading.Event()

    def garble_and_cancel(url: str, **kwargs):
        cancel.set()
        return httpx.Response(200, text=_GARBLED)

    recorder = _Recorder({_CAVE_URL: garble_and_cancel})
    with patch.object(httpx, "post", side_effect=recorder):
        route_elder_model_call(
            assemble_evidence([_hunk()]), installation_id=1, cancel_event=cancel,
        )

    assert recorder.called_urls == [_CAVE_URL]


_EMPTY_SHORT = json.dumps({
    "choices": [{"message": {"content": '{"findings": []}', "role": "assistant"}}],
    "model": "free-model",
    "usage": {"prompt_tokens": 30_000, "completion_tokens": 6},
})
_EMPTY_LONG = json.dumps({
    "choices": [{"message": {"content": '{"findings": []}', "role": "assistant"}}],
    "model": "free-model",
    "usage": {"prompt_tokens": 30_000, "completion_tokens": 900},
})


def _walk_to_free_pool(free_answer: str, cave: object = _GARBLED, lane: object = None):
    recorder = _Recorder({
        _CAVE_URL: cave,
        _FREE_GATEWAY_URL: free_answer,
        _POOLSIDE_URL: httpx.ConnectTimeout("poolside down"),
        _OPENROUTER_URL: httpx.ConnectTimeout("openrouter down"),
        _SUBSCRIPTION_URL: lane or httpx.ConnectTimeout("lane down"),
    })
    with patch.object(httpx, "post", side_effect=recorder):
        return route_elder_model_call(assemble_evidence([_hunk()]), installation_id=1)


@pytest.mark.parametrize("empty", [_EMPTY_SHORT, _EMPTY_LONG])
def test_a_free_pool_empty_after_a_garbled_cave_is_not_a_clean_pass(empty) -> None:
    """The free pool's empty answer proves nothing, whatever its length, so
    it cannot pass a review the Cave garbled."""
    resp = _walk_to_free_pool(empty)

    assert resp.kind == "parse_failed"
    assert cr_persona.evaluate_diff((), resp).degraded_reason is not None


def test_a_free_pool_empty_with_the_cave_down_is_low_confidence() -> None:
    resp = _walk_to_free_pool(_EMPTY_LONG, cave=httpx.ConnectTimeout("cave down"))

    assert resp.kind == "reviewed"
    assert resp.error.startswith(lc._LOW_CONFIDENCE_PREFIX)
    assert cr_persona.evaluate_diff((), resp).degraded_reason == "low_confidence_review"


def test_a_free_pool_empty_walks_on_to_the_lane() -> None:
    resp = _walk_to_free_pool(_EMPTY_SHORT, lane=_finding_body())

    assert resp.backend_used == Backend.SUBSCRIPTION
    assert len(resp.findings) == 1


def test_a_free_pool_finding_is_still_a_review() -> None:
    resp = _walk_to_free_pool(_finding_body())

    assert resp.backend_used == Backend.FREE_POOL
    assert len(resp.findings) == 1
    assert resp.error == ""


def _cave_answer(model: str, completion_tokens: int) -> str:
    return json.dumps({
        "choices": [{"message": {"content": '{"findings": []}', "role": "assistant"}}],
        "model": model,
        "usage": {"prompt_tokens": 30_000, "completion_tokens": completion_tokens},
    })


@pytest.mark.parametrize(
    ("model", "tokens"),
    [
        ("nvidia/spilled-model:free", 900),
        ("spilled-model-free", 900),
        ("free-pool", 900),
        ("an-unlisted-model", 900),
        ("", 900),
        (lc._CAVE_REVIEW_CODER_DEFAULT_MODEL, 6),
    ],
)
def test_an_untrusted_cave_empty_walks_on_to_the_lane(model, tokens) -> None:
    """With the Sparks away the Cave route spills to free models, and a free
    model's empty answer proves nothing, whatever its length."""
    resp = _walk_to_free_pool(
        _EMPTY_LONG, cave=_cave_answer(model, tokens), lane=_finding_body(),
    )

    assert resp.backend_used == Backend.SUBSCRIPTION
    assert len(resp.findings) == 1


def test_an_untrusted_cave_empty_with_nothing_after_it_is_low_confidence() -> None:
    resp = _walk_to_free_pool(_EMPTY_LONG, cave=_cave_answer("nvidia/spilled-model:free", 900))

    assert resp.error.startswith(lc._LOW_CONFIDENCE_PREFIX)
    assert cr_persona.evaluate_diff((), resp).degraded_reason == "low_confidence_review"


def test_the_cave_trusts_an_empty_only_from_its_own_models() -> None:
    assert {lc._CAVE_REVIEW_CODER_DEFAULT_MODEL, lc._CAVE_REVIEW_REASONER_DEFAULT_MODEL} <= (
        lc._TRUSTED_EMPTY_CAVE_MODELS
    )
    assert not lc._cave_empty_is_trusted(
        lc._ArmOutcome(backend=Backend.CAVE, kind="success", model="gemma4:31b"),
    )


def test_a_cave_low_confidence_answer_names_its_own_tier() -> None:
    resp = _walk_to_free_pool(_EMPTY_LONG, cave=_cave_answer("nvidia/spilled-model:free", 900))

    assert resp.tier_served == "self-hosted"


def test_a_long_free_pool_empty_in_the_cloud_chain_is_not_a_clean_pass(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    recorder = _Recorder({
        _FREE_GATEWAY_URL: _EMPTY_LONG,
        _CAVE_URL: httpx.ConnectTimeout("cave down"),
        "subscription": httpx.ConnectTimeout("lane down"),
    })
    with patch.object(httpx, "post", side_effect=recorder):
        resp = lc._walk_review_ladder(assemble_evidence([_hunk()]), 1, "caveman", None)

    assert resp.error.startswith(lc._LOW_CONFIDENCE_PREFIX)
    assert cr_persona.evaluate_diff((), resp).degraded_reason == "low_confidence_review"


def test_a_listed_resident_empty_of_real_length_stays_a_clean_review() -> None:
    resp = _walk_to_free_pool(
        _EMPTY_SHORT, cave=_cave_answer(lc._CAVE_REVIEW_CODER_DEFAULT_MODEL, 900),
    )

    assert resp.backend_used == Backend.CAVE
    assert not resp.error


def test_a_token_capped_cave_empty_keeps_its_truncation_error() -> None:
    capped = json.dumps({
        "choices": [{
            "message": {"content": '{"findings": []}', "role": "assistant"},
            "finish_reason": "length",
        }],
        "model": "nvidia/spilled-model:free",
    })
    resp = _walk_to_free_pool(_EMPTY_LONG, cave=capped)

    assert resp.error.startswith("partial review:")
    assert resp.tier_served == "self-hosted"


def test_the_trusted_empty_list_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_CAVE_TRUSTED_EMPTY_MODELS", " resident-a , resident-b ")
    outcome = lc._ArmOutcome(backend=Backend.CAVE, kind="success", model="resident-b")
    assert lc._cave_empty_is_trusted(outcome)
    assert not lc._cave_empty_is_trusted(
        lc._ArmOutcome(backend=Backend.CAVE, kind="success", model=lc._CAVE_REVIEW_CODER_DEFAULT_MODEL),
    )


def test_the_workloads_ship_the_model_the_sparks_serve() -> None:
    """`spark:warm-any` hides the served name, so the code defaults never match
    a reply; an empty list would mark every honest Cave empty untrusted."""
    root = Path(__file__).resolve().parents[3] / "k8s"
    for manifest in ("consumer-deployment.yaml", "webhook-deployment.yaml"):
        assert (
            'name: GRUG_CAVE_TRUSTED_EMPTY_MODELS, value: "nemotron-3.5-super-vl"'
            in (root / manifest).read_text()
        )
