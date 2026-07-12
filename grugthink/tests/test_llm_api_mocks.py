"""Tests for LLM API mocks (Gemini and Ollama).

This test suite demonstrates the usage of the LLM API mocks and validates
that they work correctly for testing bot LLM client code.
"""

import pytest
import requests

# Import the mock classes and fixtures from conftest
from src.grugthink.conftest import MockGeminiResponse


class TestGeminiAPIMocks:
    """Tests for Gemini API mock fixtures."""

    def test_mock_gemini_api_basic(self, mock_gemini_api):
        """Test basic Gemini API mock creation."""
        model = mock_gemini_api()
        assert model is not None
        assert model.model_name == "gemini-pro"
        assert model.error_mode is None
        assert model.call_count == 0

    def test_mock_gemini_generate_content(self, mock_gemini_api):
        """Test Gemini API content generation."""
        model = mock_gemini_api()
        response = model.generate_content("What is Python?")

        assert response is not None
        assert isinstance(response, MockGeminiResponse)
        assert response.text
        assert "definition" in response.text.lower()
        assert model.call_count == 1

    def test_mock_gemini_with_config(self, mock_gemini_api):
        """Test Gemini API with custom configuration."""
        model = mock_gemini_api()
        response = model.generate_content(
            "Test prompt", stream=False, generation_config={"temperature": 0.5, "top_p": 0.9}
        )

        assert response is not None
        assert response.text == "This is a mock response from Gemini API for testing purposes."

    def test_mock_gemini_grug_prompt(self, mock_gemini_api):
        """Test Gemini API with grug-themed prompt."""
        model = mock_gemini_api()
        response = model.generate_content("Grug need help with caveman code")

        assert response is not None
        assert "grug" in response.text.lower()
        assert "rock" in response.text.lower() or "stick" in response.text.lower()

    def test_mock_gemini_verify_prompt(self, mock_gemini_api):
        """Test Gemini API with verification prompt."""
        model = mock_gemini_api()
        response = model.generate_content("Verify this statement: The sky is blue")

        assert response is not None
        assert "TRUE" in response.text

    def test_mock_gemini_streaming(self, mock_gemini_api):
        """Test Gemini API with streaming response."""
        model = mock_gemini_api()
        response = model.generate_content("Test streaming", stream=True)

        # Response should be iterable
        chunks = list(response)
        assert len(chunks) > 0
        # Each chunk should be a MockGeminiResponse
        for chunk in chunks:
            assert isinstance(chunk, MockGeminiResponse)

    def test_mock_gemini_module_integration(self, mock_gemini_module):
        """Test full module-level Gemini API mock."""
        import sys

        # Verify module is patched
        assert "google.generativeai" in sys.modules

        import google.generativeai as genai

        # Test configure
        genai.configure(api_key="test_key")
        assert mock_gemini_module["configure"].called

        # Test model creation
        model = genai.GenerativeModel("gemini-pro")
        assert model is not None

        # Test content generation with grug-specific prompt
        response = model.generate_content("Grug need help with programming")
        assert response.text
        assert "grug" in response.text.lower()

    def test_mock_gemini_multiple_calls(self, mock_gemini_api):
        """Test Gemini API tracks multiple calls."""
        model = mock_gemini_api()

        model.generate_content("First prompt")
        assert model.call_count == 1

        model.generate_content("Second prompt")
        assert model.call_count == 2

        model.generate_content("Third prompt")
        assert model.call_count == 3


class TestGeminiAPIErrors:
    """Tests for Gemini API error handling mocks."""

    def test_mock_gemini_rate_limit_error(self, mock_gemini_api):
        """Test Gemini API rate limit error."""
        model = mock_gemini_api(error_mode="rate_limit")

        with pytest.raises(Exception) as exc_info:
            model.generate_content("Test prompt")

        assert "429" in str(exc_info.value)
        assert "Quota exceeded" in str(exc_info.value)

    def test_mock_gemini_timeout_error(self, mock_gemini_api):
        """Test Gemini API timeout error."""
        model = mock_gemini_api(error_mode="timeout")

        with pytest.raises(Exception) as exc_info:
            model.generate_content("Test prompt")

        assert "504" in str(exc_info.value)
        assert "Deadline exceeded" in str(exc_info.value)

    def test_mock_gemini_api_error(self, mock_gemini_api):
        """Test Gemini API internal server error."""
        model = mock_gemini_api(error_mode="api_error")

        with pytest.raises(Exception) as exc_info:
            model.generate_content("Test prompt")

        assert "500" in str(exc_info.value)
        assert "Internal server error" in str(exc_info.value)

    def test_mock_gemini_invalid_key_error(self, mock_gemini_api):
        """Test Gemini API invalid authentication error."""
        model = mock_gemini_api(error_mode="invalid_key")

        with pytest.raises(Exception) as exc_info:
            model.generate_content("Test prompt")

        assert "401" in str(exc_info.value)
        assert "authentication" in str(exc_info.value).lower()

    def test_mock_gemini_errors_fixture(self, mock_gemini_module, mock_gemini_errors):
        """Test error mode switching with fixture."""
        import google.generativeai as genai

        model = genai.GenerativeModel("gemini-pro")

        # Initially no error
        response = model.generate_content("Test")
        assert response.text

        # Set error mode
        mock_gemini_errors("rate_limit")

        # Now should raise error
        with pytest.raises(Exception) as exc_info:
            model.generate_content("Test")

        assert "429" in str(exc_info.value)


