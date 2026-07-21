"""Tests for the bounded Ollama/Cave -> Poolside -> OpenRouter -> Gemini
fallback chain (query_model in bot/prompts.py, backed by the per-backend
clients in bot/llm_clients.py).

Coverage required by the incident this closes (grugthink chat had NO
fallback at all - a Cave/spark-gateway failure just returned None):
  - fallback engages on primary (Ollama) failure
  - fallback does NOT engage on primary success
  - fallback tries Poolside then OpenRouter, in that order
  - both-SaaS-fail-cleanly (no exception, no crash, None returned)
  - the Gemini bonus tier engages last, gated on GEMINI_API_KEY
  - each tier is single-shot: exactly one HTTP call per backend, no retries
"""

import os

# config_legacy.py raises at IMPORT time if DISCORD_TOKEN is unset (module
# import happens at pytest COLLECTION time, before the setup_test_environment
# autouse fixture in src/grugthink/conftest.py has a chance to run) - same
# guard test_bot_commands.py uses ahead of its own module-level bot import.
os.environ.setdefault("DISCORD_TOKEN", "fake_token")

from unittest.mock import MagicMock, patch  # noqa: E402

import requests  # noqa: E402

from src.grugthink.bot import llm_clients as llm_clients_module  # noqa: E402
from src.grugthink.bot import prompts as prompts_module  # noqa: E402


