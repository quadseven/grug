"""grug#1575: Elder is back on OpenCode Go, and Go cannot drain the plan.

Paid Go calls stop at a daily cap shared by every pod, a spent or capped Go
falls to the next tier without failing the review, a dead backend never passes
a review, and private code reaches only models with verified data terms. The
HTTP boundary and the cap's store are faked."""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import httpx
import llm_client as lc
import pytest
from adapters.pg_rate_limit_store import ReservationResult
from llm_client import Backend, Hunk, review_diff
from review_evidence import assemble_evidence

_RESERVE = "adapters.pg_rate_limit_store.try_reserve_slot"
_PAID = "deepseek-v4.1-flash"
_FREE_GO = "beam-free"
_MANIFESTS = ("consumer-deployment.yaml", "webhook-deployment.yaml", "poller-cronjob.yaml")
_GO_USAGE_LIMIT = httpx.Response(
    429,
    headers={"retry-after": "3600"},
    json={
        "error": {"type": "GoUsageLimitError", "message": "Go usage limit exceeded"},
        "metadata": {"limitName": "monthly"},
    },
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-ocg-key")
    monkeypatch.setattr(lc, "_opencode_go_blocked_until", 0.0, raising=False)
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_OPENCODE_GO_API_KEY_SSM", "/test/opencode-go")
    monkeypatch.setenv("GRUG_OPENCODE_GO_FALLBACK_MODEL", _FREE_GO)
    monkeypatch.delenv("GRUG_CLOUD_FREE_TIER_MODEL", raising=False)
    monkeypatch.delenv("GRUG_OPENCODE_GO_SECOND_MODEL", raising=False)
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "openrouter:paid")
    monkeypatch.setenv("GRUG_OPENCODE_GO_DAILY_CAP", "2")


class _Counter:
    """The shared store: admits up to `day_limit`, like the real counter."""

    def __init__(self, used: int = 0) -> None:
        self.used = used
        self.names: list[str] = []

    def __call__(self, *, name, minute_limit, day_limit, now=None) -> ReservationResult:
        self.names.append(name)
        if self.used >= day_limit:
            return ReservationResult(admitted=False, minute_count=0, day_count=self.used)
        self.used += 1
        return ReservationResult(admitted=True, minute_count=1, day_count=self.used)


