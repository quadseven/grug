#!/usr/bin/env python3
"""
Ollama Embedder - Offload embedding generation to Ollama server

Instead of loading sentence-transformers locally (67MB+ RAM), this uses
the Ollama API to generate embeddings on a remote GPU server.

Benefits:
- Saves 50-70MB RAM in bot container
- Faster embedding generation (GPU vs CPU)
- No model loading time on bot startup
- Centralizes ML infrastructure
"""

from typing import List, Union
from urllib.parse import urlparse

import numpy as np
import requests

from ..grug_structured_logger import get_logger

log = get_logger(__name__)

_ALLOWED_SCHEMES = ("http", "https")


def _is_valid_ollama_url(url: str) -> bool:
    """Minimal scheme/host check to guard against SSRF via unvalidated URLs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in _ALLOWED_SCHEMES and bool(parsed.hostname)


class OllamaEmbedder:
    """Generate embeddings using Ollama API instead of local SentenceTransformer."""

    def __init__(
        self,
        ollama_url: str,
        model: str = "nomic-embed-text",
        dimension: int = 768,
        timeout: int = 30,
    ):
        """
        Initialize Ollama embedder.

        Args:
            ollama_url: Base URL for Ollama API (e.g., "http://localhost:11434")
            model: Embedding model to use (default: nomic-embed-text)
            dimension: Embedding dimension (768 for nomic-embed-text, 384 for all-minilm)
            timeout: Request timeout in seconds
        """
        if not _is_valid_ollama_url(ollama_url):
            raise ValueError(f"Invalid Ollama URL: {ollama_url!r}")

        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.dimension = dimension
        self.timeout = timeout

        log.info(
            "OllamaEmbedder initialized",
            extra={"ollama_url": ollama_url, "model": model, "dimension": dimension},
        )

    def encode(self, texts: Union[str, List[str]], batch_size: int = 32, show_progress_bar: bool = False) -> np.ndarray:
        """
        Generate embeddings for text(s).

        Args:
            texts: Single text string or list of text strings
            batch_size: Ignored (kept for API compatibility with SentenceTransformer)
            show_progress_bar: Ignored (kept for API compatibility)

        Returns:
            NumPy array of embeddings, shape (n, dimension)
        """
        # Handle single string input
        if isinstance(texts, str):
            texts = [texts]

        if not texts:
            return np.array([])

        embeddings = []
        failed_count = 0

        for text in texts:
            try:
                response = requests.post(
                    f"{self.ollama_url}/api/embeddings",
                    json={
                        "model": self.model,
                        "prompt": text,
                        # Keep the (tiny, ~270MB) embed model resident
                        # indefinitely instead of Ollama's default 5-minute
                        # eviction - it shares the box with a permanently
                        # resident 84GB chat model with ~37GB of headroom to
                        # spare, so there is no memory pressure trade-off,
                        # only a repeated cold-load tax to avoid.
                        "keep_alive": -1,
                    },
                    # quadseven/infra#1768/#1770/#1773: realtime priority so
                    # this queues ahead of Grug's own review calls and
                    # Hermes's batch turns on the shared gateway target
                    # (matches bot/llm_clients.py's chat call); X-Spark-
                    # Caller identifies this consumer in the gateway's
                    # metrics instead of an anonymous python client. Harmless
                    # if ollama_url ever points straight at a Spark instead
                    # of the gateway - Ollama ignores unknown headers.
                    headers={"X-Spark-Priority": "realtime", "X-Spark-Caller": "grugthink-embed"},
                    timeout=self.timeout,
                )

                if response.status_code == 200:
                    embedding = response.json().get("embedding")
                    if embedding:
                        embeddings.append(embedding)
                    else:
                        log.warning(
                            "Empty embedding returned",
                            extra={"text_preview": text[:100], "response": response.text},
                        )
                        # Use zero vector as fallback
                        embeddings.append([0.0] * self.dimension)
                        failed_count += 1
                else:
                    log.error(
                        "Ollama embedding request failed",
                        extra={
                            "status_code": response.status_code,
                            "response": response.text,
                            "text_preview": text[:100],
                        },
                    )
                    # Use zero vector as fallback
                    embeddings.append([0.0] * self.dimension)
                    failed_count += 1

            except requests.exceptions.Timeout:
                log.error(
                    "Ollama embedding request timed out",
                    extra={"timeout": self.timeout, "text_preview": text[:100]},
                )
                embeddings.append([0.0] * self.dimension)
                failed_count += 1

            except Exception as e:
                log.error(
                    "Error generating embedding",
                    extra={"error": str(e), "text_preview": text[:100]},
                )
                embeddings.append([0.0] * self.dimension)
                failed_count += 1

        if failed_count > 0:
            failure_rate = f"{failed_count / len(texts) * 100:.1f}%"
            log.warning(
                "Some embeddings failed to generate",
                extra={"failed": failed_count, "total": len(texts), "failure_rate": failure_rate},
            )

        result = np.array(embeddings, dtype=np.float32)

        log.debug(
            "Generated embeddings",
            extra={"count": len(texts), "shape": result.shape, "failed": failed_count},
        )

        return result

    def test_connection(self) -> bool:
        """
        Test connection to Ollama server and verify model is available.

        Returns:
            True if connection successful and model available, False otherwise
        """
        try:
            # Test basic connectivity. Use the full request timeout (not a short
            # hardcoded 5s): the in-cluster spark-gateway aggregates backends and
            # /api/tags can exceed 5s under load. A transient tags timeout here
            # marked the embedder dead -> fell back to SentenceTransformer (absent
            # in the light image) -> vector search silently disabled. The real
            # /api/embeddings call already uses self.timeout and works fine.
            response = requests.get(f"{self.ollama_url}/api/tags", timeout=self.timeout)
            if response.status_code != 200:
                log.error(
                    "Ollama server not reachable",
                    extra={"url": self.ollama_url, "status": response.status_code},
                )
                return False

            # Check if embedding model is available
            models = response.json().get("models", [])
            model_names = [m.get("name", "") for m in models]

            if self.model not in model_names:
                log.warning(
                    "Embedding model not found on Ollama server",
                    extra={
                        "model": self.model,
                        "available_models": model_names,
                        "suggestion": f"Run: ollama pull {self.model}",
                    },
                )
                return False

            log.info(
                "Ollama embedder connection test successful",
                extra={"model": self.model, "server": self.ollama_url},
            )
            return True

        except Exception as e:
            log.error("Ollama connection test failed", extra={"error": str(e), "url": self.ollama_url})
            return False
