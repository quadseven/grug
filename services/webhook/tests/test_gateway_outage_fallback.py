"""grug#1467: Elder concludes when the local inference gateway is down.

A review of a gateway fix runs through that same gateway, so the merge gate
shares its failure domain. Each test drives `dispatch_code_review` end to end
with only the model transport faked: the check Elder posts is the assertion.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import httpx
import llm_client as lc
import pytest
from personas.code_reviewer import dispatch as cr_dispatch

_GATEWAY = "http://cave.test"
_POOLSIDE = "inference.poolside.ai"
_OPENROUTER = "openrouter.ai"
_BUDGET_S = 700.0
_OUTAGE = "inference gateway outage"

_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
 tail
"""

_PAYLOAD = {
    "action": "opened",
    "installation": {"id": 11},
    "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
    "pull_request": {
        "number": 7,
        "head": {"sha": "abcd1234efgh"},
        "base": {"sha": "base5678ijkl"},
        "title": "Fix the queue wait",
        "body": "Bounds the wait.",
        "user": {"login": "alice"},
    },
}


def _queue_full() -> httpx.Response:
    return httpx.Response(503, json={"error": {"code": "spark_gateway_queue_full"}})


def _queue_wait_exceeded() -> httpx.Response:
    return httpx.Response(504, json={"error": {"code": "spark_gateway_queue_wait_exceeded"}})


GATEWAY_FAILURES = {
    "503_queue_full": _queue_full,
    "504_queue_wait_exceeded": _queue_wait_exceeded,
    "connection_refused": lambda: httpx.ConnectError("connection refused"),
    "read_timeout": lambda: httpx.ReadTimeout("no bytes"),
}


def _clean_review() -> httpx.Response:
    content = json.dumps({"findings": []})
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "model": "served-model",
    })


class _Clock:
    """Monotonic time that moves only when a model call spends it."""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self.now = self.start

    def __call__(self) -> float:
        return self.now

    @property
    def elapsed(self) -> float:
        return self.now - self.start


class _Models:
    """The model transport: every call is recorded, then answered by `route`."""

    def __init__(self, clock: _Clock, route) -> None:
        self.clock = clock
        self.route = route
        self.urls: list[str] = []

    def __call__(self, url: str, **kwargs) -> httpx.Response:
        self.urls.append(url)
        answer = self.route(url, kwargs)
        if isinstance(answer, httpx.TimeoutException):
            self.clock.now += float(kwargs["timeout"])
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def calls_to(self, host: str) -> int:
        return sum(host in url for url in self.urls)


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(lc.time, "monotonic", fake)
    return fake


@pytest.fixture(autouse=True)
def _elder(monkeypatch):
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "")
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda _s: None)
    monkeypatch.setattr(
        lc, "acquire_free_tier_slot",
        lambda *a, **k: type("Admit", (), {"admitted": True})(),
    )
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", _GATEWAY)
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cave")
    monkeypatch.setenv("GRUG_STAGED_REVIEW_BUDGET_S", str(_BUDGET_S))
    monkeypatch.setenv("GRUG_REVIEW_LLM_TIMEOUT_S", "330")
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "0")
    monkeypatch.delenv("GRUG_CLOUD_FREE_POOL", raising=False)
    monkeypatch.delenv("GRUG_SUBSCRIPTION_FALLBACK", raising=False)
    monkeypatch.delenv("GRUG_SUBSCRIPTION_URL", raising=False)
    monkeypatch.delenv("GRUG_DISABLED_BACKENDS", raising=False)
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda _iid, fn: fn("tok"))
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 2})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())


def _post_review(monkeypatch, models: _Models):
    """Run Elder once and return the one check it posted."""
    posted: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda _token, _owner, _repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    diff = MagicMock(spec=httpx.Response, status_code=200, text=_DIFF)
    with (
        patch("httpx.get", return_value=diff),
        patch.object(httpx, "post", side_effect=models),
    ):
        cr_dispatch.dispatch_code_review(_PAYLOAD, blocking=True)
    assert len(posted) == 1
    return posted[0]


@pytest.mark.parametrize("failure", sorted(GATEWAY_FAILURES))
def test_gateway_outage_completes_the_review_on_a_non_gateway_backend(
    monkeypatch, clock, failure,
) -> None:
    make_failure = GATEWAY_FAILURES[failure]

    def route(url: str, _kwargs: dict):
        return make_failure() if url.startswith(_GATEWAY) else _clean_review()

    models = _Models(clock, route)
    check = _post_review(monkeypatch, models)

    assert models.calls_to(_POOLSIDE) == 1
    assert check.conclusion == "success"
    assert check.title == "Elder clear - no markings"


