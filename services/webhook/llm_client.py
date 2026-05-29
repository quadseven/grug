# MIRRORED — sibling at services/api/llm_client.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""LLM client abstraction for the Code-Reviewer (Elder) persona.

Sends review prompts to either Poolside (Laguna) or OpenRouter and
returns a structured response. Backend selection is round-robin via
`installation_id % 2` so traffic splits evenly across the two and DD
LLM Obs can A/B prompt variants. If the primary backend errors hard
(post-retry), the other backend is tried before surfacing an empty
response — the caller posts an advisory check-run rather than 500ing
the webhook handler on transient LLM failures.

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
from typing import Any, Optional

import httpx

from secrets_loader import get_openrouter_api_key, get_poolside_api_key

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.llm_client")

# Per-backend endpoints + default models.
_POOLSIDE_URL = "https://inference.poolside.ai/v1/chat/completions"
_POOLSIDE_MODEL = "poolside/laguna-m.1"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"

_TIMEOUT_SECONDS = 30
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.5  # exponential: 0.5s, 1.0s, 2.0s


class Backend(str, Enum):
    """LLM backends. String-valued so DD LLM Obs tags + structured logs
    can `backend=str(backend)` without a cast."""

    POOLSIDE = "poolside"
    OPENROUTER = "openrouter"


@dataclass(frozen=True, slots=True)
class Hunk:
    """One diff hunk presented to the model as a single review unit."""

    path: str
    body: str


@dataclass(frozen=True, slots=True)
class LlmReviewResponse:
    """Result of one review_diff call.

    `backend_used=None` + non-empty `error` indicates total failure —
    the caller surfaces an advisory check-run that says "skipped".
    Successful calls always carry a backend + model attribution so DD
    LLM Obs can correlate findings to backend.
    """

    findings: list[dict[str, Any]] = field(default_factory=list)
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


def select_backend(installation_id: int) -> Backend:
    """Round-robin via `installation_id % 2`.

    Stable per-install — two PRs on the same install always hit the
    same backend, which lets DD LLM Obs compare prompt variants without
    cross-install noise.
    """
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


def _call_backend(
    backend: Backend, messages: list[dict[str, str]]
) -> httpx.Response:
    """Single backend call with 429 retry + backoff. Raises httpx errors
    on the LAST attempt — caller catches and falls back."""
    if backend == Backend.POOLSIDE:
        url, model, key = _POOLSIDE_URL, _POOLSIDE_MODEL, _load_poolside_key()
    else:
        url, model, key = _OPENROUTER_URL, _OPENROUTER_MODEL, _load_openrouter_key()

    body = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {key}"}

    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=_TIMEOUT_SECONDS)
        except (httpx.RequestError, httpx.HTTPError) as e:
            last_exc = e
            if attempt < _RETRY_ATTEMPTS - 1:
                _RETRY_SLEEP(_RETRY_BASE_DELAY * (2 ** attempt))
                continue
            raise
        if resp.status_code == 429 and attempt < _RETRY_ATTEMPTS - 1:
            _RETRY_SLEEP(_RETRY_BASE_DELAY * (2 ** attempt))
            continue
        return resp
    # Loop terminated without returning — propagate the last seen error
    # so the caller can fall back. Defense-in-depth; the loop body
    # already returns or raises in every branch.
    if last_exc:
        raise last_exc
    raise RuntimeError("retry loop exited without producing a response")


def _parse_response(resp: httpx.Response) -> tuple[list[dict[str, Any]], str, str]:
    """Returns (findings, model_name, error). On parse failure returns
    ([], model_name, error_message)."""
    if resp.status_code != 200:
        return [], "", f"http_{resp.status_code}"
    body = resp.json()
    model_name = body.get("model", "")
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return [], model_name, "missing choices/message/content"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return [], model_name, "llm returned non-json — parse failed"
    findings = parsed.get("findings", [])
    if not isinstance(findings, list):
        return [], model_name, "findings field is not a list"
    return findings, model_name, ""


def review_diff(
    hunks: list[Hunk], installation_id: int
) -> LlmReviewResponse:
    """Send `hunks` to the round-robin-selected LLM and return findings.

    Fallback: if the primary backend fails (post-retry), tries the
    other backend before giving up. Total failure surfaces as
    `backend_used=None` + non-empty `error`.
    """
    if not hunks:
        return LlmReviewResponse()

    primary = select_backend(installation_id)
    secondary = (
        Backend.OPENROUTER if primary == Backend.POOLSIDE else Backend.POOLSIDE
    )
    messages = _build_messages(hunks)

    for backend in (primary, secondary):
        try:
            resp = _call_backend(backend, messages)
        except (httpx.RequestError, httpx.HTTPError) as e:
            log.warning(
                "llm_backend_transport_failed",
                extra={"backend": backend.value, "kind": type(e).__name__},
            )
            continue
        findings, model, err = _parse_response(resp)
        if not err:
            return LlmReviewResponse(
                findings=findings,
                backend_used=backend,
                model_name=model,
            )
        if resp.status_code == 200:
            # 200 + parse failure — the LLM returned but we can't use
            # the content. Don't fall back (the other backend would
            # likely produce the same prose). Return what we have.
            log.warning(
                "llm_response_parse_failed",
                extra={"backend": backend.value, "model": model, "error": err},
            )
            return LlmReviewResponse(
                backend_used=backend, model_name=model, error=err,
            )
        log.warning(
            "llm_backend_http_failed",
            extra={"backend": backend.value, "status": resp.status_code, "error": err},
        )

    return LlmReviewResponse(error="both backends failed")
