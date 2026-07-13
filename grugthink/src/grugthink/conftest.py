"""
Global pytest configuration and fixtures for CI optimization.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="session", autouse=True)
def mock_heavy_dependencies():
    """Mock heavy dependencies to speed up CI."""

    # Mock FAISS completely
    if "faiss" not in sys.modules:
        fake_faiss = types.ModuleType("faiss")
        fake_faiss.__spec__ = importlib.util.spec_from_loader("faiss", loader=None)

        # Minimal FAISS implementation for tests
        class IndexFlatL2:
            def __init__(self, dim):
                self.dim = dim
                self.ntotal = 0

            def add(self, vecs):
                self.ntotal += len(vecs)

            def reset(self):
                self.ntotal = 0

            def search(self, queries, k):
                import numpy as np

                batch_size = len(queries)
                dists = np.zeros((batch_size, k), dtype=np.float32)
                idx = np.full((batch_size, k), -1, dtype=np.int64)
                return dists, idx

        class IndexIDMap:
            def __init__(self, index):
                self.index = index
                self.ntotal = 0

            def add_with_ids(self, embeddings, ids):
                self.index.add(embeddings)
                self.ntotal = self.index.ntotal

            def search(self, queries, k):
                return self.index.search(queries, k)

            def reset(self):
                self.index.reset()
                self.ntotal = 0

        def write_index(index, path):
            pass

        def read_index(path):
            return IndexIDMap(IndexFlatL2(384))

        fake_faiss.IndexFlatL2 = IndexFlatL2
        fake_faiss.IndexIDMap = IndexIDMap
        fake_faiss.write_index = write_index
        fake_faiss.read_index = read_index
        sys.modules["faiss"] = fake_faiss

    # Mock sentence transformers
    if "sentence_transformers" not in sys.modules:
        fake_st = types.ModuleType("sentence_transformers")
        fake_st.__spec__ = importlib.util.spec_from_loader("sentence_transformers", loader=None)

        class SentenceTransformer:
            def __init__(self, model_name, **kwargs):
                self.model_name = model_name

            def encode(self, texts, **kwargs):
                import hashlib

                import numpy as np

                if isinstance(texts, str):
                    texts = [texts]
                # Create deterministic embeddings with keyword-based similarity
                embeddings = []
                for text in texts:
                    # Create embedding based on word content - deterministic
                    words = set(text.lower().replace("?", "").replace(".", "").split())
                    embedding = np.zeros(384, dtype=np.float32)

                    # Use deterministic hash of words as features
                    for word in words:
                        # Use MD5 for deterministic hashing across platforms
                        word_hash = int(hashlib.md5(word.encode()).hexdigest()[:8], 16) % 384
                        embedding[word_hash] += 1.0

                    # Add some keyword-specific features for better matching
                    keyword_map = {
                        "hunt": 10,
                        "mammoth": 11,
                        "grug": 12,
                        "fire": 20,
                        "make": 21,
                        "ugga": 22,
                        "good": 23,
                        "find": 30,
                        "stone": 31,
                        "bork": 32,
                        "shiny": 33,
                        "sky": 40,
                        "blue": 41,
                        "think": 42,
                        "color": 40,  # color->sky mapping
                    }

                    for word in words:
                        if word in keyword_map:
                            embedding[keyword_map[word]] += 2.0  # Boost important keywords

                    # Normalize to unit vector for cosine similarity
                    norm = np.linalg.norm(embedding)
                    if norm > 0:
                        embedding = embedding / norm

                    embeddings.append(embedding)
                return np.array(embeddings, dtype=np.float32)

            def get_sentence_embedding_dimension(self):
                return 384

        fake_st.SentenceTransformer = SentenceTransformer
        sys.modules["sentence_transformers"] = fake_st

    # Mock torch
    if "torch" not in sys.modules:
        fake_torch = types.ModuleType("torch")
        fake_torch.__spec__ = importlib.util.spec_from_loader("torch", loader=None)
        fake_torch.cuda = MagicMock()
        fake_torch.cuda.is_available = MagicMock(return_value=False)
        sys.modules["torch"] = fake_torch


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Set up test environment variables."""
    os.environ.setdefault("DISCORD_TOKEN", "test_token")
    os.environ.setdefault("GEMINI_API_KEY", "test_gemini_key")
    os.environ.setdefault("LOG_LEVEL", "WARNING")  # Reduce log noise in tests