@pytest.mark.parametrize("failure", sorted(GATEWAY_FAILURES))
def test_gateway_outage_with_no_other_backend_concludes_naming_it(
    monkeypatch, clock, failure,
) -> None:
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "poolside,openrouter,opencode-go")
    make_failure = GATEWAY_FAILURES[failure]
    models = _Models(clock, lambda _url, _kwargs: make_failure())

    check = _post_review(monkeypatch, models)

    assert check.conclusion == "failure"
    assert _OUTAGE in check.title
    assert _OUTAGE in check.summary
    assert models.urls
    assert all(url.startswith(_GATEWAY) for url in models.urls)
    assert clock.elapsed <= _BUDGET_S


@pytest.mark.parametrize("answering_arm", [0, 1], ids=["coder", "reasoner"])
def test_one_gateway_arm_answering_makes_no_fallback_call(
    monkeypatch, clock, answering_arm,
) -> None:
    arm_calls: list[str] = []

    def route(url: str, kwargs: dict):
        model = str(kwargs["json"]["model"])
        if model not in arm_calls:
            arm_calls.append(model)
        return _clean_review() if arm_calls.index(model) == answering_arm else _queue_full()

    models = _Models(clock, route)
    check = _post_review(monkeypatch, models)

    assert check.conclusion == "success"
    assert all(url.startswith(_GATEWAY) for url in models.urls)
    assert models.calls_to(_POOLSIDE) == models.calls_to(_OPENROUTER) == 0


def test_failures_a_down_gateway_does_not_produce_are_not_outages() -> None:
    def outcome(error_kind: str, error_text: str, status: int | None) -> lc.AttemptOutcome:
        return lc.AttemptOutcome(
            backend=lc.Backend.CAVE, model=None, findings=(), content="",
            error_kind=error_kind, error_text=error_text, status_code=status,
            finish_reason="", usage_metrics={}, span_context=None, raw_response=None,
        )

    not_outages = [
        outcome("http_error", "http_500", 500),
        outcome("transport", "cave: RequestError", None),
        outcome("transport", "cave: ReviewDeadlineReached", None),
        outcome("transport", "cave: BackendDisabledError", None),
    ]
    for failed in not_outages:
        reason = lc._ladder_failure_reason(failed)
        assert reason is not None
        assert not lc._is_outage_failure(failed, reason)
    assert not lc._routes_through_gateway(f"https://{_POOLSIDE}/v1/chat/completions")


# The gateway's free provider pool is a gateway arm too (`/free` and
# `/free-public` routes). Production runs it on, ahead of Poolside in the
# fallback tier, so an outage must not spend the deadline on it before the
# backend that does not route through the gateway gets its one retry.
def _free_route_calls(models: _Models) -> int:
    return sum(url.startswith(f"{_GATEWAY}/free") for url in models.urls)


@pytest.mark.parametrize("failure", sorted(GATEWAY_FAILURES))
def test_gateway_outage_retries_off_gateway_before_the_free_pool(
    monkeypatch, clock, failure,
) -> None:
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", "true")
    make_failure = GATEWAY_FAILURES[failure]

    def route(url: str, _kwargs: dict):
        return make_failure() if url.startswith(_GATEWAY) else _clean_review()

    models = _Models(clock, route)
    check = _post_review(monkeypatch, models)

    assert models.calls_to(_POOLSIDE) == 1
    assert _free_route_calls(models) == 0
    assert check.conclusion == "success"
    assert check.title == "Elder clear - no markings"


@pytest.mark.parametrize("failure", sorted(GATEWAY_FAILURES))
def test_gateway_outage_does_not_re_ask_a_gateway_arm_that_already_failed(
    monkeypatch, clock, failure,
) -> None:
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", "true")
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_DISABLED_BACKENDS", "poolside,openrouter,opencode-go")
    make_failure = GATEWAY_FAILURES[failure]
    models = _Models(clock, lambda _url, _kwargs: make_failure())

    check = _post_review(monkeypatch, models)

    assert _free_route_calls(models) == 1
    assert check.conclusion == "failure"
    assert _OUTAGE in check.title
    assert _OUTAGE in check.summary
    # A read timeout runs the walk into the deadline, which clamps the last
    # call; allow for the review starting a moment after the fake clock did.
    assert clock.elapsed <= _BUDGET_S + 1.0


@pytest.mark.parametrize("answering_arm", [0, 1], ids=["coder", "reasoner"])
def test_one_gateway_arm_answering_with_the_free_pool_on_makes_no_fallback_call(
    monkeypatch, clock, answering_arm,
) -> None:
    monkeypatch.setenv("GRUG_CLOUD_FREE_POOL", "true")
    arm_calls: list[str] = []

    def route(url: str, kwargs: dict):
        model = str(kwargs["json"]["model"])
        if model not in arm_calls:
            arm_calls.append(model)
        return _clean_review() if arm_calls.index(model) == answering_arm else _queue_full()

    models = _Models(clock, route)
    check = _post_review(monkeypatch, models)

    assert check.conclusion == "success"
    assert _free_route_calls(models) == 0
    assert models.calls_to(_POOLSIDE) == models.calls_to(_OPENROUTER) == 0
