"""grugthink v2 LLM engine (spark-gateway native) tests."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import grugthink.llm as llm


class _AsyncResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "Grug say hi."}}]}


class _AsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _AsyncClient.last_url = url
        _AsyncClient.last_body = json
        return _AsyncResp()


class _EmbedResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}


class _EmbedClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        _EmbedClient.last_url = url
        return _EmbedResp()


def test_chat_hits_openai_endpoint_and_returns_text(monkeypatch):
    monkeypatch.setenv("SPARK_GATEWAY_URL", "http://spark-gateway.spark-gateway.svc:8080")
    with patch("httpx.AsyncClient", lambda *a, **k: _AsyncClient()):
        out = asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert out == "Grug say hi."
    assert _AsyncClient.last_url.endswith("/v1/chat/completions")
    assert _AsyncClient.last_body["stream"] is False


def test_embed_single_and_batch(monkeypatch):
    monkeypatch.setenv("SPARK_GATEWAY_URL", "http://gw:8080")
    with patch("httpx.Client", lambda *a, **k: _EmbedClient()):
        one = llm.embed("cave")
        many = llm.embed(["cave", "fire"])
    assert one == [0.1, 0.2]  # single string -> one vector
    assert many == [[0.1, 0.2], [0.3, 0.4]]  # iterable -> list of vectors
    assert _EmbedClient.last_url.endswith("/v1/embeddings")


def test_base_url_prefers_gateway_then_ollama_then_default(monkeypatch):
    monkeypatch.delenv("SPARK_GATEWAY_URL", raising=False)
    monkeypatch.delenv("GRUGTHINK_LLM_URL", raising=False)
    monkeypatch.setenv("OLLAMA_URLS", "http://a:8080,http://b:8080")
    assert llm.base_url() == "http://a:8080"  # first OLLAMA_URLS entry
    monkeypatch.delenv("OLLAMA_URLS", raising=False)
    assert "spark-gateway" in llm.base_url()  # in-cluster default


def test_chat_wraps_transport_errors_in_llmerror(monkeypatch):
    import httpx

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("gateway down")

    with patch("httpx.AsyncClient", lambda *a, **k: _Boom()):
        try:
            asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
            assert False, "expected LLMError"
        except llm.LLMError as e:
            assert "chat failed" in str(e)