# ============================================================================
# Discord.py Mocks for Testing Bot Commands
# ============================================================================


@pytest.fixture
def mock_discord_user():
    """Create a mock Discord User object.

    Provides a mock User with typical attributes and methods used in bot commands.
    The user has a fixed ID (123456789) and can be customized per test if needed.

    Returns:
        MagicMock: A mock Discord User with common attributes.

    Example:
        >>> def test_command(mock_discord_user):
        ...     assert mock_discord_user.id == 123456789
        ...     assert mock_discord_user.name == "TestUser"
    """
    user = MagicMock()
    user.id = 123456789
    user.name = "TestUser"
    user.display_name = "TestUser"
    user.discriminator = "0001"
    user.bot = False
    user.mention = "<@123456789>"
    return user


@pytest.fixture
def mock_discord_member(mock_discord_user):
    """Create a mock Discord Member object.

    A Member is a User with additional guild-specific attributes like roles,
    nickname, and guild permissions. Inherits from mock_discord_user.

    Args:
        mock_discord_user: The base user fixture to extend.

    Returns:
        MagicMock: A mock Discord Member with guild-specific attributes.

    Example:
        >>> def test_command(mock_discord_member):
        ...     assert mock_discord_member.nick == "TestNick"
        ...     assert mock_discord_member.guild.id == 987654321
    """
    member = MagicMock(spec=["id", "name", "display_name", "bot", "mention", "nick", "roles", "guild"])
    # Copy user attributes
    member.id = mock_discord_user.id
    member.name = mock_discord_user.name
    member.display_name = mock_discord_user.display_name
    member.bot = mock_discord_user.bot
    member.mention = mock_discord_user.mention

    # Add member-specific attributes
    member.nick = "TestNick"
    member.roles = []
    member.guild = MagicMock()
    member.guild.id = 987654321
    member.guild.name = "Test Guild"

    return member


@pytest.fixture
def mock_discord_guild():
    """Create a mock Discord Guild (server) object.

    Provides a mock Guild with common attributes like name, ID, channels, and members.

    Returns:
        MagicMock: A mock Discord Guild object.

    Example:
        >>> def test_command(mock_discord_guild):
        ...     assert mock_discord_guild.id == 987654321
        ...     assert mock_discord_guild.name == "Test Guild"
    """
    guild = MagicMock()
    guild.id = 987654321
    guild.name = "Test Guild"
    guild.member_count = 100
    guild.channels = []
    guild.members = []
    guild.roles = []
    guild.owner_id = 123456789
    return guild


@pytest.fixture
def mock_discord_text_channel(mock_discord_guild):
    """Create a mock Discord TextChannel object.

    Provides a mock TextChannel with async methods for sending messages and
    retrieving message history.

    Args:
        mock_discord_guild: The guild that owns this channel.

    Returns:
        MagicMock: A mock Discord TextChannel with async methods.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_send_message(mock_discord_text_channel):
        ...     msg = await mock_discord_text_channel.send("Hello")
        ...     mock_discord_text_channel.send.assert_called_with("Hello")
    """
    from unittest.mock import AsyncMock

    channel = MagicMock()
    channel.id = 111222333
    channel.name = "test-channel"
    channel.guild = mock_discord_guild
    channel.mention = "<#111222333>"
    channel.type = "text"

    # Mock async methods
    channel.send = AsyncMock(return_value=MagicMock(id=999888777, content="Test message"))
    channel.typing = MagicMock()
    channel.fetch_message = AsyncMock()

    # Mock history as an async generator
    async def mock_history(limit=100):
        """Mock message history generator."""
        # Return empty list by default, can be overridden in tests
        for _ in range(0):
            yield

    channel.history = mock_history

    return channel


@pytest.fixture
def mock_discord_message(mock_discord_user, mock_discord_text_channel):
    """Create a mock Discord Message object.

    Provides a mock Message with common attributes and async methods for
    reactions, editing, and deletion.

    Args:
        mock_discord_user: The author of the message.
        mock_discord_text_channel: The channel containing the message.

    Returns:
        MagicMock: A mock Discord Message object.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_message(mock_discord_message):
        ...     await mock_discord_message.add_reaction("👍")
        ...     assert mock_discord_message.content == "Test message"
    """
    from unittest.mock import AsyncMock

    message = MagicMock()
    message.id = 999888777
    message.content = "Test message"
    message.author = mock_discord_user
    message.channel = mock_discord_text_channel
    message.guild = mock_discord_text_channel.guild
    message.created_at = MagicMock()
    message.edited_at = None
    message.mentions = []
    message.mention_everyone = False
    message.embeds = []
    message.attachments = []

    # Mock async methods
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    message.reply = AsyncMock()

    return message


