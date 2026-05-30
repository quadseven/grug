# MIRRORED — sibling at services/api/llm_client.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""LLM client abstraction for the Code-Reviewer (Elder) persona.

Sends review prompts to either Poolside (Laguna) or OpenRouter and
returns a structured response. Backend selection is stable per-install
via `installation_id % 2`: two PRs on the same install always hit the
same backend, which lets DD LLM Obs A/B-compare prompt variants without
cross-install noise. Traffic distribution across the two backends
depends on how installs are sized — it is NOT a true even split.
If the primary backend errors hard (post-retry), the other backend is
tried before surfacing an empty response — the caller posts an advisory
check-run rather than 500ing the webhook handler on transient LLM
failures.

Both backends use the OpenAI-compatible chat-completions API so the
request shape is identical. Only the base URL, auth header, and
default model name differ. Response is constrained to JSON via
`response_format={"type": "json_object"}` and parsed defensively —
malformed JSON or refusals degrade to empty findings rather than crash.

Secrets are loaded via secrets_loader.py (`/grug/poolside-api-key` +
`/grug/openrouter-api-key`); api Lambda has no IAM grant on these
paths so this module only ever runs from the webhook process.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Optional, TypedDict, get_args

import httpx

from secrets_loader import get_openrouter_api_key, get_poolside_api_key

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.llm_client")

# DD LLM Observability seams. Imported lazily and wrapped behind module-
# level indirection so:
#   1. Tests can monkeypatch `_llmobs_llm` / `_llmobs_annotate` without
#      touching the real ddtrace.llmobs SDK.
#   2. Cold-start cost of `ddtrace.llmobs` is paid once on first call,
#      not at import time.
#   3. If `DD_LLMOBS_ENABLED` is unset (local dev, tests), the span
#      becomes a no-op rather than failing loudly.
try:  # pragma: no cover — import-time guard
    from ddtrace.llmobs import LLMObs as _LLMObs

    def _llmobs_llm(**kwargs: Any) -> Any:
        return _LLMObs.llm(**kwargs)

    def _llmobs_annotate(**kwargs: Any) -> None:
        _LLMObs.annotate(**kwargs)
except ImportError:  # pragma: no cover — local dev without ddtrace

    class _NoopSpan:
        def __enter__(self) -> "_NoopSpan":
            return self
        def __exit__(self, *a: Any) -> bool:
            return False

    def _llmobs_llm(**kwargs: Any) -> Any:
        return _NoopSpan()

    def _llmobs_annotate(**kwargs: Any) -> None:
        return None

    # Loud signal so a layer-drift / partial-install in Lambda doesn't
    # silently turn off DD LLM Obs. AWS_LAMBDA_FUNCTION_NAME is the
    # canonical Lambda-environment marker; in local dev it's unset.
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        log.warning(
            "llmobs_import_failed_falling_back_to_noop",
            extra={"lambda_fn": os.environ["AWS_LAMBDA_FUNCTION_NAME"]},
        )

_LLMOBS_NAME = "elder_code_review"
_LLMOBS_HEAD_SHA_TAG_LEN = 8  # truncated to keep tag cardinality bounded


class PrContext(TypedDict, total=False):
    """PR coords threaded into DD LLM Obs span tags.

    `total=False` (every key optional) because callers without GH
    coords (e.g. ad-hoc tests, future REPL probes) still need to be
    able to call `review_diff` — they just get traces without
    PR-filterable tags. Promoting from a bare `dict` makes typos like
    `pr_num` fail at type-check time rather than silently dropping the
    tag in DD.
    """
    installation_id: int
    repo: str
    pr_number: int
    head_sha: str


def _elapsed_ms(start_ns: int) -> int:
    """Wall-clock elapsed in ms since `start_ns` (monotonic). Used for
    LLM Obs latency metrics — `time.monotonic_ns` avoids clock-skew."""
    return (time.monotonic_ns() - start_ns) // 1_000_000

# Per-backend endpoints + default models.
_POOLSIDE_URL = "https://inference.poolside.ai/v1/chat/completions"
_POOLSIDE_MODEL = "poolside/laguna-m.1"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"