class TestOllamaAPIMocks:
    """Tests for Ollama API mock fixtures."""

    def test_mock_ollama_basic(self, mock_ollama_api):
        """Test basic Ollama API mock."""
        response = mock_ollama_api.post(
            "http://localhost:11434/api/generate", json={"model": "llama2", "prompt": "Test prompt", "stream": False}
        )

        assert response.status_code == 200
        assert response.json()["response"]
        assert response.json()["done"] is True
        assert response.json()["model"] == "llama2"

    def test_mock_ollama_generate_endpoint(self, mock_ollama_api):
        """Test Ollama /api/generate endpoint."""
        response = mock_ollama_api.post(
            "http://localhost:11434/api/generate", json={"model": "mistral", "prompt": "What is Python?"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert "done" in data
        assert data["model"] == "mistral"

    def test_mock_ollama_grug_prompt(self, mock_ollama_api):
        """Test Ollama with grug-themed prompt."""
        response = mock_ollama_api.post(
            "http://localhost:11434/api/generate", json={"model": "llama2", "prompt": "Grug need help with code"}
        )

        assert response.status_code == 200
        response_text = response.json()["response"]
        assert "grug" in response_text.lower()
        assert "rock" in response_text.lower() or "fire" in response_text.lower()

    def test_mock_ollama_verify_prompt(self, mock_ollama_api):
        """Test Ollama with verification prompt."""
        response = mock_ollama_api.post(
            "http://localhost:11434/api/generate", json={"model": "llama2", "prompt": "Verify: The sky is blue"}
        )

        assert response.status_code == 200
        response_text = response.json()["response"]
        assert "TRUE" in response_text

    def test_mock_ollama_streaming(self, mock_ollama_api):
        """Test Ollama streaming response."""
        response = mock_ollama_api.post(
            "http://localhost:11434/api/generate", json={"model": "llama2", "prompt": "Test stream", "stream": True}
        )

        assert response.status_code == 200
        # Streaming returns text with newline-delimited JSON
        assert response.text
        assert '{"response"' in response.text
        assert '{"done": true}' in response.text

    def test_mock_ollama_model_not_found(self, mock_ollama_api):
        """Test Ollama model not found error."""
        response = mock_ollama_api.post(
            "http://localhost:11434/api/generate", json={"model": "nonexistent-model", "prompt": "Test"}
        )

        assert response.status_code == 404
        assert "model not found" in response.json()["error"]

    def test_mock_ollama_missing_payload(self, mock_ollama_api):
        """Test Ollama with missing payload."""
        response = mock_ollama_api.post("http://localhost:11434/api/generate", json=None)

        assert response.status_code == 400
        assert "Invalid request" in response.json()["error"]

    def test_mock_ollama_tags_endpoint(self, mock_ollama_api):
        """Test Ollama /api/tags endpoint."""
        response = mock_ollama_api.post("http://localhost:11434/api/tags")

        assert response.status_code == 200
        data = response.json()
        assert "models" in data
        assert len(data["models"]) > 0
        assert any(model["name"] == "llama2:latest" for model in data["models"])

    def test_mock_ollama_show_endpoint(self, mock_ollama_api):
        """Test Ollama /api/show endpoint."""
        response = mock_ollama_api.post("http://localhost:11434/api/show", json={"name": "llama2"})

        assert response.status_code == 200
        data = response.json()
        assert "modelfile" in data
        assert "parameters" in data
        assert "template" in data

    def test_mock_ollama_show_missing_name(self, mock_ollama_api):
        """Test Ollama /api/show without model name."""
        response = mock_ollama_api.post("http://localhost:11434/api/show", json={})

        assert response.status_code == 400

    def test_mock_ollama_unknown_endpoint(self, mock_ollama_api):
        """Test Ollama with unknown endpoint."""
        response = mock_ollama_api.post("http://localhost:11434/api/unknown")

        assert response.status_code == 404
        assert response.text == "Not Found"

    def test_mock_ollama_call_history(self, mock_ollama_api):
        """Test Ollama API call history tracking."""
        mock_ollama_api.post(
            "http://localhost:11434/api/generate", json={"model": "llama2", "prompt": "First"}, timeout=30
        )

        mock_ollama_api.post(
            "http://localhost:11434/api/generate", json={"model": "mistral", "prompt": "Second"}, timeout=60
        )

        assert len(mock_ollama_api.call_history) == 2
        assert mock_ollama_api.call_history[0]["json"]["prompt"] == "First"
        assert mock_ollama_api.call_history[1]["json"]["prompt"] == "Second"


class TestOllamaAPIErrors:
    """Tests for Ollama API error handling mocks."""

    def test_mock_ollama_timeout_error(self, mock_ollama_errors):
        """Test Ollama timeout error."""

        mock_ollama_errors("timeout")

        with pytest.raises(requests.exceptions.Timeout):
            requests.Session().post("http://localhost:11434/api/generate", json={})

    def test_mock_ollama_connection_error(self, mock_ollama_errors):
        """Test Ollama connection error."""

        mock_ollama_errors("connection")

        with pytest.raises(requests.exceptions.ConnectionError):
            requests.Session().post("http://localhost:11434/api/generate", json={})

    def test_mock_ollama_http_error(self, mock_ollama_errors):
        """Test Ollama HTTP error."""
        mock_ollama_errors("http")

        response = requests.Session().post("http://localhost:11434/api/generate", json={})
        assert response.status_code == 500
        assert response.text == "Internal Server Error"

    def test_mock_ollama_rate_limit_error(self, mock_ollama_errors):
        """Test Ollama rate limit error."""
        mock_ollama_errors("rate_limit")

        response = requests.Session().post("http://localhost:11434/api/generate", json={})
        assert response.status_code == 429
        assert response.text == "Too Many Requests"

    def test_mock_ollama_custom_error(self, mock_ollama_errors):
        """Test Ollama custom error."""

        mock_ollama_errors("custom_error_type")

        with pytest.raises(requests.exceptions.RequestException) as exc_info:
            requests.Session().post("http://localhost:11434/api/generate", json={})

        assert "custom_error_type" in str(exc_info.value)


class TestLLMClientIntegration:
    """Integration tests using the LLM client code with mocks."""

    def test_query_gemini_api_integration(self, mock_gemini_module, monkeypatch):
        """Test query_gemini_api function with mock."""
        # Mock the config
        from unittest.mock import MagicMock, patch

        mock_config = MagicMock()
        mock_config.GEMINI_API_KEY = "test_key"
        mock_config.GEMINI_MODEL = "gemini-pro"

        # Mock validate_and_process_response at the module where it's imported
        with patch("src.grugthink.bot.prompts.validate_and_process_response") as mock_validate:
            mock_validate.return_value = "Grug think this good question."

            with patch("src.grugthink.bot.llm_clients.config", mock_config):
                from src.grugthink.bot.llm_clients import query_gemini_api

                result = query_gemini_api(prompt_text="Grug need help", cache_key="test_key", bot_id="test_bot")

                assert result is not None
                assert "grug" in result.lower()

    def test_query_ollama_api_integration(self, mock_ollama_api, monkeypatch):
        """Test query_ollama_api function with mock."""
        # Mock the config
        from unittest.mock import MagicMock, patch

        mock_config = MagicMock()
        mock_config.OLLAMA_URLS = ["http://localhost:11434"]
        mock_config.OLLAMA_MODELS = ["llama2"]

        # Mock validate_and_process_response at the module where it's imported
        with patch("src.grugthink.bot.prompts.validate_and_process_response") as mock_validate:
            mock_validate.return_value = "Grug think this good. Rock smash problem."

            with patch("src.grugthink.bot.llm_clients.config", mock_config):
                from src.grugthink.bot.llm_clients import query_ollama_api

                result = query_ollama_api(
                    prompt_text="Grug need help", cache_key="test_key", personality_name="grug", bot_id="test_bot"
                )

                assert result is not None
                assert "grug" in result.lower()
