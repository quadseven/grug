"""Tests for OllamaEmbedder.

Focus: the connectivity self-test must honor the configured request timeout.
A previously hardcoded 5s timeout on the /api/tags probe caused a transient
in-cluster latency spike to mark the embedder dead, falling back to a local
SentenceTransformer that the light image does not ship -> semantic search
silently disabled. These tests pin the "every outbound request honors the
configured timeout" contract so that regression cannot recur.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.grugthink.embedders.ollama_embedder import OllamaEmbedder, _is_valid_ollama_url


class TestOllamaEmbedderTimeout:
    """The configured timeout must be forwarded to every outbound request."""

    def test_test_connection_forwards_configured_timeout(self):
        """/api/tags probe uses self.timeout, not a hardcoded value."""
        embedder = OllamaEmbedder("http://gateway:11434", model="nomic-embed-text:v1.5", timeout=30)

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"models": [{"name": "nomic-embed-text:v1.5"}]}

        with patch(
            "src.grugthink.embedders.ollama_embedder.requests.get", return_value=resp
        ) as mock_get:
            assert embedder.test_connection() is True

        # The exact configured timeout must reach the probe -- guards against a
        # future hardcoded value re-disabling the embedder fallback path.
        _, kwargs = mock_get.call_args
        assert kwargs["timeout"] == 30

    def test_test_connection_forwards_non_default_timeout(self):
        """A non-default timeout is forwarded verbatim, not clamped or ignored."""
        embedder = OllamaEmbedder("http://gateway:11434", model="nomic-embed-text:v1.5", timeout=12)

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"models": [{"name": "nomic-embed-text:v1.5"}]}

        with patch(
            "src.grugthink.embedders.ollama_embedder.requests.get", return_value=resp
        ) as mock_get:
            embedder.test_connection()

        _, kwargs = mock_get.call_args
        assert kwargs["timeout"] == 12

    def test_encode_forwards_configured_timeout(self):
        """The embedding request path also honors the configured timeout."""
        embedder = OllamaEmbedder("http://gateway:11434", timeout=30, dimension=768)

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"embedding": [0.0] * 768}

        with patch(
            "src.grugthink.embedders.ollama_embedder.requests.post", return_value=resp
        ) as mock_post:
            embedder.encode("Ugga is Grug wife")

        _, kwargs = mock_post.call_args
        assert kwargs["timeout"] == 30


class TestOllamaEmbedderPriorityAndKeepAlive:
    """githumps/infra#1768/#1770/#1773 - live incident 2026-07-13: embedding
    calls sent no priority header (defaulted to batch) and no keep_alive, so
    they both queued behind unrelated chat generations on the shared gateway
    target AND paid a repeated cold-load tax. Both fixed at the request
    level; verified here rather than via a live gateway."""

    def test_encode_sends_realtime_priority_and_caller_headers(self):
        embedder = OllamaEmbedder("http://gateway:11434", dimension=768)

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"embedding": [0.0] * 768}

        with patch(
            "src.grugthink.embedders.ollama_embedder.requests.post", return_value=resp
        ) as mock_post:
            embedder.encode("Ugga is Grug wife")

        _, kwargs = mock_post.call_args
        assert kwargs["headers"] == {
            "X-Spark-Priority": "realtime",
            "X-Spark-Caller": "grugthink-embed",
        }

    def test_encode_pins_keep_alive_indefinite(self):
        embedder = OllamaEmbedder("http://gateway:11434", model="nomic-embed-text:v1.5", dimension=768)

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"embedding": [0.0] * 768}

        with patch(
            "src.grugthink.embedders.ollama_embedder.requests.post", return_value=resp
        ) as mock_post:
            embedder.encode("Ugga is Grug wife")

        _, kwargs = mock_post.call_args
        assert kwargs["json"]["keep_alive"] == -1
        assert kwargs["json"]["model"] == "nomic-embed-text:v1.5"


class TestOllamaEmbedderUrlValidation:
    """URL scheme/host guard (SSRF hardening) stays intact."""

    @pytest.mark.parametrize("url", ["http://gateway:11434", "https://gateway.example"])
    def test_valid_urls(self, url):
        assert _is_valid_ollama_url(url) is True

    @pytest.mark.parametrize("url", ["ftp://gateway", "not-a-url", "", "file:///etc/passwd"])
    def test_invalid_urls_rejected(self, url):
        assert _is_valid_ollama_url(url) is False

    def test_constructor_rejects_invalid_url(self):
        with pytest.raises(ValueError):
            OllamaEmbedder("ftp://gateway")