_TIMEOUT_SECONDS = 30
_RETRY_ATTEMPTS = 3
# Exponential backoff applies on every attempt except the final one,
# which either returns the response or raises.
_RETRY_BASE_DELAY = 0.5

# 429 (rate limit) + 503 (CF edge blip / temporary backend overload)
# are routinely transient on both Poolside and OpenRouter. Other 5xx
# (500, 502, 504) return immediately; review_diff then falls back to
# the secondary backend rather than burning retries on what may be a
# permanent issue.
_RETRYABLE_STATUSES: frozenset[int] = frozenset((429, 503))


class Backend(str, Enum):
    """LLM backends. String-valued so DD LLM Obs tags + structured logs
    can `backend=str(backend)` without a cast."""

    POOLSIDE = "poolside"
    OPENROUTER = "openrouter"


Severity = Literal["low", "medium", "high", "critical"]
# Derived from the Literal so adding a level (e.g. "info") in one place
# also updates parse-time validation. Without `get_args`, the two lists
# silently drift.
_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))


@dataclass(frozen=True, slots=True)
class Hunk:
    """One diff hunk presented to the model as a single review unit."""

    path: str
    body: str


@dataclass(frozen=True, slots=True)
class Finding:
    """A single review finding, validated at parse time.

    Hostile or hallucinating LLMs sometimes return findings with bogus
    severity strings or non-string fields. Parsing into this dataclass
    drops malformed entries with a warning so the Elder caller iterates
    over a known shape rather than `Any`.
    """

    path: str
    line: int
    rule: str
    severity: Severity
    message: str


@dataclass(frozen=True, slots=True)
class BackendConfig:
    """All the data a single backend dispatch needs. Replacing per-backend
    if/else branches with a `BackendConfig` lookup means adding a third
    backend is one new entry, not four scattered edits."""

    backend: Backend
    url: str
    model: str
    key_loader: Callable[[], str]


@dataclass(frozen=True, slots=True)
class LlmReviewResponse:
    """Result of one review_diff call.

    `kind` is the load-bearing discriminator the caller switches on:
      - `"no_diff"`: empty hunks, no LLM ran. Don't post anything.
      - `"reviewed"`: at least one backend returned a parseable payload.
        `findings` may be empty (clean review). Always carries
        backend + model attribution.
      - `"parse_failed"`: LLM responded with non-JSON or prose. Caller
        posts an advisory check-run with the error.
      - `"all_failed"`: every backend errored. Caller posts a
        "skipped" advisory check-run.

    Keeping all four states in one dataclass instead of a true union
    keeps the call sites cheap (one isinstance check vs many) at the
    cost of mildly redundant `Optional[...]` fields. Acceptable v1.
    """

    kind: Literal["no_diff", "reviewed", "parse_failed", "all_failed"]
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    backend_used: Optional[Backend] = None
    model_name: Optional[str] = None
    error: str = ""


# Test hook — replaced with a no-op in unit tests to avoid real sleeps.
def _RETRY_SLEEP(seconds: float) -> None:
    time.sleep(seconds)


# Test hooks — wrap secret loaders so tests can patch them without
# monkeypatching the SSM client. Production-equivalent thin wrappers.
def _load_poolside_key() -> str:
    return get_poolside_api_key()


def _load_openrouter_key() -> str:
    return get_openrouter_api_key()


# Single source of truth for per-backend dispatch data. Adding a third
# backend = one new entry; review_diff's "try every backend" loop
# generalizes without touching the type-design.
_BACKEND_CONFIGS: dict[Backend, BackendConfig] = {
    Backend.POOLSIDE: BackendConfig(
        backend=Backend.POOLSIDE,
        url=_POOLSIDE_URL,
        model=_POOLSIDE_MODEL,
        # Lambda (not bare ref) defers the name lookup to call time so
        # `monkeypatch.setattr(lc, "_load_poolside_key", ...)` in tests
        # actually reaches the dispatch. A bare reference captures the
        # original function at import; the patch then mutates only
        # `lc._load_poolside_key`, which `_BACKEND_CONFIGS` no longer
        # consults.
        key_loader=lambda: _load_poolside_key(),
    ),
    Backend.OPENROUTER: BackendConfig(
        backend=Backend.OPENROUTER,
        url=_OPENROUTER_URL,
        model=_OPENROUTER_MODEL,
        key_loader=lambda: _load_openrouter_key(),
    ),
}