class _Http:
    """Stands in for `httpx.post`: records model per call, answers per model."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.models: list[str] = []

    def __call__(self, url, **kwargs):
        model = (kwargs.get("json") or json.loads(kwargs.get("content") or "{}")).get("model", "")
        self.models.append(model)
        answer = self.answers.get(model, httpx.ConnectError(f"no fake for {model}"))
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _finding() -> httpx.Response:
    content = (
        '{"findings": [{"path": "src/x.py", "line": 1, "rule": "lost-error", '
        '"severity": "high", "message": "bug"}]}'
    )
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content, "role": "assistant"}}], "model": "m",
    })


def _review(http: _Http, reserve: object, pr_context: dict | None = None):
    with patch.object(httpx, "post", side_effect=http), patch(_RESERVE, side_effect=reserve):
        return review_diff(
            assemble_evidence(
                [Hunk(path="src/x.py", body="@@ -1 +1 @@\n-foo\n+bar")], pr_context=pr_context,
            ),
            installation_id=1,
        )


def test_go_serves_the_review_while_the_day_has_room() -> None:
    http, counter = _Http({_PAID: _finding()}), _Counter()
    out = _review(http, counter)
    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENCODE_GO
    assert http.models == [_PAID]
    assert counter.used == 1


def test_at_the_daily_cap_no_paid_call_is_made_and_the_free_tier_answers(caplog) -> None:
    http, counter = _Http({_PAID: _finding(), _FREE_GO: _finding()}), _Counter(used=2)
    with caplog.at_level("WARNING"):
        out = _review(http, counter)
    assert out.kind == "reviewed"
    assert http.models == [_FREE_GO]
    assert "opencode_go_daily_cap_reached" in [r.getMessage() for r in caplog.records]


def test_the_cap_holds_every_pod_off_until_the_next_utc_day() -> None:
    with patch.object(lc.shared_benches, "publish") as publish:
        _review(_Http({_FREE_GO: _finding()}), _Counter(used=2))
    publish.assert_called_once_with(
        "go", Backend.OPENCODE_GO.value, "", lc._next_utc_midnight(),
    )
    assert not lc._opencode_go_available()
    assert lc._opencode_go_available(now=lc._next_utc_midnight() + 1)
    assert [t.model for t in lc._cloud_chain_tiers()] == [_FREE_GO, lc._POOLSIDE_MODEL]


def test_a_free_go_model_never_touches_the_cap(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_OPENCODE_GO_MODEL", _FREE_GO)
    config = lc._opencode_go_extra_model_config(_FREE_GO, 5.0)
    counter = _Counter(used=2)
    with patch.object(httpx, "post", return_value=_finding()), patch(_RESERVE, side_effect=counter):
        lc._call_backend(config, [{"role": "user", "content": "hi"}])
    assert counter.names == []


def test_a_store_outage_refuses_paid_go_and_opens_no_hold() -> None:
    """No counter means no bound, so a paid call does not go out; the hold
    stays shut so a store blip does not cost Go the rest of the day."""
    http = _Http({_PAID: _finding(), _FREE_GO: _finding()})
    out = _review(http, RuntimeError("store down"))
    assert out.kind == "reviewed"
    assert http.models == [_FREE_GO]
    assert lc._opencode_go_available()


def test_an_unset_cap_leaves_a_self_hosted_deployment_uncapped(monkeypatch) -> None:
    monkeypatch.delenv("GRUG_OPENCODE_GO_DAILY_CAP")
    counter = _Counter()
    _review(_Http({_PAID: _finding()}), counter)
    assert counter.names == []


def test_a_garbled_cap_admits_nothing(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_OPENCODE_GO_DAILY_CAP", "lots")
    http = _Http({_PAID: _finding(), _FREE_GO: _finding()})
    _review(http, _Counter())
    assert http.models == [_FREE_GO]


def test_a_spent_allowance_falls_to_the_next_backend_with_no_failed_review() -> None:
    http = _Http({_PAID: _GO_USAGE_LIMIT, _FREE_GO: _finding()})
    out = _review(http, _Counter())
    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENCODE_GO
    assert len(out.findings) == 1
    assert http.models == [_PAID, _FREE_GO]
    assert not lc._opencode_go_available()


def test_every_backend_dead_does_not_pass_the_review() -> None:
    out = _review(_Http({_PAID: _GO_USAGE_LIMIT}), _Counter())
    assert out.kind == "all_failed"
    assert not out.findings


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 15_000, "completion_tokens": 7},
    {"prompt_tokens": 33_000, "completion_tokens": 1792},
    None,
], ids=["short", "all-reasoning", "no-usage"])
def test_a_free_go_empty_answer_is_never_a_clean_pass(usage) -> None:
    """Live 2026-11-01: Beam Free answered empty on a 33k-token diff that
    DeepSeek found faults in, in 1792 reasoning tokens."""
    body = {"choices": [{"message": {"content": '{"findings": []}', "role": "assistant"}}]}
    if usage is not None:
        body["usage"] = usage
    http = _Http({_FREE_GO: httpx.Response(200, json=body)})
    out = _review(http, _Counter(used=2))
    assert http.models[0] == _FREE_GO
    assert (out.error or "").startswith(lc._LOW_CONFIDENCE_PREFIX)


def test_a_free_go_empty_answer_cannot_agree_with_the_paid_model() -> None:
    def empty() -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"findings": []}', "role": "assistant"}}],
            "usage": {"prompt_tokens": 15_000, "completion_tokens": 7},
        })
    out = _review(_Http({_PAID: empty(), _FREE_GO: empty()}), _Counter())
    assert (out.error or "").startswith(lc._LOW_CONFIDENCE_PREFIX)


def test_beam_free_reasons_at_low_effort_or_it_never_answers() -> None:
    """Live 2026-11-01: at the default effort it spent all 8192 tokens on
    reasoning and returned nothing; `none` and `thinking` are 400s."""
    assert lc._opencode_go_extra_model_config(_FREE_GO, 5.0).extra_body == {
        "reasoning_effort": "low", "max_tokens": lc._CLOUD_CHAIN_MAX_TOKENS,
    }


def test_each_go_answer_logs_what_it_cost_the_allowance(caplog) -> None:
    body = _finding().json()
    body["usage"] = {"opencode": {"billing_source": "go", "allowance": 0.00115}}
    http = _Http({_PAID: httpx.Response(200, json=body)})
    with caplog.at_level("INFO"):
        _review(http, _Counter())
    usage = [r for r in caplog.records if r.getMessage() == "opencode_go_usage"]
    assert [(r.model, r.allowance, r.billing_source) for r in usage] == [
        (_PAID, 0.00115, "go"),
    ]


def _shipped(name: str) -> dict[str, str]:
    root = Path(__file__).resolve().parents[3] / "k8s"
    found: dict[str, str] = {}
    for manifest in _MANIFESTS:
        values = re.findall(
            r'name: ' + name + r', value: "([^"]*)"', (root / manifest).read_text(),
        )
        assert len(values) == 1, (manifest, values)
        found[manifest] = values[0]
    return found


def test_every_workload_ships_go_on_with_the_paid_openrouter_pool_off() -> None:
    assert set(_shipped("GRUG_DISABLED_BACKENDS").values()) == {"openrouter:paid"}


def test_every_workload_ships_the_same_daily_cap() -> None:
    caps = set(_shipped("GRUG_OPENCODE_GO_DAILY_CAP").values())
    assert len(caps) == 1
    assert int(caps.pop()) > 0


def test_every_shipped_go_model_has_verified_data_terms() -> None:
    shipped = {
        *_shipped("GRUG_OPENCODE_GO_FALLBACK_MODEL").values(),
        lc._OPENCODE_GO_DEFAULT_MODEL,
    }
    assert shipped <= lc._OPENCODE_GO_PRIVATE_SAFE_MODELS


_PRIVATE = {"repo": "o/r", "pr_number": 1, "repo_private": True}
_PUBLIC = {"repo": "o/r", "pr_number": 1, "repo_private": False}


@pytest.mark.parametrize("off_list", ["muse-spark-1.3-contributor", "iceman-contributor-free"])
@pytest.mark.parametrize("pr_context", [_PRIVATE, {"repo": "o/r", "pr_number": 1}, None],
                         ids=["private", "visibility-unknown", "no-context"])
def test_a_model_off_the_list_never_receives_a_private_diff(
    monkeypatch, off_list, pr_context,
) -> None:
    monkeypatch.setenv("GRUG_OPENCODE_GO_SECOND_MODEL", off_list)
    http = _Http({_PAID: httpx.ConnectError("down"), _FREE_GO: _finding()})
    _review(http, _Counter(), pr_context)
    assert off_list not in http.models
    assert http.models == [_PAID, _FREE_GO]


def test_a_model_off_the_list_may_read_a_public_diff(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_OPENCODE_GO_SECOND_MODEL", "kimi-k3")
    http = _Http({_PAID: httpx.ConnectError("down"), "kimi-k3": _finding()})
    _review(http, _Counter(), _PUBLIC)
    assert http.models == [_PAID, "kimi-k3"]
