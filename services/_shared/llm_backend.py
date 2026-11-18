"""Provider identity and request configuration shared by routing and transport."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal, Optional

_TIMEOUT_SECONDS = 60
_RETRY_ATTEMPTS = 3


class Backend(str, Enum):
    """LLM backends. String-valued so DD LLM Obs tags + structured logs
    can `backend=str(backend)` without a cast."""

    POOLSIDE = "poolside"
    OPENROUTER = "openrouter"
    # Owned in-cluster review ensemble (ADR-0009), both fronted by the same
    # spark-gateway (it routes by model name to whichever Spark carries it,
    # warm-first). CAVE = the coder arm (qwen3-coder-next on one GPU host),
    # CAVE_REASONER = the reasoner arm (default below is Laguna, and BOTH
    # deployments override it to `spark:warm-any` - see #843). Deep
    # review runs BOTH and merges - the brain+hands split that is now the
    # PRIMARY review path (POOLSIDE/OPENROUTER above are a bounded overload
    # fallback only - see _saas_overload_fallback_config). The exposed-secret
    # judge (#439) also routes to CAVE.
    CAVE = "cave"
    # A distinct vendor identity (grug#910), NOT a POOLSIDE/OPENROUTER alias -
    # different endpoint, different key, different provenance for telemetry.
    # Sits ahead of Cave in the grug#906/#910 "cloud" priority chain, never
    # in the default ("cave") priority path.
    OPENCODE_GO = "opencode-go"
    CAVE_REASONER = "cave-reasoner"
    # The spark-gateway's free provider pool on its free route (grug#1269):
    # free cloud models only, never a Spark. The last cloud chain tier, ahead
    # of the Cave.
    FREE_POOL = "free-pool"
    # The operator's own subscription plans run headless (Claude Code / Codex)
    # as the last-resort fallback after self-hosted and free tiers fail (grug#1276).
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True, slots=True)
class BackendConfig:
    """All the data a single backend dispatch needs. Replacing per-backend
    if/else branches with a `BackendConfig` lookup means adding a third
    backend is one new entry, not four scattered edits."""

    backend: Backend
    url: str
    model: str
    key_loader: Callable[[], str]
    # Vendor-specific chat-completions body params merged for THIS backend
    # only. Poolside's laguna-s-2.1 runs thinking ON by default - ~87% of output
    # was reasoning tokens, which (a) blew past the 30s read timeout (measured
    # 72s for even a tiny diff -> ReadTimeout -> Elder posted nothing for days)
    # and (b) leaked reasoning prose into `content`, breaking JSON parse. The
    # vLLM `chat_template_kwargs.enable_thinking=false` switch disables it
    # (verified live: 72s-><1s, reasoning_tokens 1106->0). claude/OpenRouter
    # rejects this key, so it MUST be per-backend, never on the shared body.
    extra_body: dict = field(default_factory=dict)
    # Vendor-agnostic outgoing headers, merged before Authorization (which
    # always wins - _call_backend rejects an extra_headers entry named
    # Authorization outright). Today this is only the Cave arms'
    # X-Spark-Priority: interactive - the spark-gateway
    # priority queue that keeps Grug's short-timeout calls from starving
    # behind Hermes's long agentic turns on a shared, single-generation-slot
    # Ollama target. SaaS backends don't look at it; harmless to send
    # everywhere it's set.
    extra_headers: dict = field(default_factory=dict)
    # Headers whose VALUE must be computed per call, not once at config
    # definition time (grug#984). opencode Go's `x-opencode-session`
    # needs a value that is fresh per outgoing request - re-sending the
    # same literal string for the process's entire lifetime would still
    # satisfy the "header present" check that actually gates the 400
    # below, but would defeat the routing/prompt-caching optimization the
    # header exists for (every unrelated PR's review would collide on one
    # cache key) and reads as exactly the kind of static, non-conversation
    # -scoped traffic opencode Go's abuse monitoring is watching for.
    # Callable, same lazy-at-call-time shape as `key_loader` above, for
    # the same reason: a bare value captured here would be frozen at
    # `_BACKEND_CONFIGS` module-load time, not regenerated per call. A
    # callable that returns None leaves its header off that call.
    dynamic_headers: dict[str, Callable[[], str | None]] = field(default_factory=dict)
    timeout_seconds: float = _TIMEOUT_SECONDS
    retry_attempts: int = _RETRY_ATTEMPTS
    # Long review calls should still retry quick 429/503 responses, but must not
    # repeat a full _review_llm_timeout_s() transport timeout. None follows
    # retry_attempts.
    transport_retry_attempts: Optional[int] = None
    # Which HTTP wire protocol this backend speaks. "chat" is the
    # OpenAI chat-completions shape every backend here used until
    # gpt-5.6-luna: `messages` in, `choices[0].message.content` out.
    #
    # "responses" is OpenAI's Responses API, which opencode Go documents as
    # the ONLY endpoint for gpt-5.6-luna, grok-4.6 and muse-spark (its other
    # models are chat-completions or anthropic /v1/messages). It differs in
    # three ways that all matter here: the field is `input` not `messages`,
    # JSON mode is `text.format` not `response_format`, and the reply is an
    # `output[]` list of typed blocks rather than `choices[]`.
    #
    # Sending the chat shape to a responses-only model is not a graceful
    # 400 - measured 2026-08-31, gpt-5.6-luna returns HTTP 500 "Internal
    # server error" on BOTH /v1/chat/completions and /v1/messages, for every
    # request variant tried (max_tokens, max_completion_tokens, no token
    # param, reasoning_effort). Six other models answered 200 on the
    # identical request with the identical key, so a 500 here reads like a
    # provider outage rather than the routing mistake it actually is.
    wire: Literal["chat", "responses"] = "chat"