@pytest.fixture
def mock_discord_embed():
    """Create a mock Discord Embed object.

    Provides a mock Embed with methods for adding fields, setting colors,
    and other embed properties.

    Returns:
        MagicMock: A mock Discord Embed object.

    Example:
        >>> def test_embed(mock_discord_embed):
        ...     mock_discord_embed.add_field(name="Test", value="Value")
        ...     assert len(mock_discord_embed.fields) == 1
    """
    embed = MagicMock()
    embed.title = None
    embed.description = None
    embed.color = None
    embed.fields = []
    embed.footer = None
    embed.image = None
    embed.thumbnail = None
    embed.author = None

    # Mock methods
    def add_field(name, value, inline=True):
        field = {"name": name, "value": value, "inline": inline}
        embed.fields.append(field)
        return embed

    embed.add_field = MagicMock(side_effect=add_field)
    embed.set_footer = MagicMock(return_value=embed)
    embed.set_image = MagicMock(return_value=embed)
    embed.set_thumbnail = MagicMock(return_value=embed)
    embed.set_author = MagicMock(return_value=embed)

    return embed


@pytest.fixture
def mock_discord_interaction_response():
    """Create a mock Discord InteractionResponse object.

    Provides async methods for deferring responses and sending messages.
    This is accessed via interaction.response in Discord.py.

    Returns:
        MagicMock: A mock InteractionResponse with async methods.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_response(mock_discord_interaction_response):
        ...     await mock_discord_interaction_response.defer()
        ...     mock_discord_interaction_response.defer.assert_called_once()
    """
    from unittest.mock import AsyncMock

    response = MagicMock()
    response.defer = AsyncMock()
    response.send_message = AsyncMock()
    response.edit_message = AsyncMock()
    response.is_done = MagicMock(return_value=False)

    return response


@pytest.fixture
def mock_discord_interaction_followup():
    """Create a mock Discord Webhook (followup) object.

    Provides async methods for sending followup messages after an
    interaction has been deferred or responded to.

    Returns:
        MagicMock: A mock Webhook with async send method.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_followup(mock_discord_interaction_followup):
        ...     msg = await mock_discord_interaction_followup.send("Followup")
        ...     mock_discord_interaction_followup.send.assert_called_with("Followup")
    """
    from unittest.mock import AsyncMock

    followup = MagicMock()
    # Mock send to return a message-like object
    followup.send = AsyncMock(return_value=MagicMock(id=111222333, content="Followup message"))

    return followup


@pytest.fixture
def mock_discord_interaction(
    mock_discord_user,
    mock_discord_guild,
    mock_discord_text_channel,
    mock_discord_interaction_response,
    mock_discord_interaction_followup,
):
    """Create a mock Discord Interaction object.

    Provides a complete interaction mock with response, followup, user,
    guild, and channel attributes. This is the main object passed to
    slash command handlers.

    Args:
        mock_discord_user: The user who triggered the interaction.
        mock_discord_guild: The guild where the interaction occurred.
        mock_discord_text_channel: The channel where the interaction occurred.
        mock_discord_interaction_response: The response object for initial replies.
        mock_discord_interaction_followup: The followup object for additional messages.

    Returns:
        MagicMock: A mock Discord Interaction object.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_interaction(mock_discord_interaction):
        ...     await mock_discord_interaction.response.defer()
        ...     await mock_discord_interaction.followup.send("Done")
        ...     assert mock_discord_interaction.user.id == 123456789
    """
    interaction = MagicMock()
    interaction.user = mock_discord_user
    interaction.guild = mock_discord_guild
    interaction.guild_id = mock_discord_guild.id
    interaction.channel = mock_discord_text_channel
    interaction.channel_id = mock_discord_text_channel.id
    interaction.response = mock_discord_interaction_response
    interaction.followup = mock_discord_interaction_followup
    interaction.type = 2  # APPLICATION_COMMAND
    interaction.token = "test_interaction_token"
    interaction.id = 555666777
    interaction.application_id = 444555666

    return interaction


