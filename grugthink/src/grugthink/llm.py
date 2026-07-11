"""grugthink v2 LLM engine - spark-gateway native (owned Sparks).

A single OpenAI-compatible client for BOTH chat and embeddings against the
in-cluster spark-gateway (ADR-0009), replacing the old google-generativeai
(Gemini) chat path AND the sentence-transformers/torch embedding path. No SaaS
keys, no heavy ML deps - the gateway is the one owned LLM surface.

Endpoints (OpenAI-compatible, how grug's Elder already calls the gateway):
  POST {base}/v1/chat/completions   {model, messages, ...}
  POST {base}/v1/embeddings         {model, input}
Unauthenticated in-cluster.

Config (env):
  SPARK_GATEWAY_URL / GRUGTHINK_LLM_URL - gateway base URL. Falls back to the
    first OLLAMA_URLS entry, then the in-cluster default.
  GRUGTHINK_LLM_MODEL   - chat model (default qwen3-coder-next:q8_0).
  GRUGTHINK_EMBED_MODEL - embedding model (default nomic-embed-text:v1.5).
  GRUGTHINK_LLM_TIMEOUT - per-request seconds (default 60).
"""
from __future__ import annotations

import os
from typing import Iterable, Sequence

import httpx

_DEFAULT_GATEWAY = "http://spark-gateway.spark-gateway.svc:8080"


class LLMError(RuntimeError):
    """A spark-gateway chat/embedding call failed. Callers degrade gracefully
    (a Discord reply falls back to a canned line; memory falls back to keyword
    search) rather than crashing the bot."""


def base_url() -> str:
    url = (
        os.getenv("SPARK_GATEWAY_URL")
        or os.getenv("GRUGTHINK_LLM_URL")
        or (os.getenv("OLLAMA_URLS", "").split(",")[0].strip() if os.getenv("OLLAMA_URLS") else "")
        or _DEFAULT_GATEWAY
    )
    return url.rstrip("/")


def chat_model() -> str:
    # Pinned Q8_0 tag (was ":latest"): the exact model srv-sparkles serves.
    return os.getenv("GRUGTHINK_LLM_MODEL", "qwen3-coder-next:q8_0")


def embed_model() -> str:
    # Pinned version (was bare "nomic-embed-text").
    return os.getenv("GRUGTHINK_EMBED_MODEL", "nomic-embed-text:v1.5")


def _timeout() -> float:
    try:
        return float(os.getenv("GRUGTHINK_LLM_TIMEOUT", "60"))
    except ValueError:
        return 60.0


async def chat(
    messages: Sequence[dict],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
) -> str:
    """One-shot chat completion via the spark-gateway. `messages` is the
    OpenAI shape: [{"role": "system"|"user"|"assistant", "content": str}].
    Returns the assistant text; raises LLMError on any transport/parse failure."""
    body: dict = {
        "model": model or chat_model(),
        "messages": list(messages),
        "stream": False,
        "temperature": temperature,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.post(f"{base_url()}/v1/chat/completions", json=body)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        raise LLMError(f"spark-gateway chat failed: {type(e).__name__}: {e}") from e


def embed(texts: str | Iterable[str], *, model: str | None = None):
    """Embeddings via the spark-gateway. Accepts a single string (returns one
    vector) or an iterable (returns a list of vectors). Raises LLMError on
    failure so the vector store can degrade to keyword search."""
    single = isinstance(texts, str)
    inputs = [texts] if single else list(texts)
    body = {"model": model or embed_model(), "input": inputs}
    try:
        with httpx.Client(timeout=_timeout()) as client:
            resp = client.post(f"{base_url()}/v1/embeddings", json=body)
            resp.raise_for_status()
            vectors = [row["embedding"] for row in resp.json()["data"]]
        return vectors[0] if single else vectors
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        raise LLMError(f"spark-gateway embeddings failed: {type(e).__name__}: {e}") from e


def health() -> bool:
    """Best-effort readiness probe of the gateway (used by /health and startup
    logs). Never raises."""
    try:
        with httpx.Client(timeout=5.0) as client:
            return client.get(f"{base_url()}/v1/models").status_code == 200
    except httpx.HTTPError:
        return False