def select_backend(installation_id: int) -> Backend:
    """Stable per-install backend pick via `installation_id % 2`.

    Two PRs on the same install always hit the same backend, which lets
    DD LLM Obs compare prompt variants without cross-install noise.

    Coupled to a 2-backend `Backend` enum. The assert is the only thing
    that fails loudly when a third backend is added — the modulo math
    would silently keep returning Poolside/OpenRouter and the new
    backend would never be picked.
    """
    assert len(Backend) == 2, (
        "select_backend assumes a 2-backend enum; add a real selector "
        "before extending Backend."
    )
    return Backend.POOLSIDE if installation_id % 2 == 0 else Backend.OPENROUTER


_SYSTEM_PROMPT = (
    "You are a senior code reviewer for the Grug bot. Review the supplied "
    "diff hunks and return JSON of shape "
    '{"findings": [{"path": str, "line": int, "rule": str, "severity": '
    '"low"|"medium"|"high"|"critical", "message": str}]}. '
    "Only flag concrete, actionable bugs (silent failures, secret leakage, "
    "obvious correctness errors). If the diff has no issues, return "
    '{"findings": []}. Do not include prose outside the JSON object.'
)


def _build_messages(hunks: list[Hunk]) -> list[dict[str, str]]:
    user = "\n\n".join(
        f"### {h.path}\n```diff\n{h.body}\n```" for h in hunks
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


class _BackendConfigError(Exception):
    """Backend is misconfigured (empty key, missing env var, SSM
    failure). Distinct from transport errors so the caller can fall
    back to the other backend without retry-burning the broken one."""


def _call_backend(
    config: BackendConfig, messages: list[dict[str, str]]
) -> httpx.Response:
    """Single backend call with 429/503 retry + backoff. Raises
    `httpx.RequestError`/`httpx.TimeoutException` on transport failure
    or `_BackendConfigError` on misconfig — caller catches and falls
    back. Narrow exception scope deliberately: `httpx.InvalidURL`,
    `httpx.UnsupportedProtocol`, `httpx.CookieConflict` are config
    bugs that should crash loudly, not retry silently."""
    try:
        key = config.key_loader()
    except Exception as e:
        # secrets_loader.RuntimeError("SSM parameter name is empty…")
        # or boto3 ClientError on a missing param. Wrap so the caller's
        # except clause is uniform.
        raise _BackendConfigError(
            f"{config.backend.value} key_loader failed: {type(e).__name__}: {e}"
        ) from e
    if not key:
        raise _BackendConfigError(
            f"{config.backend.value} key_loader returned empty string"
        )

    body = {
        "model": config.model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {key}"}

    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = httpx.post(
                config.url, json=body, headers=headers, timeout=_TIMEOUT_SECONDS,
            )
        except (httpx.RequestError, httpx.TimeoutException) as e:
            if attempt < _RETRY_ATTEMPTS - 1:
                _RETRY_SLEEP(_RETRY_BASE_DELAY * (2 ** attempt))
                continue
            raise
        if resp.status_code in _RETRYABLE_STATUSES and attempt < _RETRY_ATTEMPTS - 1:
            _RETRY_SLEEP(_RETRY_BASE_DELAY * (2 ** attempt))
            continue
        return resp
    # Unreachable: every iteration either returns, continues, or raises.
    raise AssertionError("retry loop exited without producing a response")


def _coerce_finding(raw: Any) -> tuple[Optional[Finding], str]:
    """Validate one raw dict from the LLM into a `Finding`. Returns
    `(finding, "")` on success or `(None, reason)` on rejection so
    the caller can log per-entry context (defense against a hostile
    LLM hiding a critical finding by mixing it with malformed ones).
    """
    if not isinstance(raw, dict):
        return None, "non_dict"
    try:
        path = str(raw["path"])
        line = int(raw["line"])
        rule = str(raw["rule"])
        severity = str(raw["severity"])
        message = str(raw.get("message", ""))
    except KeyError as e:
        return None, f"missing_field:{e.args[0]}"
    except (TypeError, ValueError) as e:
        return None, f"bad_type:{type(e).__name__}"
    if severity not in _VALID_SEVERITIES:
        return None, f"invalid_severity:{severity[:32]}"
    return Finding(
        path=path, line=line, rule=rule, severity=severity, message=message,  # type: ignore[arg-type]
    ), ""


def _parse_response(
    resp: httpx.Response,
) -> tuple[tuple[Finding, ...], str, str]:
    """Returns (findings, model_name, error). On parse failure returns
    ((), model_name, error_message)."""
    if resp.status_code != 200:
        return (), "", f"http_{resp.status_code}"
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        # Cloudflare HTML interstitial, gateway error page, truncated
        # body — all return 200 + non-JSON. Surface as parse failure
        # instead of crashing the webhook handler.
        return (), "", "envelope_json_decode_failed"
    if not isinstance(body, dict):
        return (), "", "envelope_not_a_dict"
    model_name = body.get("model", "")
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return (), model_name, "missing choices/message/content"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return (), model_name, "llm returned non-json — parse failed"
    raw_findings = parsed.get("findings", [])
    if not isinstance(raw_findings, list):
        return (), model_name, "findings field is not a list"
    coerced: list[Finding] = []
    for raw in raw_findings:
        finding, reason = _coerce_finding(raw)
        if finding is None:
            # Log per-drop with truncated raw so a hostile/hallucinating
            # LLM can't hide a critical finding by surrounding it with
            # malformed noise. `reason` carries the failure class +
            # offending field value so triage is mechanical.
            log.warning(
                "llm_finding_dropped",
                extra={
                    "reason": reason,
                    "model": model_name,
                    # repr() not str() so a partial multibyte char at the
                    # 200-byte boundary becomes `\xNN` rather than an
                    # invalid UTF-8 sequence DD log ingest may reject.
                    "raw_truncated": repr(raw)[:200],
                },
            )
            continue
        coerced.append(finding)
    return tuple(coerced), model_name, ""


def _llmobs_tags(pr_context: Optional[PrContext]) -> dict[str, str]:
    """Build the tag dict for an LLM Obs span from `pr_context`.

    Tags are stringified because DD facet types are inferred from the
    first value seen — keeping all coords as strings prevents schema
    drift if a future call passes an int where another passed a string.
    `head_sha` is truncated to 8 chars so the tag-cardinality budget
    isn't blown by full 40-char hashes.
    """
    if not pr_context:
        return {}
    tags: dict[str, str] = {}
    if "installation_id" in pr_context:
        tags["installation_id"] = str(pr_context["installation_id"])
    if "repo" in pr_context:
        tags["repo"] = str(pr_context["repo"])
    if "pr_number" in pr_context:
        tags["pr_number"] = str(pr_context["pr_number"])
    if "head_sha" in pr_context:
        tags["head_sha"] = str(pr_context["head_sha"])[:_LLMOBS_HEAD_SHA_TAG_LEN]
    return tags


def _extract_usage_metrics(body: Any) -> dict[str, Optional[int]]:
    """Pull token counts from an OpenAI-compat response body. Missing
    `usage` is normal (OpenRouter free-tier omits it sometimes) and
    must not crash the span emission."""
    if not isinstance(body, dict):
        return {"input_tokens": None, "output_tokens": None}
    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None}
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


def review_diff(
    hunks: list[Hunk],
    installation_id: int,
    pr_context: Optional[PrContext] = None,
) -> LlmReviewResponse:
    """Send `hunks` to the round-robin-selected LLM and return findings.

    Returns one of four discriminated states (`response.kind`):
      - `no_diff`: empty hunks short-circuit, no LLM call made.
      - `reviewed`: at least one backend returned a parseable payload.
      - `parse_failed`: LLM responded but the content wasn't usable JSON.
      - `all_failed`: every backend errored or timed out.

    `pr_context` (Optional dict) carries the PR coords for DD LLM Obs
    tags. Keys consumed: installation_id, repo, pr_number, head_sha.
    Omitted ⇒ traces still emit but without filterable PR tags.
    """
    if not hunks:
        return LlmReviewResponse(kind="no_diff")

    primary = select_backend(installation_id)
    secondary = (
        Backend.OPENROUTER if primary == Backend.POOLSIDE else Backend.POOLSIDE
    )
    messages = _build_messages(hunks)
    pr_tags = _llmobs_tags(pr_context)

    last_error = ""
    for backend in (primary, secondary):
        config = _BACKEND_CONFIGS[backend]
        # Open one LLM Obs span per backend attempt. Annotate on every
        # exit path (success + failures) so DD captures latency tails
        # and error rates per-backend.
        start_ns = time.monotonic_ns()
        with _llmobs_llm(
            model_name=config.model,
            model_provider=backend.value,
            name=_LLMOBS_NAME,
        ) as span:
            try:
                resp = _call_backend(config, messages)
            except _BackendConfigError as e:
                log.error(
                    "llm_backend_misconfigured",
                    extra={"backend": backend.value, "detail": str(e)},
                )
                _llmobs_annotate(
                    span=span, input_data=messages,
                    metadata={"backend": backend.value, "error": "config"},
                    metrics={"latency_ms": _elapsed_ms(start_ns)},
                    tags=pr_tags,
                )
                last_error = f"{backend.value} misconfigured: {e}"
                continue
            except (httpx.RequestError, httpx.TimeoutException) as e:
                log.warning(
                    "llm_backend_transport_failed",
                    extra={"backend": backend.value, "kind": type(e).__name__},
                )
                _llmobs_annotate(
                    span=span, input_data=messages,
                    metadata={"backend": backend.value, "error": type(e).__name__},
                    metrics={"latency_ms": _elapsed_ms(start_ns)},
                    tags=pr_tags,
                )
                last_error = f"{backend.value}: {type(e).__name__}"
                continue
            findings, model, err = _parse_response(resp)
            # Annotate AFTER the response is parsed so we capture the
            # raw content + token counts. resp.json() was already
            # consumed inside _parse_response; re-call here for the
            # span input. httpx.Response.json() re-parses every call,
            # but review response bodies are small — re-parse cost is
            # negligible vs the LLM round-trip we just paid.
            try:
                body = resp.json() if resp.status_code == 200 else {}
            except (ValueError, json.JSONDecodeError):
                # The first parse succeeded (otherwise err would be
                # truthy and we'd skip the body fields); a re-parse
                # failure here means httpx cache mutation or a real
                # divergence. Log so DD can alert on the rate —
                # silently emitting `kind=reviewed` with empty
                # content would undercount token cost in dashboards.
                log.warning(
                    "llm_body_reparse_failed",
                    extra={
                        "backend": backend.value,
                        "status_code": resp.status_code,
                    },
                )
                body = {}
            content = ""
            if isinstance(body, dict):
                choices = body.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    content = (choices[0].get("message") or {}).get("content", "")
            usage_metrics = _extract_usage_metrics(body)
            _llmobs_annotate(
                span=span,
                input_data=messages,
                output_data=content or None,
                metadata={
                    "backend": backend.value,
                    "status_code": resp.status_code,
                    "kind": "reviewed" if not err else (
                        "parse_failed" if resp.status_code == 200 else "http_error"
                    ),
                },
                metrics={
                    "latency_ms": _elapsed_ms(start_ns),
                    **usage_metrics,
                },
                tags=pr_tags,
            )
        if not err:
            return LlmReviewResponse(
                kind="reviewed",
                findings=findings,
                backend_used=backend,
                model_name=model,
            )
        if resp.status_code == 200:
            # 200 + parse failure — the LLM returned but we can't use
            # the content. Don't fall back (the other backend would
            # likely produce the same prose). Surface the parse failure
            # directly so the caller can post an advisory check-run.
            log.warning(
                "llm_response_parse_failed",
                extra={"backend": backend.value, "model": model, "error": err},
            )
            return LlmReviewResponse(
                kind="parse_failed",
                backend_used=backend,
                model_name=model,
                error=err,
            )
        log.warning(
            "llm_backend_http_failed",
            extra={"backend": backend.value, "status": resp.status_code, "error": err},
        )
        last_error = f"{backend.value}: {err}"

    return LlmReviewResponse(
        kind="all_failed",
        error=last_error or "both backends failed",
    )