@pytest.fixture
def mock_discord_client():
    """Create a mock Discord Client object.

    Provides a mock client with async methods for fetching users, guilds,
    and other Discord objects. This represents the bot's connection to Discord.

    Returns:
        MagicMock: A mock Discord Client with async methods.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_client(mock_discord_client):
        ...     user = await mock_discord_client.fetch_user(123)
        ...     assert user is not None
    """
    from unittest.mock import AsyncMock

    client = MagicMock()
    client.user = MagicMock()
    client.user.id = 999999999
    client.user.name = "TestBot"
    client.user.bot = True

    # Mock async methods
    client.fetch_user = AsyncMock()
    client.fetch_guild = AsyncMock()
    client.fetch_channel = AsyncMock()
    client.wait_until_ready = AsyncMock()
    client.close = AsyncMock()

    # Mock properties
    client.guilds = []
    client.latency = 0.05

    return client


@pytest.fixture
def mock_discord_app_commands_tree(mock_discord_client):
    """Create a mock Discord app_commands.CommandTree object.

    The CommandTree manages slash command registration and syncing.
    Provides async methods for syncing commands to guilds or globally.

    Args:
        mock_discord_client: The client that owns this command tree.

    Returns:
        MagicMock: A mock CommandTree with async methods.

    Example:
        >>> @pytest.mark.asyncio
        >>> async def test_tree(mock_discord_app_commands_tree):
        ...     await mock_discord_app_commands_tree.sync()
        ...     mock_discord_app_commands_tree.sync.assert_called_once()
    """
    from unittest.mock import AsyncMock

    tree = MagicMock()
    tree.client = mock_discord_client
    tree.sync = AsyncMock()
    tree.copy_global_to = MagicMock()
    tree.clear_commands = MagicMock()
    tree.get_commands = MagicMock(return_value=[])
    tree.add_command = MagicMock()
    tree.remove_command = MagicMock()

    return tree


# ============================================================================
# LLM API Mocks - Gemini and Ollama
# ============================================================================


class MockGeminiResponse:
    """Mock response object for Gemini API calls.

    Simulates the response structure from google.generativeai.GenerativeModel.generate_content().
    """

    def __init__(self, text: str, error: Exception | None = None):
        """Initialize mock response.

        Args:
            text: The generated text content
            error: Optional exception to raise when accessing properties
        """
        self._text = text
        self._error = error

    @property
    def text(self) -> str:
        """Get response text, raising error if configured."""
        if self._error:
            raise self._error
        return self._text

    def __iter__(self):
        """Support streaming iteration."""
        if self._error:
            raise self._error
        # Simulate streaming by yielding chunks
        words = self._text.split()
        for i in range(0, len(words), 3):
            chunk = " ".join(words[i : i + 3])
            yield MockGeminiResponse(chunk + " ")


class MockGeminiModel:
    """Mock Google Gemini GenerativeModel for testing.

    Provides realistic mock responses for different prompt patterns and error conditions.
    """

    def __init__(self, model_name: str = "gemini-pro", error_mode: str | None = None):
        """Initialize mock model.

        Args:
            model_name: Name of the model (e.g., 'gemini-pro', 'gemini-1.5-flash')
            error_mode: Optional error mode ('rate_limit', 'timeout', 'api_error', 'invalid_key')
        """
        self.model_name = model_name
        self.error_mode = error_mode
        self.call_count = 0

    def generate_content(
        self, prompt: str, stream: bool = False, generation_config: dict | None = None
    ) -> MockGeminiResponse:
        """Generate content from prompt.

        Args:
            prompt: The input prompt text
            stream: Whether to stream the response
            generation_config: Optional generation configuration

        Returns:
            MockGeminiResponse object with generated text

        Raises:
            Various exceptions based on error_mode
        """
        self.call_count += 1

        # Simulate error conditions
        if self.error_mode == "rate_limit":
            # Create custom exception classes that mimic google.api_core.exceptions
            class ResourceExhausted(Exception):
                pass

            raise ResourceExhausted("429 Quota exceeded for quota metric")

        if self.error_mode == "timeout":

            class DeadlineExceeded(Exception):
                pass

            raise DeadlineExceeded("504 Deadline exceeded")

        if self.error_mode == "api_error":

            class InternalServerError(Exception):
                pass

            raise InternalServerError("500 Internal server error")

        if self.error_mode == "invalid_key":

            class Unauthenticated(Exception):
                pass

            raise Unauthenticated("401 Request had invalid authentication credentials")

        # Generate response based on prompt patterns
        response_text = self._generate_response(prompt)

        if stream:
            return MockGeminiResponse(response_text)
        return MockGeminiResponse(response_text)

    def _generate_response(self, prompt: str) -> str:
        """Generate realistic response based on prompt content.

        Args:
            prompt: The input prompt

        Returns:
            Generated response text
        """
        prompt_lower = prompt.lower()

        # Pattern matching for different types of prompts
        if "verify" in prompt_lower or "true or false" in prompt_lower:
            return "TRUE - This statement appears to be accurate."

        if "what is" in prompt_lower or "define" in prompt_lower:
            return "This is a test definition response from the mock Gemini API."

        if "grug" in prompt_lower or "caveman" in prompt_lower:
            return "Grug think this good question. Grug say answer is simple. Rock and stick always work."

        if "summarize" in prompt_lower or "summary" in prompt_lower:
            return "Summary: The key points are X, Y, and Z."

        # Default response
        return "This is a mock response from Gemini API for testing purposes."


