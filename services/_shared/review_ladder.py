"""Elder's per-chunk routing policy and failure history."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from threading import Event
from typing import TYPE_CHECKING

import httpx

import model_availability as availability
import opencode_go_free
from llm_backend import Backend, BackendConfig
from model_family import model_family
from openrouter_free_limiter import is_free_tier_model
from provider_wire import ReviewDeadlineReached

if TYPE_CHECKING:
    from llm_client import AttemptOutcome, Finding, LlmReviewResponse, PrContext, _ArmOutcome

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.llm_client")


@dataclass(frozen=True, slots=True)
class _LadderAttempt:
    backend: str
    model: str
    reason: str
    # Seconds to wait before asking this model again, for a rate limit (429)
    # that a wait can clear. 0 means the failure is final for this chunk
    # (grug#1494).
    retry_after: float = 0.0
    through_gateway: bool = False
    outage_failure: bool = False

    @property
    def label(self) -> str:
        return f"{self.model} ({self.reason})"


_GATEWAY_OUTAGE_REASON = "inference gateway outage"
# A gateway call the review deadline cut short failed neither way: it does not
# show the gateway down, and it does not show it up.
_NEUTRAL_GATEWAY_REASONS = frozenset({ReviewDeadlineReached.__name__})


def _with_gateway_outage_named(trail: list[_LadderAttempt]) -> list[_LadderAttempt]:
    """Name the outage on one failed walk's gateway attempts (grug#1467).

    Only when every gateway attempt failed that way: one arm timing out while
    the other answers is a slow model. The labels reach the check title and
    summary through coverage, and `tried` keeps them past the chunk retry,
    whose skipped calls reach no model and could not name the outage."""
    gateway = [
        attempt for attempt in trail
        if attempt.through_gateway and attempt.reason not in _NEUTRAL_GATEWAY_REASONS
    ]
    if not gateway or not all(attempt.outage_failure for attempt in gateway):
        return trail
    return [
        replace(attempt, reason=f"{_GATEWAY_OUTAGE_REASON}: {attempt.reason}")
        if attempt.through_gateway and attempt.outage_failure else attempt
        for attempt in trail
    ]


@dataclass(frozen=True, slots=True)
class _GatewayCall:
    """One model call through the inference gateway in a ladder walk
    (grug#1467). `neutral` marks a call the review deadline cut short, which
    says nothing about whether the gateway is up."""

    backend: str
    model: str
    outage: bool
    neutral: bool


def _gateway_down(calls: Sequence[_GatewayCall]) -> bool:
    """True when the walk reached the gateway and every call that ran to an
    answer failed the way a down or load-shedding gateway fails. One arm
    answering, or failing any other way, means the gateway is up."""
    counted = [call for call in calls if not call.neutral]
    return bool(counted) and all(call.outage for call in counted)


# The longest the chunk retry waits out a 429's Retry-After, and the wait when
# the 429 names none. The staged deadline still clamps the retried call.
_RATE_LIMIT_WAIT_CAP_SECONDS = 30.0
_RATE_LIMIT_WAIT_DEFAULT_SECONDS = 5.0


def _rate_limit_wait(outcome: "AttemptOutcome") -> float:
    """Seconds a 429 asks the caller to wait, or 0.0 when this failure is not
    a rate limit a wait can clear (any other failure, or a spent allowance)."""
    resp = outcome.raw_response
    if outcome.status_code != 429 or resp is None or availability._is_spent_quota_refusal(resp):
        return 0.0
    try:
        seconds = float((resp.headers.get("retry-after") or "").strip())
    except ValueError:
        return _RATE_LIMIT_WAIT_DEFAULT_SECONDS
    if not math.isfinite(seconds) or seconds <= 0:
        return _RATE_LIMIT_WAIT_DEFAULT_SECONDS
    return min(seconds, _RATE_LIMIT_WAIT_CAP_SECONDS)


class _ChunkLadder:
    """Per-chunk memory of failed model calls across attempts (grug#1196).

    The staged scheduler retries a failed chunk once. That retry used to walk
    the same ladder again from the top, so it re-asked the models that had
    just failed and was a coin flip (one large PR failed three times in a row).
    Here the retry skips every model that already failed on this chunk and
    goes on to the next one in the ladder; when none is left it costs no
    network call. A failed chunk's response carries every attempt so the
    neutral check can name the models tried and why each failed."""

    def __init__(self, sleep: Callable[[float], None]) -> None:
        self._sleep = sleep
        self._tried: dict[int, list[_LadderAttempt]] = {}
        # grug#1494: the wait a chunk's last walk was told to make by a 429.
        self._wait: dict[int, float] = {}
        self._parse_failed: dict[int, LlmReviewResponse] = {}

    def run(
        self, key: int, call: Callable[[ChunkAttempt], LlmReviewResponse],
    ) -> LlmReviewResponse:
        response = self._run_once(key, call)
        # A model that answered unparseably is a more specific failure than a
        # retry that reached no model at all, so `parse_failed` keeps winning
        # over `all_failed` across attempts, exactly as it does inside one.
        # The FIRST unparseable answer keeps the attribution, as it does there.
        if response.kind == "parse_failed":
            self._parse_failed.setdefault(key, response)
        if response.kind != "reviewed" and key in self._parse_failed:
            response = replace(self._parse_failed[key], attempts=response.attempts)
        return response

    def _run_once(
        self, key: int, call: Callable[[ChunkAttempt], LlmReviewResponse],
    ) -> LlmReviewResponse:
        tried = self._tried.setdefault(key, [])
        # A rate-limited model is asked again after its wait; only models that
        # really failed are skipped (grug#1494).
        skip = frozenset(
            (attempt.backend, attempt.model)
            for attempt in tried if not attempt.retry_after
        )
        wait = self._wait.pop(key, 0.0)
        if wait:
            self._sleep(wait)
        chunk = ChunkAttempt(skip=skip)
        response = call(chunk)
        trail = chunk.trail
        if skip:
            log.info(
                "llm_chunk_retry_next_model",
                extra={
                    "chunk": key + 1,
                    "skipped_models": sorted(model for _, model in skip),
                    "new_attempts": [attempt.label for attempt in trail],
                    "kind": response.kind,
                },
            )
        tried.extend(
            trail if response.kind == "reviewed" else _with_gateway_outage_named(trail)
        )
        waits = [attempt.retry_after for attempt in trail if attempt.retry_after]
        if waits and response.kind != "reviewed":
            self._wait[key] = max(waits)
        if response.kind in ("all_failed", "parse_failed") and tried:
            labels = tuple(dict.fromkeys(attempt.label for attempt in tried))
            return replace(response, attempts=labels)
        return response


_GATEWAY_OUTAGE_STATUSES = frozenset({503, 504})
# By name, because `AttemptOutcome` keeps only the class name. Not the base
# `httpx.RequestError`: a cancelled call, a passed deadline and a disabled
# backend all raise a subclass of it and none of them is an outage.
_GATEWAY_OUTAGE_TRANSPORT_ERRORS = frozenset(
    error.__name__
    for error in (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
    )
)


def _routes_through_gateway(url: str) -> bool:
    base = os.getenv("GRUG_CAVE_GATEWAY_URL", "").strip().rstrip("/")
    return bool(base) and url.startswith(f"{base}/")


def _is_outage_failure(outcome: AttemptOutcome, reason: str) -> bool:
    """Is this failure what a down or load-shedding gateway produces?"""
    if outcome.error_kind == "transport":
        return reason in _GATEWAY_OUTAGE_TRANSPORT_ERRORS
    return outcome.status_code in _GATEWAY_OUTAGE_STATUSES


def _ladder_failure_reason(outcome: AttemptOutcome) -> str | None:
    """Short reason for a failed attempt, or None when it answered usably."""
    if outcome.error_kind is None:
        return None
    if outcome.error_kind == "config":
        return "misconfigured"
    if outcome.error_kind == "parse_failed":
        return "parse_failed"
    text = outcome.error_text or outcome.error_kind
    return text.rsplit(": ", 1)[-1]


def _gateway_outage_order(
    configs: list[BackendConfig], calls: Sequence[_GatewayCall],
) -> list[BackendConfig]:
    """Tier 2's order for this walk (grug#1467).

    Unchanged unless the gateway is down. Then the off-gateway backends come
    first, so the review's one retry outside the gateway runs before the
    deadline is spent on a gateway that is not answering, and a gateway arm
    this walk already saw fail is dropped. A gateway arm not yet tried stays
    last: the gateway may still serve it."""
    if not calls or not _gateway_down(calls):
        return configs
    failed = {(call.backend, call.model) for call in calls}
    off_gateway = [c for c in configs if not _routes_through_gateway(c.url)]
    untried_gateway = [
        c for c in configs
        if _routes_through_gateway(c.url) and (c.backend.value, c.model) not in failed
    ]
    ordered = off_gateway + untried_gateway
    log.warning(
        "llm_gateway_outage_fallback",
        extra={
            "gateway_calls": len(calls),
            "order": [f"{c.backend.value}/{c.model}" for c in ordered],
            "off_gateway": len(off_gateway),
        },
    )
    return ordered


# A model that answers a 10-20k-token diff with `{"findings": []}` in 7
# output tokens and 2-4s did not review it (nemotron-3-super:free, 25 such
# calls in 3 hours on 2026-09-26, each published as a clean pass).
_DEGENERATE_MAX_OUTPUT_TOKENS = 50
_DEGENERATE_MIN_INPUT_TOKENS = 2_000
# grug#1642: input tokens count the whole prompt, and the fixed review context
# alone is about 20,000 tokens, so every short empty answer cleared the 2,000
# bar however small the diff. The bar was meant for the diff: 2,000 tokens at
# about 4 characters per token is 8,000 characters. A diff under that is small
# enough that an empty answer is a real clean review.
_DEGENERATE_MIN_DIFF_CHARS = _DEGENERATE_MIN_INPUT_TOKENS * 4


def _is_degenerate_empty_review(
    findings: Sequence[Finding], usage: dict[str, int | float], diff_chars: int | None = None,
) -> bool:
    """No findings, under 50 output tokens, over 2k input tokens, on a diff
    of at least `_DEGENERATE_MIN_DIFF_CHARS` (grug#1642). Missing usage
    counts is not evidence either way, so it never flags. An unknown diff
    size keeps the token rule alone."""
    if findings:
        return False
    if diff_chars is not None and diff_chars < _DEGENERATE_MIN_DIFF_CHARS:
        return False
    output_tokens = usage.get("output_tokens")
    input_tokens = usage.get("input_tokens")
    if output_tokens is None or input_tokens is None:
        return False
    return (
        output_tokens < _DEGENERATE_MAX_OUTPUT_TOKENS
        and input_tokens > _DEGENERATE_MIN_INPUT_TOKENS
    )


@dataclass(frozen=True, slots=True)
class _CloudPrimaryResult:
    """What the cloud chain produced for one review chunk.

    `answer` is the review when a tier answered; the caller returns it as-is.
    With no answer, `low_confidence` is the first short empty answer, if any:
    the caller still tries the Cave and keeps this only when the Cave fails
    too (grug#1097)."""

    answer: LlmReviewResponse | None = None
    low_confidence: LlmReviewResponse | None = None


def _empty_confirms_nothing(key: "tuple[Backend, str]") -> bool:
    """A short empty answer from a model that gives one almost every time.

    OpenRouter's `:free` model answers empty in about 7 tokens on 10k-44k
    token cohorts (seen deciding a vote 2026-10-05, and on every call
    2026-10-19). The gateway's free pool is whichever free model had room,
    gemma4:31b among them, which did the same on 2026-10-19. Neither side of
    a vote may be one of these: with the free tiers first in the chain
    (grug#1269), the first empty was always one, and the old check looked
    only at the second, so two lazy answers passed PRs as clean."""
    backend, model = key
    if backend == Backend.OPENROUTER and is_free_tier_model(model):
        return True
    # Go's free models are unranked on review quality (grug#1561); Beam Free
    # answered empty on a 33k-token diff DeepSeek found faults in (2026-11-01).
    if backend == Backend.OPENCODE_GO and availability._opencode_go_model_is_free(model):
        return True
    return backend == Backend.FREE_POOL


def _free_tier_found_nothing(tier: BackendConfig, outcome: _ArmOutcome) -> bool:
    """An empty answer from a free tier is never a clean pass. The short-
    answer check misses it: Beam Free spent 1792 output tokens, all reasoning,
    on the empty answer above, and an answer with no usage is never flagged."""
    return not outcome.findings and _empty_confirms_nothing((tier.backend, tier.model))


# grug#1601: the narrow relaxation of the rule above. A free Go model answered
# empty on a 33k-token diff where the paid primary found faults, so the default
# stands: a free model's empty answer confirms nothing. On a small cohort, after
# a paid or ranked model already answered empty, a free model that an eval
# measured at a false-empty rate of 5 percent or less may be the second opinion.
# Off unless GRUG_OPENCODE_GO_FREE_CONFIRM is set.
_FREE_CONFIRM_DEFAULT_MAX_TOKENS = 10_000
_CHARS_PER_TOKEN = 4


def _free_confirm_on() -> bool:
    return os.getenv("GRUG_OPENCODE_GO_FREE_CONFIRM", "").strip().lower() in ("1", "true", "yes")


def _free_confirm_cohort_is_small(chars: int | None) -> bool:
    """The cohort under review is at most the configured tokens, from its diff
    characters at about four to a token. An unknown size is never small."""
    if chars is None:
        return False
    limit = availability._env_number(
        "GRUG_OPENCODE_GO_FREE_CONFIRM_MAX_TOKENS", _FREE_CONFIRM_DEFAULT_MAX_TOKENS, int,
    )
    return limit > 0 and chars <= limit * _CHARS_PER_TOKEN


def _free_confirm_allowlist() -> frozenset[str]:
    max_age = availability._env_number(
        "GRUG_OPENCODE_GO_FREE_CONFIRM_MAX_AGE_DAYS", opencode_go_free.DEFAULT_REPORT_MAX_AGE_DAYS, int,
    )
    path = os.getenv("GRUG_OPENCODE_GO_FREE_CONFIRM_REPORT", "").strip() or None
    return opencode_go_free.qualified_models(
        opencode_go_free.load_eval_report(path), max_age_days=max_age,
    )


def _free_empty_may_confirm(current: tuple[Backend, str], diff_chars: int | None) -> bool:
    """May this free model's empty answer count as the second opinion? Only for
    a Go model that is free now, on a small cohort, when it is on the measured
    allowlist and the flag is on. The caller has already required that the
    first empty answer came from a paid or ranked model."""
    backend, model = current
    return (
        _free_confirm_on()
        and backend == Backend.OPENCODE_GO
        and availability._opencode_go_model_is_free(model)
        and _free_confirm_cohort_is_small(diff_chars)
        and model in _free_confirm_allowlist()
    )


def _empty_answers_agree(
    first_empty: "tuple[Backend, str] | None",
    current: "tuple[Backend, str]",
    pr_context: PrContext | None,
    diff_chars: int | None = None,
) -> bool:
    """True when a second, DIFFERENT model also gave a short empty answer.

    One short empty answer can be a skim; two independent models agreeing is
    a clean review. Walking on burned the staged budget and starved later
    cohorts (2026-10-05). The same model twice is not agreement."""
    if first_empty is None or first_empty == current:
        return False
    if _empty_confirms_nothing(first_empty):
        return False
    relaxed = _empty_confirms_nothing(current)
    if relaxed and not _free_empty_may_confirm(current, diff_chars):
        return False
    log.info(
        "llm_cloud_empty_consensus",
        extra={
            "relaxed": relaxed,
            "first_backend": first_empty[0].value,
            "first_model": first_empty[1],
            "second_backend": current[0].value,
            "second_model": current[1],
            "repo": (pr_context or {}).get("repo"),
            "pr_number": (pr_context or {}).get("pr_number"),
        },
    )
    return True


_CAVE_REVIEW_CODER_DEFAULT_MODEL = "qwen3-coder-next:q8_0"
_CAVE_REVIEW_REASONER_DEFAULT_MODEL = "qwen3.5:122b"


# The models whose empty answer the Cave path trusts. Everything else is
# untrusted: the route spills to whatever is free while the Sparks are away,
# and no name pattern can tell a spill from a resident model.
_TRUSTED_EMPTY_CAVE_MODELS = frozenset({
    _CAVE_REVIEW_CODER_DEFAULT_MODEL, _CAVE_REVIEW_REASONER_DEFAULT_MODEL,
})


def _trusted_empty_cave_models() -> frozenset[str]:
    """`GRUG_CAVE_TRUSTED_EMPTY_MODELS` (comma-separated), else the defaults."""
    raw = os.getenv("GRUG_CAVE_TRUSTED_EMPTY_MODELS", "")
    named = frozenset(m.strip() for m in raw.split(",") if m.strip())
    return named or _TRUSTED_EMPTY_CAVE_MODELS


def _cave_empty_is_trusted(outcome: "_ArmOutcome") -> bool:
    """A named, listed model that took a real look. A reply naming no model
    fails closed."""
    return (
        not outcome.degenerate
        and not outcome.model_unconfirmed
        and outcome.model in _trusted_empty_cave_models()
    )


@dataclass(slots=True)
class ChunkAttempt:
    """Retry history shared by both family walks of one chunk attempt."""

    skip: frozenset[tuple[str, str]] = frozenset()
    trail: list[_LadderAttempt] = field(default_factory=list)


@dataclass(slots=True)
class ReviewWalk:
    """Routing data for a single walk, including calls made by both Cave arms."""

    diff_chars: int | None = None
    author_family: str | None = None
    same_family_only: bool = False
    chunk: ChunkAttempt | None = None
    lane_first: bool = False
    lane_asked: bool = False
    # Concurrent arms only append. Fallback ordering reads the list after both join.
    gateway_calls: list[_GatewayCall] = field(default_factory=list)

    def family_gated(self, model: str) -> bool:
        return self.author_family is not None and (
            (model_family(model) == self.author_family) != self.same_family_only
        )

    def skip_reason(self, backend: Backend, model: str) -> str:
        if self.family_gated(model):
            return f"{backend.value}: {model} is gated out by model family"
        if self.chunk is not None and (backend.value, model) in self.chunk.skip:
            return f"{backend.value}: {model} already failed on this chunk"
        return ""

    def author_family_answered(self, model: str) -> bool:
        # The lane picks its own model. On the first walk, verify who answered.
        return not self.same_family_only and self.family_gated(model)

    def record(self, config: BackendConfig, outcome: AttemptOutcome) -> None:
        backend = outcome.backend.value
        through_gateway = _routes_through_gateway(config.url)
        reason = _ladder_failure_reason(outcome)
        outage = reason is not None and _is_outage_failure(outcome, reason)
        if through_gateway:
            self.gateway_calls.append(_GatewayCall(
                backend, config.model, outage, reason in _NEUTRAL_GATEWAY_REASONS,
            ))
        if self.chunk is not None and reason is not None:
            self.chunk.trail.append(_LadderAttempt(
                backend, config.model, reason, _rate_limit_wait(outcome),
                through_gateway, outage,
            ))


def _lane_reviews_first(priority: str, repo_public: bool) -> bool:
    """Only public diffs can lead with a lane whose account settings are unknown."""
    return priority == "subscription" and repo_public


def walk_review(
    *,
    author_family: str | None,
    diff_chars: int,
    chunk: ChunkAttempt | None,
    cancel_event: Event | None,
    priority: str,
    repo_public: bool,
    cloud: Callable[[ReviewWalk], _CloudPrimaryResult],
    cave: Callable[[ReviewWalk], LlmReviewResponse],
    pr_context: PrContext | None,
) -> LlmReviewResponse:
    """Try other families first, then the author's family if neither path answered."""
    def run(same_family_only: bool) -> LlmReviewResponse:
        walk = ReviewWalk(
            diff_chars=diff_chars,
            author_family=author_family,
            same_family_only=same_family_only,
            chunk=chunk,
            lane_first=_lane_reviews_first(priority, repo_public),
        )
        result = cloud(walk) if priority != "cave" else _CloudPrimaryResult()
        if result.answer is not None:
            return result.answer
        response = cave(walk)
        low_confidence = result.low_confidence
        if low_confidence is None or response.kind not in ("all_failed", "parse_failed"):
            return response
        log.warning(
            "llm_review_low_confidence_kept",
            extra={
                "backend": low_confidence.backend_used.value
                if low_confidence.backend_used is not None else None,
                "model": low_confidence.model_name,
                "cave_kind": response.kind,
                "repo": (pr_context or {}).get("repo"),
                "pr_number": (pr_context or {}).get("pr_number"),
            },
        )
        return low_confidence

    response = run(False)
    if not author_family or response.kind == "reviewed" or (cancel_event and cancel_event.is_set()):
        return response
    same_family = run(True)
    return same_family if same_family.kind == "reviewed" else response