class _FakeResponse:
    """Minimal requests.Response stand-in - both query_ollama_api (reads
    response["response"]) and the OpenAI-compatible fallback clients (read
    response["choices"][0]["message"]["content"]) are exercised via this."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self) -> dict:
        return self._json_data


def _ollama_ok(text: str) -> _FakeResponse:
    return _FakeResponse(200, {"response": text})


def _chat_completion_ok(text: str) -> _FakeResponse:
    return _FakeResponse(200, {"choices": [{"message": {"content": text}}]})


def _make_router(*, ollama=None, poolside=None, openrouter=None):
    """Builds a `session.post` replacement that dispatches by URL and
    records every call - lets one test drive a multi-tier fallback chain
    with per-backend canned behavior (a response, or a raised exception)."""
    call_log: list[dict] = []

    def _post(url, json=None, headers=None, timeout=None, **kwargs):
        call_log.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if "/api/generate" in url or "localhost:11434/v1/chat/completions" in url:
            handler = ollama
        elif "poolside.ai" in url:
            handler = poolside
        elif "openrouter.ai" in url:
            handler = openrouter
        else:
            raise AssertionError(f"unexpected URL in fallback chain test: {url}")
        if handler is None:
            raise AssertionError(f"no handler configured for {url}")
        result = handler()
        if isinstance(result, Exception):
            raise result
        return result

    _post.call_log = call_log
    return _post


def _mock_config(**overrides):
    cfg = MagicMock()
    cfg.OLLAMA_URLS = ["http://localhost:11434"]
    cfg.OLLAMA_MODELS = ["llama2"]
    cfg.POOLSIDE_API_KEY = "fake-poolside-key"
    cfg.OPENROUTER_API_KEY = "fake-openrouter-key"
    cfg.GEMINI_API_KEY = None
    cfg.USE_GEMINI = False
    cfg.GEMINI_MODEL = "gemini-pro"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_personality_engine():
    pe = MagicMock()
    personality = MagicMock()
    personality.chosen_name = None
    personality.name = "Grug"
    personality.base_context = "You are Grug, a caveman."
    personality.response_style = "caveman"
    pe.get_personality.return_value = personality
    pe.evolve_personality = MagicMock()
    return pe


def _make_server_db():
    db = MagicMock()
    db.search_facts.return_value = []
    return db


def _run_query_model(monkeypatch, router, mock_config, statement="The sky is blue today"):
    """Drives the real query_model dispatch end-to-end with the LLM
    transport and config mocked, and validate_and_process_response
    stubbed to a passthrough so the test asserts on WHICH backend
    answered (via distinct response text) rather than prompt-format
    validation rules (covered elsewhere)."""
    monkeypatch.setattr(llm_clients_module.session, "post", router)
    with (
        patch("src.grugthink.bot.llm_clients.config", mock_config),
        patch("src.grugthink.bot.prompts.config", mock_config),
        patch("src.grugthink.bot.prompts.validate_and_process_response", side_effect=lambda r, *a, **k: r or None),
    ):
        return prompts_module.query_model(
            statement, _make_server_db(), "server1", _make_personality_engine(), current_bot_id="test_bot"
        )


class TestFallbackEngagement:
    def test_primary_can_use_openai_compatible_gateway(self, monkeypatch):
        monkeypatch.setenv("GRUGTHINK_LLM_API", "openai")
        router = _make_router(ollama=lambda: _chat_completion_ok("LAGUNA_REPLY"))
        result = _run_query_model(
            monkeypatch,
            router,
            _mock_config(OLLAMA_MODELS=["poolside/Laguna-S-2.1-NVFP4"]),
        )

        assert result == "LAGUNA_REPLY"
        call = router.call_log[0]
        assert call["url"] == "http://localhost:11434/v1/chat/completions"
        assert call["json"]["messages"][0]["content"]
        assert call["json"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_fallback_not_engaged_on_primary_success(self, monkeypatch):
        """Ollama/Cave answering must short-circuit the whole chain - the
        fallback must NEVER be called on a genuine success."""
        router = _make_router(ollama=lambda: _ollama_ok("OLLAMA_REPLY"))
        result = _run_query_model(monkeypatch, router, _mock_config())

        assert result == "OLLAMA_REPLY"
        urls_called = [c["url"] for c in router.call_log]
        assert urls_called == ["http://localhost:11434/api/generate"]

    def test_fallback_engages_on_primary_timeout(self, monkeypatch):
        """A primary transport timeout (not just an HTTP error) must fall
        through to Poolside."""
        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("gateway queue-wait timed out"),
            poolside=lambda: _chat_completion_ok("POOLSIDE_REPLY"),
        )
        result = _run_query_model(monkeypatch, router, _mock_config())

        assert result == "POOLSIDE_REPLY"
        urls_called = [c["url"] for c in router.call_log]
        assert urls_called == [
            "http://localhost:11434/api/generate",
            "https://inference.poolside.ai/v1/chat/completions",
        ]

    def test_fallback_engages_on_primary_connection_error(self, monkeypatch):
        """A hard connection failure (spark-gateway pod unreachable) must
        also fall through - not just HTTP-level errors."""
        router = _make_router(
            ollama=lambda: requests.exceptions.ConnectionError("connection refused"),
            poolside=lambda: _chat_completion_ok("POOLSIDE_REPLY"),
        )
        result = _run_query_model(monkeypatch, router, _mock_config())
        assert result == "POOLSIDE_REPLY"

    def test_fallback_engages_on_primary_empty_response(self, monkeypatch):
        """A 200 with an empty/unparseable body (e.g. done_reason=length,
        no <END> ever produced) must also count as failure, not success -
        it has no legitimate error to log but must still fall through."""
        router = _make_router(
            ollama=lambda: _ollama_ok(""),
            poolside=lambda: _chat_completion_ok("POOLSIDE_REPLY"),
        )
        result = _run_query_model(monkeypatch, router, _mock_config())
        assert result == "POOLSIDE_REPLY"


class TestFallbackOrder:
    def test_tries_poolside_then_openrouter_in_order(self, monkeypatch):
        """Poolside failing must fall through to OpenRouter, not skip
        straight to Gemini or give up."""
        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("primary down"),
            poolside=lambda: _FakeResponse(503, {}),
            openrouter=lambda: _chat_completion_ok("OPENROUTER_REPLY"),
        )
        result = _run_query_model(monkeypatch, router, _mock_config())

        assert result == "OPENROUTER_REPLY"
        urls_called = [c["url"] for c in router.call_log]
        assert urls_called == [
            "http://localhost:11434/api/generate",
            "https://inference.poolside.ai/v1/chat/completions",
            "https://openrouter.ai/api/v1/chat/completions",
        ]

    def test_poolside_disables_thinking_mode(self, monkeypatch):
        """Regression guard for the exact incident grug's Elder hit on this
        same backend: laguna-m.1 defaults to thinking ON, which blows the
        short fallback timeout. The request body must carry
        chat_template_kwargs.enable_thinking=false."""
        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("primary down"),
            poolside=lambda: _chat_completion_ok("POOLSIDE_REPLY"),
        )
        _run_query_model(monkeypatch, router, _mock_config())

        poolside_call = next(c for c in router.call_log if "poolside" in c["url"])
        assert poolside_call["json"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_openrouter_uses_fast_haiku_model_not_review_config(self, monkeypatch):
        """Must NOT reuse grug Elder's Opus-plus-high-reasoning OpenRouter
        config - that is a multi-minute review config, unsuited to a
        realtime chat reply."""
        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("primary down"),
            poolside=lambda: _FakeResponse(500, {}),
            openrouter=lambda: _chat_completion_ok("OPENROUTER_REPLY"),
        )
        _run_query_model(monkeypatch, router, _mock_config())

        openrouter_call = next(c for c in router.call_log if "openrouter" in c["url"])
        assert openrouter_call["json"]["model"] == "anthropic/claude-haiku-4.5"
        assert "reasoning" not in openrouter_call["json"]


class TestAllBackendsFail:
    def test_all_fail_cleanly_returns_none(self, monkeypatch):
        """Every tier failing must return None, not raise - the caller
        (bot.py) reads None as its canned 'having trouble' reply."""
        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("primary down"),
            poolside=lambda: requests.exceptions.ConnectionError("poolside unreachable"),
            openrouter=lambda: _FakeResponse(500, {}),
        )
        result = _run_query_model(monkeypatch, router, _mock_config())
        assert result is None

    def test_all_fail_is_single_shot_no_retries(self, monkeypatch):
        """Exactly one HTTP call per backend across the whole failed chain -
        no retry loop, no re-racing a backend that already said no."""
        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("primary down"),
            poolside=lambda: requests.exceptions.Timeout("poolside down too"),
            openrouter=lambda: requests.exceptions.Timeout("openrouter down too"),
        )
        _run_query_model(monkeypatch, router, _mock_config())

        assert len(router.call_log) == 3
        urls_called = [c["url"] for c in router.call_log]
        assert urls_called == [
            "http://localhost:11434/api/generate",
            "https://inference.poolside.ai/v1/chat/completions",
            "https://openrouter.ai/api/v1/chat/completions",
        ]

    def test_missing_api_key_skips_tier_without_a_network_call(self, monkeypatch):
        """An unconfigured fallback tier must be skipped cleanly (no doomed
        request with an empty Authorization header) rather than attempted
        and failed."""
        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("primary down"),
            openrouter=lambda: _chat_completion_ok("OPENROUTER_REPLY"),
        )
        cfg = _mock_config(POOLSIDE_API_KEY=None)
        result = _run_query_model(monkeypatch, router, cfg)

        assert result == "OPENROUTER_REPLY"
        urls_called = [c["url"] for c in router.call_log]
        # Poolside never called at all - straight from Ollama to OpenRouter.
        assert urls_called == [
            "http://localhost:11434/api/generate",
            "https://openrouter.ai/api/v1/chat/completions",
        ]


class TestGeminiBonusTier:
    def test_gemini_engages_as_final_tier_when_configured(self, monkeypatch, mock_gemini_module):
        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("primary down"),
            poolside=lambda: requests.exceptions.ConnectionError("poolside down"),
            openrouter=lambda: _FakeResponse(500, {}),
        )
        mock_gemini_module["model_instance"]._generate_response = lambda prompt: "GEMINI_REPLY"
        cfg = _mock_config(GEMINI_API_KEY="fake-gemini-key", USE_GEMINI=True)

        result = _run_query_model(monkeypatch, router, cfg)

        assert result == "GEMINI_REPLY"
        assert len(router.call_log) == 3  # Ollama + Poolside + OpenRouter, all before Gemini

    def test_gemini_not_engaged_when_not_configured(self, monkeypatch):
        """USE_GEMINI is derived from GEMINI_API_KEY presence - unset means
        the bonus tier is skipped entirely (no doomed request), same as any
        other unconfigured fallback tier."""
        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("primary down"),
            poolside=lambda: requests.exceptions.ConnectionError("poolside down"),
            openrouter=lambda: _FakeResponse(500, {}),
        )
        cfg = _mock_config(GEMINI_API_KEY=None, USE_GEMINI=False)

        result = _run_query_model(monkeypatch, router, cfg)
        assert result is None

    def test_gemini_not_engaged_when_ollama_succeeds(self, monkeypatch, mock_gemini_module):
        """Even with GEMINI_API_KEY configured, a successful primary must
        short-circuit before Gemini (or any fallback tier) is ever touched."""
        router = _make_router(ollama=lambda: _ollama_ok("OLLAMA_REPLY"))
        cfg = _mock_config(GEMINI_API_KEY="fake-gemini-key", USE_GEMINI=True)

        result = _run_query_model(monkeypatch, router, cfg)

        assert result == "OLLAMA_REPLY"
        assert len(router.call_log) == 1


class TestPerBackendClientUnit:
    """Narrower unit tests directly against query_poolside_api /
    query_openrouter_api, independent of the full query_model chain."""

    def test_query_poolside_api_not_configured_returns_none_without_call(self, monkeypatch):
        router = _make_router()
        monkeypatch.setattr(llm_clients_module.session, "post", router)
        cfg = _mock_config(POOLSIDE_API_KEY=None)

        with patch("src.grugthink.bot.llm_clients.config", cfg):
            result = llm_clients_module.query_poolside_api("hello", "cache_key", bot_id="test_bot")

        assert result is None
        assert router.call_log == []

    def test_query_openrouter_api_success(self, monkeypatch):
        router = _make_router(openrouter=lambda: _chat_completion_ok("TRUE - openrouter said so <END>"))
        monkeypatch.setattr(llm_clients_module.session, "post", router)
        cfg = _mock_config()

        with (
            patch("src.grugthink.bot.llm_clients.config", cfg),
            patch(
                "src.grugthink.bot.prompts.validate_and_process_response",
                side_effect=lambda r, *a, **k: r,
            ),
        ):
            result = llm_clients_module.query_openrouter_api("hello", "cache_key", bot_id="test_bot")

        assert result == "TRUE - openrouter said so <END>"

    def test_query_poolside_api_timeout_returns_none(self, monkeypatch):
        router = _make_router(poolside=lambda: requests.exceptions.Timeout("slow"))
        monkeypatch.setattr(llm_clients_module.session, "post", router)
        cfg = _mock_config()

        with patch("src.grugthink.bot.llm_clients.config", cfg):
            result = llm_clients_module.query_poolside_api("hello", "cache_key", bot_id="test_bot")

        assert result is None
        assert len(router.call_log) == 1  # single-shot: no retry after the timeout

    def test_fallback_timeout_is_short_not_review_scale(self):
        """The fallback tier timeout must be an order of magnitude below
        grug review's 330-350s scale - sized for a realtime chat reply."""
        connect_timeout, read_timeout = llm_clients_module._FALLBACK_TIMEOUT
        assert connect_timeout + read_timeout < 30