@pytest.fixture
def mock_gemini_api():
    """Mock Google Gemini API for testing.

    Provides a factory function to create mock models with different configurations.

    Returns:
        Factory function that creates MockGeminiModel instances

    Example:
        def test_gemini(mock_gemini_api):
            model = mock_gemini_api()
            response = model.generate_content("test prompt")
            assert response.text
    """

    def _create_mock_model(model_name: str = "gemini-pro", error_mode: str | None = None):
        return MockGeminiModel(model_name=model_name, error_mode=error_mode)

    return _create_mock_model


@pytest.fixture
def mock_gemini_module(monkeypatch, mock_gemini_api):
    """Mock the entire google.generativeai module.

    Automatically patches the module to return mock models.

    Args:
        monkeypatch: pytest monkeypatch fixture
        mock_gemini_api: mock_gemini_api fixture

    Returns:
        Dictionary with mock module components for inspection/configuration

    Example:
        def test_with_gemini(mock_gemini_module):
            import google.generativeai as genai
            model = genai.GenerativeModel("gemini-pro")
            response = model.generate_content("test")
            assert response.text
    """
    mock_genai = MagicMock()
    mock_model_instance = mock_gemini_api()

    # Configure the mock module
    mock_genai.configure = MagicMock()
    mock_genai.GenerativeModel = MagicMock(return_value=mock_model_instance)

    # Patch the module
    monkeypatch.setitem(sys.modules, "google.generativeai", mock_genai)

    return {
        "module": mock_genai,
        "model_instance": mock_model_instance,
        "configure": mock_genai.configure,
    }


class MockOllamaResponse:
    """Mock HTTP response for Ollama API calls.

    Simulates responses from the Ollama REST API.
    """

    def __init__(
        self, status_code: int = 200, json_data: dict | None = None, error: Exception | None = None, text: str = ""
    ):
        """Initialize mock response.

        Args:
            status_code: HTTP status code
            json_data: JSON response data
            error: Optional exception to raise
            text: Response text content
        """
        self.status_code = status_code
        self._json_data = json_data or {}
        self._error = error
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict:
        """Return JSON response data.

        Returns:
            Dictionary with response data

        Raises:
            Exception if error is configured
        """
        if self._error:
            raise self._error
        return self._json_data

    def raise_for_status(self):
        """Raise HTTPError for bad status codes."""
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} Error", response=self)


def _mock_ollama_generate_response(prompt: str) -> str:
    """Generate realistic Ollama response based on prompt."""
    prompt_lower = prompt.lower()

    if "grug" in prompt_lower or "caveman" in prompt_lower:
        return "Grug think this good. Rock smash problem. Fire make warm."

    if "verify" in prompt_lower:
        return "TRUE - Statement check out good."

    if "what is" in prompt_lower or "define" in prompt_lower:
        return "This is mock definition from Ollama local model."

    return "Mock response from Ollama API for testing."


