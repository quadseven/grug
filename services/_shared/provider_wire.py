"""Provider request and response shapes, retries, and cancellable HTTP calls."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Callable

import httpx

import model_availability as availability
from llm_backend import Backend, BackendConfig

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.llm_client")


def post(url: str, **kwargs: Any) -> httpx.Response:
    """Send one HTTP request. Tests can substitute the provider at this boundary."""
    return httpx.post(url, **kwargs)


# A call with less than this left is not sent: it could not finish a review.
_DEADLINE_CALL_FLOOR_SECONDS = 10.0


class ReviewDeadlineReached(httpx.RequestError):
    """A model call was cut, or never sent, because the staged review's
    deadline passed. A RequestError (not a TimeoutException) on purpose:
    every caller already treats it as a transport failure, and the slow bench
    only counts real timeouts - a deadline cut says nothing about the model."""


def _deadline_timeout(timeout_seconds: float, deadline: float | None) -> float:
    """`timeout_seconds` clamped to the time left before `deadline`.

    Raises `ReviewDeadlineReached` when less than the call floor is left, so a
    call that could not finish is never sent."""
    if deadline is None:
        return timeout_seconds
    left = deadline - time.monotonic()
    if left < _DEADLINE_CALL_FLOOR_SECONDS:
        raise ReviewDeadlineReached(
            f"staged review deadline reached ({max(left, 0.0):.1f}s left)"
        )
    return min(timeout_seconds, left)


# Exponential backoff applies on every attempt except the final one,
# which either returns the response or raises.
_RETRY_BASE_DELAY = 0.5

# 429 (rate limit) + 503 (CF edge blip / temporary backend overload)
# are routinely transient on both Poolside and OpenRouter. Other 5xx
# (500, 502, 504) return immediately; review_diff then falls back to
# the secondary backend rather than burning retries on what may be a
# permanent issue.
_RETRYABLE_STATUSES: frozenset[int] = frozenset((429, 503))


def _validate_subscription_key(key: str) -> None:
    """Ensure subscription fallback uses headless subscription lanes, never
    pay-per-token API keys (e.g. sk-ant-*, sk-*)."""
    if key.startswith("sk-") or "api-key" in key.lower():
        raise _BackendConfigError(
            "subscription backend must use headless subscription lanes, never pay-per-token API keys"
        )


class _BackendConfigError(Exception):
    """Backend is misconfigured (empty key, missing env var, SSM
    failure). Distinct from transport errors so the caller can fall
    back to the other backend without retry-burning the broken one."""


def _post_with_retries(
    url: str, body: dict[str, Any], headers: dict[str, str],
    timeout_seconds: float, retry_attempts: int, transport_attempts: int,
    deadline: float | None = None,
    *, sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """The actual 429/503-retrying HTTP call, shared by both dispatch paths below. Always goes through the bare `httpx.post()` convenience
    function (not an explicit `httpx.Client`) - tests mock `httpx.post`
    directly, and this keeps that working unmodified for every caller.

    `deadline` (grug#1216) is passed explicitly, not read from the context
    var, because the cancellable path runs this on a fresh thread that does
    not inherit it. Each attempt is clamped to the time left; a timeout that
    only fired because of the clamp is re-raised as `ReviewDeadlineReached`."""
    for attempt in range(retry_attempts):
        attempt_timeout = _deadline_timeout(timeout_seconds, deadline)
        try:
            resp = post(url, json=body, headers=headers, timeout=attempt_timeout)
        except httpx.RequestError as e:
            if isinstance(e, httpx.TimeoutException) and attempt_timeout < timeout_seconds:
                raise ReviewDeadlineReached(
                    f"staged review deadline cut the call after {attempt_timeout:.1f}s"
                ) from e
            if attempt < transport_attempts - 1:
                sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                continue
            raise
        if (
            resp.status_code in _RETRYABLE_STATUSES
            and attempt < retry_attempts - 1
            # A spent daily or monthly allowance does not clear in a backoff,
            # and a limit refusal opens a hold, so asking again only repeats it.
            and not availability._is_spent_quota_refusal(resp)
            and not availability._is_limit_refusal(resp)
        ):
            sleep(_RETRY_BASE_DELAY * (2 ** attempt))
            continue
        return resp
    # Unreachable: every iteration either returns, continues, or raises.
    raise AssertionError("retry loop exited without producing a response")


def _build_request_body(
    config: BackendConfig, messages: list[dict[str, str]],
) -> dict[str, Any]:
    """The outgoing body for one backend call, in that backend's wire shape.

    Extracted from `_call_backend` rather than inlined: adding the second
    wire took that function from cyclomatic 15 to 17, past the Elder cap,
    and the body shape is a self-contained decision with no bearing on the
    retry/cancellation logic around it.

    "responses" is OpenAI's Responses API - `input` not `messages`, JSON
    mode under `text.format` not `response_format`, and `max_output_tokens`
    not `max_tokens`. The cap is TRANSLATED rather than dropped, so a
    runaway generation stays bounded on this wire too (grug#883).
    """
    if config.wire != "responses":
        return {
            "model": config.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            **config.extra_body,
        }
    extra = dict(config.extra_body)
    max_tokens = extra.pop("max_tokens", None)
    body: dict[str, Any] = {
        "model": config.model,
        "input": messages,
        "text": {"format": {"type": "json_object"}},
        **extra,
    }
    if max_tokens is not None:
        body["max_output_tokens"] = max_tokens
    return body


def _merged_headers(config: BackendConfig, key: str) -> dict[str, str]:
    """The final outgoing header set for one call: static `extra_headers`,
    then `dynamic_headers` resolved at call time, then the bearer token -
    in that order, so Authorization always wins the merge even if a caller
    tried to sneak a same-named entry into either of the other two dicts.
    Extracted from `_call_backend` purely to shed one branch from that
    function's own complexity score (grug#984 follow-up); no behavior
    change - FLINT #618's guard now covers both header dicts."""
    if any(
        name.lower() == "authorization"
        for name in (*config.extra_headers, *config.dynamic_headers)
    ):
        raise _BackendConfigError(
            f"{config.backend.value} extra_headers must not contain Authorization"
        )
    dynamic = {name: fn() for name, fn in config.dynamic_headers.items()}
    return {
        **config.extra_headers,
        **{name: value for name, value in dynamic.items() if value is not None},
        "Authorization": f"Bearer {key}",
    }


def _validated_transport_attempts(config: BackendConfig) -> int:
    """Resolve and validate the retry budget; raises `_BackendConfigError`."""
    if config.retry_attempts < 1:
        raise _BackendConfigError(
            f"{config.backend.value} retry_attempts must be positive"
        )
    transport_attempts = (
        config.transport_retry_attempts
        if config.transport_retry_attempts is not None
        else config.retry_attempts
    )
    if transport_attempts < 1:
        raise _BackendConfigError(
            f"{config.backend.value} transport_retry_attempts must be positive"
        )
    if transport_attempts > config.retry_attempts:
        # The dispatch loop is bounded by retry_attempts, so a larger transport
        # budget can never be spent - and worse, a transport error on the final
        # attempt takes the `continue` branch (attempt < transport_attempts - 1
        # still holds), exhausts the loop, and raises the spurious
        # AssertionError below instead of re-raising the real transport error.
        raise _BackendConfigError(
            f"{config.backend.value} transport_retry_attempts "
            f"({transport_attempts}) must not exceed retry_attempts "
            f"({config.retry_attempts})"
        )
    return transport_attempts

def call(
    config: BackendConfig, messages: list[dict[str, str]],
    cancel_event: threading.Event | None = None,
    *, deadline: float | None, public: bool, sleep: Callable[[float], None],
) -> httpx.Response:
    """Single backend call with 429/503 retry + backoff. Raises
    `httpx.RequestError`/`httpx.TimeoutException` on transport failure
    or `_BackendConfigError` on misconfig - caller catches and falls
    back. Narrow exception scope deliberately: `httpx.InvalidURL`,
    `httpx.UnsupportedProtocol`, `httpx.CookieConflict` are config
    bugs that should crash loudly, not retry silently.

    `cancel_event` (#635 follow-up, mid-flight review cancellation): closing
    an `httpx.Client` from another thread does NOT actually interrupt an
    in-flight synchronous request - verified against a real local HTTP
    server before settling on this design, not assumed. So instead of
    trying to truly kill the blocked call, the request runs on a background
    thread and this function races it against `cancel_event`: whichever
    resolves first wins. If cancellation wins, `_call_backend` returns
    control to the caller immediately - the background thread is abandoned
    (daemon, unjoined) and keeps running to its own natural conclusion, its
    result silently discarded. This does NOT reduce server-side Spark
    compute (ollama has no way to know the caller gave up - a separate,
    already-documented limitation). What it DOES do is stop a superseded
    review from holding its queue slot / SQS message for the network call's
    full remaining duration, so the PR's next (current) commit doesn't have
    to wait behind it.

    Every other caller (the judge, the walkthrough summary, the SaaS
    overload fallback) passes no `cancel_event` and takes the direct,
    unmodified path below - no extra thread, no behavior change.

    OpenRouter free-tier gate (grug#870, epic #869): a `:free`-suffixed
    OpenRouter model first acquires a slot from the shared, cross-process
    limiter in `openrouter_free_limiter.py` before any network call is
    made. See that module's docstring for why an in-process limiter
    cannot enforce OpenRouter's account-wide caps against grug's 4
    replicas. Exhausting the bounded queue wait raises
    `RateLimitTimeoutError`, an `httpx.RequestError` subclass every
    caller below already catches - it falls back / degrades exactly like
    a transport failure, never like a call that quietly succeeded. Cave
    and Poolside never carry the `:free` suffix, so `is_free_tier_model`
    is the entire cost this gate adds to their calls."""
    if cancel_event is not None and cancel_event.is_set():
        raise httpx.RequestError("cancelled before dispatch")
    availability.prepare_call(config, public=public, cancel_event=cancel_event)
    try:
        key = config.key_loader()
    except Exception as e:
        # secrets_loader.RuntimeError("SSM parameter name is empty...")
        # or boto3 ClientError on a missing param. Wrap so the caller's
        # except clause is uniform.
        raise _BackendConfigError(
            f"{config.backend.value} key_loader failed: {type(e).__name__}: {e}"
        ) from e
    if not key:
        raise _BackendConfigError(
            f"{config.backend.value} key_loader returned empty string"
        )
    if config.backend == Backend.SUBSCRIPTION:
        _validate_subscription_key(key)

    body = _build_request_body(config, messages)
    headers = _merged_headers(config, key)

    transport_attempts = _validated_transport_attempts(config)
    with availability._opencode_go_free_slot(config):
        if cancel_event is None:
            resp = _post_with_retries(
                config.url, body, headers, config.timeout_seconds,
                config.retry_attempts, transport_attempts, deadline, sleep=sleep,
            )
        else:
            resp = _post_with_retries_cancellable(
                config, body, headers, transport_attempts, cancel_event, deadline, sleep=sleep,
            )
    availability.note_response(config, resp)
    return resp


# --- abandoned-LLM-call in-flight metric (#638) ------------------------------

# grug#637 added mid-flight review cancellation: when a newer commit lands,
# the waiter stops waiting on an in-flight backend call and returns
# immediately. The background call is abandoned, not killed - it keeps
# running to its own natural conclusion and still competes for the
# backend's generation slot (e.g. spark-gateway's single slot for the Cave
# target). Under rapid PR churn several abandoned generations for stale
# commits can stack up silently. This tracker counts the abandoned calls
# that are still running so the pileup is observable.


class _AbandonedCallTracker:
    """Thread-safe count of abandoned `_do_call` background calls still
    running. Incremented when the waiting side gives up on a still-running
    call, decremented when that call's real request finishes - regardless
    of whether the waiter already gave up. A call that finished before the
    abandon is never counted: it was never in flight AS abandoned."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def note_abandoned(self, call_state: dict, backend: str) -> None:
        """The waiting side gave up. `call_state` is this call's
        {"abandoned", "finished"} flags, shared with `_do_call`'s finally
        block; both transitions hold the same lock, so the count is exact
        even when abandon and finish race."""
        emit_count: int | None = None
        with self._lock:
            call_state["abandoned"] = True
            if not call_state["finished"]:
                self._in_flight += 1
                emit_count = self._in_flight
        if emit_count is not None:
            _emit_abandoned_in_flight_gauge(emit_count, backend)

    def call_finished(self, call_state: dict, backend: str) -> None:
        """The background call's real request finished (ok or error). Only
        decrements if this call was previously counted as abandoned."""
        emit_count: int | None = None
        with self._lock:
            call_state["finished"] = True
            if call_state["abandoned"]:
                self._in_flight -= 1
                emit_count = self._in_flight
        if emit_count is not None:
            _emit_abandoned_in_flight_gauge(emit_count, backend)


_abandoned_calls = _AbandonedCallTracker()


def _emit_abandoned_in_flight_gauge(count: int, backend: str) -> None:
    """Dual emission for the abandoned-call gauge, same shape as
    `openrouter_free_limiter._emit_telemetry`: one structured log token
    (the operator-grep / DD-log-monitor token) plus the DogStatsD gauge a
    dashboard graphs directly. Emitted on every transition (abandon and
    drain), so the gauge always reflects the current count - a silent
    series must never be read as "zero abandoned".

    Expected range, for a future alert (no alert yet - this ships the
    signal, per #638's out-of-scope boundary):
    - 0 nearly always: abandoned calls drain as their real requests finish.
    - Brief 1-3 under rapid PR churn (several pushes inside one review's
      ~330-660s budget): normal; each drains within the backend timeout.
    - Sustained > 5, or a count that climbs without draining: abandoned
      generations are piling up faster than they finish and competing
      with live reviews for the backend slot - the silent pileup #638
      describes. Alert candidate: max over 5m > 5.

    Never raises into the call path it describes."""
    log.info(
        "llm_abandoned_call_in_flight",
        extra={"backend": backend, "abandoned_in_flight": count},
    )
    try:
        from observability import emit_gauge  # type: ignore  # late: webhook-image only
    except Exception:  # noqa: BLE001 - telemetry must never break the call it describes
        return
    emit_gauge("grug.llm.abandoned_calls_in_flight", float(count), {"backend": backend})


def _post_with_retries_cancellable(
    config: BackendConfig, body: dict[str, Any], headers: dict[str, str],
    transport_attempts: int, cancel_event: threading.Event,
    deadline: float | None = None,
    *, sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """`_call_backend`'s cancellable path (#635 follow-up), split out to keep
    the call's branch count down. Runs the real call on a
    background thread, races it against `cancel_event` via a 1-item queue -
    whichever resolves first wins. See `_call_backend`'s docstring for why
    this is "abandon the loser", not "kill the loser"."""
    result_q: queue.Queue = queue.Queue(maxsize=1)
    # Shared with _do_call's finally block; the tracker resolves the
    # abandon-vs-finish race under its own lock (#638).
    call_state = {"abandoned": False, "finished": False}
    backend_tag = config.backend.value

    def _do_call() -> None:
        try:
            resp = _post_with_retries(
                config.url, body, headers, config.timeout_seconds,
                config.retry_attempts, transport_attempts, deadline, sleep=sleep,
            )
            result_q.put(("ok", resp))
        except Exception as e:  # noqa: BLE001 - re-raised on the waiting side
            result_q.put(("error", e))
        finally:
            _abandoned_calls.call_finished(call_state, backend_tag)

    # Re-check immediately before spawning (FLINT, #637): _call_backend's
    # own top-level guard only catches cancellation that was ALREADY set
    # before key_loader()/body/header construction ran - if it fires during
    # that window, this is the last chance to skip starting a doomed
    # background request instead of spawning it just to abandon it moments
    # later in the loop below.
    if cancel_event.is_set():
        raise httpx.RequestError("cancelled before dispatch")
    threading.Thread(target=_do_call, daemon=True).start()
    while True:
        if cancel_event.is_set():
            # The background call is abandoned, not killed: it keeps
            # running to its own conclusion. Count it while it is still
            # in flight (#638).
            _abandoned_calls.note_abandoned(call_state, backend_tag)
            raise httpx.RequestError("cancelled mid-flight")
        try:
            kind, payload = result_q.get(timeout=0.25)
            break
        except queue.Empty:
            continue
    if kind == "error":
        raise payload
    return payload


def responses_envelope_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Rewrite an OpenAI Responses API reply into the chat-completions shape.

    `output` is a list of typed blocks, and a reasoning model emits
    `reasoning` blocks alongside the `message` one - so this picks the
    message block rather than assuming output[0], which would hand the
    parser a reasoning block and produce a confusing "missing content"
    instead of the real answer sitting one element later.

    Within that block, `content` is itself a list of typed parts; the answer
    is the `output_text` part. Anything unexpected degrades to
    `content: None`, which _parse_response already diagnoses precisely
    (grug#881) rather than raising.

    `status` is carried into `finish_reason` because "incomplete" is this
    wire's equivalent of `length` - the truncation signal grug#851 exists to
    surface, which must never read as a clean empty review.
    """
    text: str | None = None
    for block in body.get("output") or []:
        if not isinstance(block, dict) or block.get("type") != "message":
            continue
        for part in block.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                value = part.get("text")
                if isinstance(value, str):
                    text = value
                break
        break
    status = body.get("status")
    return {
        "model": body.get("model", ""),
        "choices": [{
            "message": {"content": text},
            "finish_reason": "length" if status == "incomplete" else status,
        }],
        "usage": body.get("usage", {}),
    }