def _mock_ollama_handle_generate(payload: dict | None) -> MockOllamaResponse:
    """Handle /api/generate endpoint (mock_ollama_api fixture)."""
    if not payload:
        return MockOllamaResponse(
            status_code=400, json_data={"error": "Invalid request - missing payload"}, text="Bad Request"
        )

    model = payload.get("model", "")
    prompt = payload.get("prompt", "")
    stream = payload.get("stream", False)

    # Simulate model not found
    if "nonexistent" in model:
        return MockOllamaResponse(status_code=404, json_data={"error": "model not found"}, text="Not Found")

    # Generate response based on prompt
    response_text = _mock_ollama_generate_response(prompt)

    if stream:
        # For streaming, return newline-delimited JSON
        stream_chunks = [f'{{"response": "{word} "}}' for word in response_text.split()]
        stream_chunks.append('{"done": true}')
        return MockOllamaResponse(status_code=200, json_data={}, text="\n".join(stream_chunks))
    else:
        # Non-streaming response
        return MockOllamaResponse(
            status_code=200,
            json_data={
                "model": model,
                "response": response_text,
                "done": True,
                "context": [1, 2, 3],
                "total_duration": 1000000000,
                "load_duration": 100000000,
                "prompt_eval_count": 10,
                "eval_count": 20,
            },
        )


def _mock_ollama_handle_tags() -> MockOllamaResponse:
    """Handle /api/tags endpoint (mock_ollama_api fixture)."""
    return MockOllamaResponse(
        status_code=200,
        json_data={
            "models": [
                {"name": "llama2:latest", "size": 3826793677},
                {"name": "mistral:latest", "size": 4108916688},
            ]
        },
    )


def _mock_ollama_handle_show(payload: dict | None) -> MockOllamaResponse:
    """Handle /api/show endpoint (mock_ollama_api fixture)."""
    if not payload or "name" not in payload:
        return MockOllamaResponse(status_code=400, text="Bad Request")

    return MockOllamaResponse(
        status_code=200,
        json_data={
            "modelfile": "# Modelfile content",
            "parameters": "temperature 0.8",
            "template": "{{ .Prompt }}",
        },
    )


@pytest.fixture
def mock_ollama_api(monkeypatch):
    """Mock Ollama API HTTP responses.

    Patches the requests.Session.post method to return mock responses.

    Returns:
        Mock session object for inspection and configuration

    Example:
        def test_ollama(mock_ollama_api):
            response = mock_ollama_api.post("http://localhost:11434/api/generate", json={})
            assert response.status_code == 200
    """
    mock_session = MagicMock()
    call_history = []

    def _mock_post(url: str, json=None, timeout: int = 60, headers=None, **kwargs):
        """Mock POST request handler."""
        call_history.append({"url": url, "json": json, "timeout": timeout, "headers": headers})

        # Parse URL to determine endpoint
        if "/api/generate" in url:
            return _mock_ollama_handle_generate(json)
        elif "/api/tags" in url:
            return _mock_ollama_handle_tags()
        elif "/api/show" in url:
            return _mock_ollama_handle_show(json)
        else:
            return MockOllamaResponse(status_code=404, text="Not Found")

    mock_session.post = _mock_post
    mock_session.call_history = call_history

    # Patch both requests.Session.post and the session instance used by llm_clients
    monkeypatch.setattr("requests.Session.post", _mock_post)
    monkeypatch.setattr("src.grugthink.bot.llm_clients.session.post", _mock_post)

    return mock_session


@pytest.fixture
def mock_ollama_errors(monkeypatch):
    """Mock Ollama API with various error conditions.

    Returns:
        Factory function to create error responses

    Example:
        def test_ollama_timeout(mock_ollama_errors):
            mock_ollama_errors("timeout")
            # Now requests will raise timeout errors
    """

    def _create_error_mock(error_type: str):
        """Create mock that raises specific error types."""
        import requests

        def _error_post(*args, **kwargs):
            if error_type == "timeout":
                raise requests.exceptions.Timeout("Connection timeout")
            elif error_type == "connection":
                raise requests.exceptions.ConnectionError("Connection refused")
            elif error_type == "http":
                return MockOllamaResponse(status_code=500, text="Internal Server Error")
            elif error_type == "rate_limit":
                return MockOllamaResponse(status_code=429, text="Too Many Requests")
            else:
                raise requests.exceptions.RequestException(f"Mock error: {error_type}")

        monkeypatch.setattr("requests.Session.post", _error_post)

    return _create_error_mock


@pytest.fixture
def mock_gemini_errors(mock_gemini_module):
    """Mock Gemini API with various error conditions.

    Returns:
        Function to set error mode on the mock model

    Example:
        def test_gemini_rate_limit(mock_gemini_errors):
            mock_gemini_errors("rate_limit")
            # Now Gemini API will raise rate limit errors
    """

    def _set_error_mode(error_mode: str):
        """Set error mode on the mock Gemini model."""
        mock_gemini_module["model_instance"].error_mode = error_mode

    return _set_error_mode
