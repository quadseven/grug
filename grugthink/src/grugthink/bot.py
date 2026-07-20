#!/usr/bin/env python3
"""GrugThink – Adaptable Personality Engine for Discord
An AI bot that develops unique personalities for each Discord server.
Supports organic personality evolution and self-naming capabilities.
Run with PYTHONUNBUFFERED so every print is flushed immediately.
"""
# Cache bust: 2025-11-12-8675309

from __future__ import annotations

import asyncio

# Import config.py module (not config/ package) - handle shadowing
# config/ package shadows config.py, so we need special handling
import importlib.util
import os
import random
import re
import signal
import sys
import time
import traceback

import discord
from discord.ext import commands

_config_py_path = os.path.join(os.path.dirname(__file__), "config_legacy.py")
_config_spec = importlib.util.spec_from_file_location("grugthink.config_module", _config_py_path)
if _config_spec and _config_spec.loader:
    config = importlib.util.module_from_spec(_config_spec)
    sys.modules["grugthink.config_module"] = config
    _config_spec.loader.exec_module(config)
else:
    raise ImportError("Failed to load config.py module")

# Lazy import of commands module to avoid loading Discord command decorators at module import time
# cross_bot can be imported normally as it doesn't have decorator issues
# from .bot import commands as bot_commands  # Imported lazily in __init__
from . import llm  # noqa: E402  (v2 spark-gateway LLM engine)
from .bot import (  # noqa: E402
    cross_bot,
    review_relay,
    task_relay,
)
from .bot.prompts import (  # noqa: E402
    get_personality_engine,
    is_rate_limited,
    query_model,
)
from .bot.utils import LRUCache, clean_statement, generate_shit_talk  # noqa: E402
from .grug_db import make_server_manager  # noqa: E402
from .logging_config import get_logger  # noqa: E402

log = get_logger(__name__)


# Cross-bot interaction tracking
cross_bot_mentions = LRUCache(max_size=200, ttl_seconds=600)  # Track mentions for 10 minutes
# Track if bots have already fired back at each other for a given conversation
cross_bot_responses = LRUCache(max_size=200, ttl_seconds=600)
cross_bot_topic_responses = LRUCache(max_size=100, ttl_seconds=1800)  # Store bot responses by topic for 30 minutes

# Initialize Server Manager and Personality Engine
server_manager = make_server_manager(config.DB_PATH, load_embedder=config.LOAD_EMBEDDER)


def store_bot_response_for_cross_reference(response: str, personality_name: str):
    """DEPRECATED: Use cross_bot.store_bot_response_for_cross_reference instead."""
    return cross_bot.store_bot_response_for_cross_reference(response, personality_name, cross_bot_topic_responses)


def _pair_key(name_a: str, name_b: str, server_id: str, channel_id: str) -> str:
    """DEPRECATED: Use pair_key from bot.utils instead."""
    from .bot.utils import pair_key

    return pair_key(name_a, name_b, server_id, channel_id)


def get_server_db(interaction_or_guild_id):
    """Get the appropriate database for a Discord interaction or guild ID."""
    if hasattr(interaction_or_guild_id, "guild_id"):
        # It's a Discord interaction
        guild_id = interaction_or_guild_id.guild_id
    elif hasattr(interaction_or_guild_id, "guild") and interaction_or_guild_id.guild:
        # It's a Discord message with guild
        guild_id = interaction_or_guild_id.guild.id
    else:
        # It's a guild ID directly, or a DM
        guild_id = interaction_or_guild_id

    return server_manager.get_server_db(guild_id)


# ---------------------------------------------------------------------------
# 1. Early sanity checks (handled in config.py)
# ---------------------------------------------------------------------------
# All environment variables are now loaded and validated in config.py
# and fatal errors will exit the program there.
# ---------------------------------------------------------------------------


# Discord client setup
intents = discord.Intents.default()
intents.message_content = True


def get_cross_bot_personality_info(server_id: str = "global") -> dict:
    """DEPRECATED: Use cross_bot.get_cross_bot_personality_info instead."""
    return cross_bot.get_cross_bot_personality_info(server_id)


def get_cross_bot_memories(statement: str, server_id: str, current_bot_id: str = None) -> str:
    """DEPRECATED: Use cross_bot.get_cross_bot_memories instead."""
    return cross_bot.get_cross_bot_memories(statement, server_id, server_manager, current_bot_id)


class GrugThinkBot(commands.Cog):
    def __init__(self, client: commands.Bot, bot_instance):
        self.client = client
        self.bot_instance = bot_instance
        self.personality_engine = bot_instance.personality_engine
        self.server_manager = getattr(bot_instance, "server_manager", None)
        self.bot_manager = getattr(bot_instance, "bot_manager", None)  # For cross-bot access
        self.tree = client.tree
        self.chat_frequencies = {}  # Server ID -> chat frequency percentage
        self.last_messages = {}  # Server ID -> list of recent messages for context analysis

        # Activity tracking for intelligent conversation triggers
        self.channel_activity = {}  # Channel ID -> activity data
        self.conversation_states = {}  # Channel ID -> conversation state

        # Use the logger from the bot_instance
        self.log = bot_instance.logger

        # Initialize chat frequency persistence - save to data directory for persistence
        data_dir = os.path.join(self.bot_instance.config.data_dir, self.bot_instance.config.bot_id)
        os.makedirs(data_dir, exist_ok=True)
        self.chat_freq_file = os.path.join(data_dir, "chat_frequencies.json")
        self._load_chat_frequencies()

        # Register slash commands from the commands module (lazy import)
        from .bot import commands as bot_commands

        bot_commands.register_commands(self)

        self.log.info(
            "Bot instance initialized",
            extra={
                "bot_id": self.get_bot_id(),
                "client_user": str(client.user) if client.user else "Not ready",
                "server_manager_available": self.server_manager is not None,
                "bot_manager_available": self.bot_manager is not None,
                "chat_frequencies_loaded": len(self.chat_frequencies),
            },
        )

    def get_server_db(self, interaction_or_guild_id):
        """Get the appropriate database for a Discord interaction or guild ID."""
        if self.server_manager:
            # Multi-bot mode: use the server manager
            if hasattr(interaction_or_guild_id, "guild_id"):
                # It's a Discord interaction
                guild_id = interaction_or_guild_id.guild_id
            elif hasattr(interaction_or_guild_id, "guild") and interaction_or_guild_id.guild:
                # It's a Discord message with guild
                guild_id = interaction_or_guild_id.guild.id
            else:
                # It's a guild ID directly, or a DM
                guild_id = interaction_or_guild_id
            return self.server_manager.get_server_db(guild_id)
        else:
            # Single-bot mode: fallback to the global server manager
            return get_server_db(interaction_or_guild_id)

    def get_bot_id(self):
        """Get the bot ID for logging purposes."""
        try:
            if hasattr(self, "bot_instance") and self.bot_instance and hasattr(self.bot_instance, "config"):
                return getattr(self.bot_instance.config, "bot_id", "unknown-bot")
            else:
                return "unknown-bot"
        except Exception:
            return "unknown-bot"

    def _load_chat_frequencies(self):
        """Load chat frequencies from disk."""
        try:
            import json

            with open(self.chat_freq_file, "r") as f:
                self.chat_frequencies = json.load(f)
            self.log.info(
                "Chat frequencies loaded from disk",
                extra={"bot_id": self.get_bot_id(), "frequency_count": len(self.chat_frequencies)},
            )
        except FileNotFoundError:
            self.chat_frequencies = {}
            self.log.info("No saved chat frequencies found, starting fresh", extra={"bot_id": self.get_bot_id()})
        except Exception as e:
            self.chat_frequencies = {}
            self.log.error("Failed to load chat frequencies", extra={"bot_id": self.get_bot_id(), "error": str(e)})

    def _save_chat_frequencies(self):
        """Save chat frequencies to disk."""
        try:
            import json

            self.log.info(
                "Attempting to save chat frequencies",
                extra={
                    "bot_id": self.get_bot_id(),
                    "file_path": self.chat_freq_file,
                    "frequencies": self.chat_frequencies,
                },
            )
            with open(self.chat_freq_file, "w") as f:
                json.dump(self.chat_frequencies, f)
            self.log.info(
                "Chat frequencies saved to disk",
                extra={"bot_id": self.get_bot_id(), "frequency_count": len(self.chat_frequencies)},
            )
        except Exception as e:
            self.log.error(
                "Failed to save chat frequencies",
                extra={"bot_id": self.get_bot_id(), "file_path": self.chat_freq_file, "error": str(e)},
            )

    @commands.Cog.listener()
    async def on_ready(self):
        bot_id = self.get_bot_id()
        guild_names = [f"{guild.name} ({guild.id})" for guild in self.client.guilds]

        self.log.info(
            "Logged in to Discord and ready.",
            extra={
                "bot_id": bot_id,
                "user": str(self.client.user),
                "guild_count": len(self.client.guilds),
                "guilds": guild_names,
                "latency": round(self.client.latency * 1000, 2),
            },
        )

        try:
            synced = await self.tree.sync()
            self.log.info(
                "Commands synced successfully.",
                extra={"bot_id": bot_id, "synced_commands": len(synced), "command_names": [cmd.name for cmd in synced]},
            )
        except Exception as e:
            # Don't fail on slash command sync errors - message handling still works
            self.log.warning(
                "Failed to sync slash commands (message handling still works)",
                extra={"bot_id": bot_id, "error": str(e)},
            )

    @commands.Cog.listener()
    async def on_connect(self):
        self.log.info(
            "Bot has connected to the Discord gateway.",
            extra={"bot_id": self.get_bot_id(), "session_id": getattr(self.client, "session_id", "unknown")},
        )

    @commands.Cog.listener()
    async def on_disconnect(self):
        self.log.warning(
            "Bot has disconnected from the Discord gateway.",
            extra={
                "bot_id": self.get_bot_id(),
                "disconnect_reason": "Unknown",
                "reconnect_attempt": True,
                "latency": round(self.client.latency * 1000, 2),
            },
        )

    @commands.Cog.listener()
    async def on_resumed(self):
        self.log.info(
            "Bot has resumed its session.",
            extra={
                "bot_id": self.get_bot_id(),
                "session_resumed": True,
                "latency": round(self.client.latency * 1000, 2),
            },
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        """Initialize personality when joining a new server."""
        server_id = str(guild.id)
        personality = self.personality_engine.get_personality(server_id)

        log.info(
            "Joined new server, personality initialized",
            extra={"guild_id": server_id, "guild_name": guild.name, "personality_name": personality.name},
        )

    @commands.Cog.listener()
    async def on_message(self, message):
        """Handle incoming messages and detect name mentions for auto-verification."""
        bot_id = self.get_bot_id()

        self.log.info(
            "Processing message",
            extra={
                "bot_id": bot_id,
                "message_id": message.id,
                "author": str(message.author),
                "author_bot": message.author.bot,
                "channel": str(message.channel),
                "guild": str(message.guild) if message.guild else "DM",
                "content_length": len(message.content),
                "has_attachments": len(message.attachments) > 0,
            },
        )

        # Ignore messages from bots, except for Markov bots and other GrugThink bots
        is_markov_bot = False
        if message.author.bot:
            is_markov_bot = "markov" in message.author.name.lower()
            self.log.info(
                "Bot message detected",
                extra={"bot_id": bot_id, "is_markov_bot": is_markov_bot, "author_name": message.author.name},
            )

        # Get personality for this server
        server_id = str(message.guild.id) if message.guild else "dm"
        personality = self.personality_engine.get_personality(server_id)
        bot_name = personality.chosen_name or personality.name

        self.log.info(
            "Personality loaded",
            extra={
                "bot_id": bot_id,
                "server_id": server_id,
                "bot_name": bot_name,
                "personality_style": personality.response_style,
            },
        )

        # Track channel activity for intelligent conversation triggers
        channel_id = str(message.channel.id)
        current_time = time.time()

        if channel_id not in self.channel_activity:
            self.channel_activity[channel_id] = {
                "last_human_message": 0,
                "last_bot_message": 0,
                "message_count": 0,
                "recent_authors": set(),
            }

        activity = self.channel_activity[channel_id]
        activity["message_count"] += 1
        activity["recent_authors"].add(message.author.display_name or message.author.name)

        # Track human vs bot activity
        if not message.author.bot:
            activity["last_human_message"] = current_time
        elif message.author.bot:
            activity["last_bot_message"] = current_time

        # Detect and store cross-bot mentions from all messages
        mentioned_bots = self.detect_cross_bot_mentions(message)
        if mentioned_bots:
            message_author = message.author.display_name or message.author.name
            if message.author.bot:
                self.store_cross_bot_mention(message_author, mentioned_bots, message)
            else:
                self.store_cross_bot_mention(f"user:{message_author}", mentioned_bots, message)

        # If another bot explicitly talks about this bot, fire back with a short insult once
        if (
            message.author.bot
            and self.is_bot_mentioned(message.content, bot_name)
            and message.author != self.client.user
            and not is_markov_bot
        ):
            channel_id = str(message.channel.id)
            other_name = message.author.display_name or message.author.name
            pair_key = _pair_key(other_name, bot_name, server_id, channel_id)
            pair_state = cross_bot_responses.get(pair_key)
            if pair_state is None:
                pair_state = {other_name.lower(): True, bot_name.lower(): False}
            else:
                pair_state[other_name.lower()] = True

            if not pair_state.get(bot_name.lower(), False):
                pair_state[bot_name.lower()] = True
                cross_bot_responses.put(pair_key, pair_state)
                insult = generate_shit_talk(other_name, personality.response_style)
                # Wait a moment to let the other bot finish their main response first
                await asyncio.sleep(2)
                await message.channel.send(insult)
            else:
                cross_bot_responses.put(pair_key, pair_state)
            return

        if message.author.bot and not is_markov_bot:
            # Ignore other bot messages that don't mention us
            return

        # Check if bot name is mentioned in the message by a human
        if self.is_bot_mentioned(message.content, bot_name):
            # Process commands first
            await self.client.process_commands(message)
            self.log.info(
                "Bot mentioned by user",
                extra={
                    "bot_id": bot_id,
                    "user_id": message.author.id,
                    "server_id": server_id,
                    "channel_id": channel_id,
                    "bot_name": bot_name,
                    "message_preview": message.content[:100],
                },
            )

            if is_rate_limited(message.author.id, self.get_bot_id()):
                self.log.info(
                    "User rate limited", extra={"bot_id": bot_id, "user_id": message.author.id, "server_id": server_id}
                )
                if personality.response_style == "caveman":
                    await message.channel.send("Grug need rest. Wait little.", delete_after=5)
                elif personality.response_style == "british_working_class":
                    await message.channel.send("slow down mate, too much carlin last nite, simple as", delete_after=5)
                else:
                    await message.channel.send("Please wait a moment.", delete_after=5)
                return

            clean_content = self._clean_mention_content(message, personality)
            if task_relay.looks_like_task(clean_content):
                pr_number = review_relay.extract_pr_number(clean_content)
                repo = task_relay.resolve_repo(clean_content)
                if pr_number is not None and repo is not None:
                    # A specific PR is named - this is "what did Elder say",
                    # not "go implement something". Read the real check-run
                    # rather than asking Hermes to do anything.
                    self.log.info(
                        "Review-shaped mention detected, relaying Elder's verdict",
                        extra={"bot_id": bot_id, "user_id": message.author.id, "server_id": server_id, "repo": repo},
                    )
                    asyncio.create_task(review_relay.relay_review(message, bot_name, repo, pr_number))
                    return

                self.log.info(
                    "Task-shaped mention detected, relaying to Hermes",
                    extra={"bot_id": bot_id, "user_id": message.author.id, "server_id": server_id},
                )
                # Launched as a background task, not awaited: relaying and
                # watching for Hermes' reply can take many minutes, and
                # must not block this bot from processing other messages.
                asyncio.create_task(task_relay.relay_to_hermes(self.client, message, bot_name, clean_content))
                return

            self.log.info(
                "Processing bot mention",
                extra={
                    "bot_id": bot_id,
                    "user_id": message.author.id,
                    "server_id": server_id,
                    "starting_response_generation": True,
                },
            )
            await self.handle_auto_verification(message, server_id, personality, mentioned_bots)
        else:
            # Only do engagement logic if no specific bot is mentioned
            any_bot_mentioned = self.detect_any_bot_mentioned(message.content)
            if not any_bot_mentioned:
                self.log.info(
                    "No bot mentioned, checking engagement logic",
                    extra={"bot_id": bot_id, "server_id": server_id, "user_id": message.author.id},
                )

                # Check for intelligent bot-to-bot conversation triggers
                await self.handle_intelligent_bot_conversation(
                    message, server_id, personality, bot_name, channel_id, current_time
                )

                # Natural chat engagement logic
                await self.handle_natural_chat_engagement(message, server_id, personality, bot_name)
            else:
                self.log.info(
                    "Another bot mentioned, skipping all engagement logic",
                    extra={"bot_id": bot_id, "server_id": server_id, "other_bot_mentioned": True},
                )

    def is_bot_mentioned(self, content: str, bot_name: str) -> bool:
        """Check if the bot name is mentioned in the message content."""
        content_lower = content.lower()
        bot_name_lower = bot_name.lower()

        self.log.debug(
            "Checking bot mention",
            extra={
                "bot_id": self.get_bot_id(),
                "content": content,
                "bot_name": bot_name,
                "client_user_id": self.client.user.id if self.client.user else None,
            },
        )

        # Check for direct name mentions (word boundaries)
        name_mentioned = re.search(rf"\b{re.escape(bot_name_lower)}\b", content_lower)
        if name_mentioned:
            self.log.info(
                "Bot name mentioned", extra={"bot_id": self.get_bot_id(), "mentioned_text": name_mentioned.group()}
            )
            return True

        # Check for @mentions of the bot user
        if self.client.user and f"<@{self.client.user.id}>" in content:
            self.log.info("Bot @mentioned", extra={"bot_id": self.get_bot_id(), "mention_type": "direct"})
            return True
        if self.client.user and f"<@!{self.client.user.id}>" in content:
            self.log.info("Bot @mentioned", extra={"bot_id": self.get_bot_id(), "mention_type": "nickname"})
            return True

        self.log.debug(
            "Bot not mentioned",
            extra={"bot_id": self.get_bot_id(), "content_lower": content_lower, "bot_name_lower": bot_name_lower},
        )
        return False

    def detect_any_bot_mentioned(self, content: str) -> bool:
        """Check if ANY bot name is mentioned in the message content."""
        content_lower = content.lower()

        # Common bot names to check (same as cross-bot detection but returns bool)
        bot_names = ["grug", "big rob", "rob", "adaptive", "markov", "grugthink"]

        # Also check for variations
        name_variations = {
            "big rob": ["big rob", "bigrob", "rob"],
            "grug": ["grug", "grugthink"],
            "adaptive": ["adaptive", "adapt"],
            "markov": ["markov"],
        }

        for bot_name in bot_names:
            # Check primary name
            if re.search(rf"\b{re.escape(bot_name.lower())}\b", content_lower):
                self.log.debug(
                    "Detected bot mention in content",
                    extra={"bot_id": self.get_bot_id(), "mentioned_bot": bot_name, "content": content},
                )
                return True

            # Check variations
            for main_name, variations in name_variations.items():
                if bot_name == main_name:
                    for variation in variations:
                        if re.search(rf"\b{re.escape(variation.lower())}\b", content_lower):
                            self.log.debug(
                                "Detected bot mention via variation",
                                extra={
                                    "bot_id": self.get_bot_id(),
                                    "mentioned_bot": main_name,
                                    "variation": variation,
                                    "content": content,
                                },
                            )
                            return True

        return False

    def detect_cross_bot_mentions(self, message) -> list:
        """Detect mentions of other bot names in a message."""
        return cross_bot.detect_cross_bot_mentions(message)

    def store_cross_bot_mention(self, mentioning_source: str, mentioned_bot_names: list, message):
        """Store cross-bot mentions for later reference."""
        return cross_bot.store_cross_bot_mention(mentioning_source, mentioned_bot_names, message, cross_bot_mentions)

    def get_recent_mentions_about_bot(self, bot_name: str, server_id: str, channel_id: str) -> list:
        """Get recent mentions about this bot from other sources."""
        return cross_bot.get_recent_mentions_about_bot(bot_name, server_id, channel_id, cross_bot_mentions)

    def get_cross_bot_topic_context(self, statement: str, current_bot_name: str) -> str:
        """Get context from other bots about topics mentioned in the statement."""
        return cross_bot.get_cross_bot_topic_context(statement, current_bot_name, cross_bot_topic_responses)

    async def store_bot_response_after_edit(self, message, response_content: str, server_id: str):
        """Store bot response for cross-bot detection after message edit."""
        try:
            # Create a mock message object for the cross-bot detection
            # This simulates what would happen if this was a new message
            bot_name = self.personality_engine.get_personality(server_id).chosen_name or "Bot"

            # Check if this response mentions other bots
            mentioned_bots = self.detect_cross_bot_mentions_in_text(response_content)
            if mentioned_bots:
                # Create a simplified message data structure
                class MockMessage:
                    def __init__(self, content, channel, guild, author_name):
                        self.content = content
                        self.channel = channel
                        self.guild = guild
                        self.id = message.id  # Use the actual message ID
                        self.author_name = author_name

                mock_message = MockMessage(response_content, message.channel, message.guild, bot_name)

                # Store the cross-bot mention using the bot as the mentioning source
                self.store_cross_bot_mention(bot_name, mentioned_bots, mock_message)

                log.info(
                    "Stored cross-bot mentions from edited bot response",
                    extra={
                        "bot_id": self.get_bot_id(),
                        "bot_name": bot_name,
                        "mentioned_bots": mentioned_bots,
                        "response_content": response_content[:100],
                    },
                )
        except Exception as e:
            log.error(
                "Failed to store bot response after edit",
                extra={"bot_id": self.get_bot_id(), "error": str(e), "response_content": response_content[:100]},
            )

    def detect_cross_bot_mentions_in_text(self, text: str) -> list:
        """Detect mentions of other bot names in text content."""
        return cross_bot.detect_cross_bot_mentions_in_text(text)

    def _clean_mention_content(self, message, personality) -> str:
        """Strip the bot's name/mentions out of a message, leaving the
        actual statement or request. Shared by handle_auto_verification and
        the task-relay classification check in on_message so the two paths
        agree on what the user actually said.
        """
        clean_content = clean_statement(message.content)

        bot_name = personality.chosen_name or personality.name
        clean_content = re.sub(rf"\b{re.escape(bot_name.lower())}\b", "", clean_content, flags=re.IGNORECASE)

        if self.client.user:
            clean_content = clean_content.replace(f"<@{self.client.user.id}>", "")
            clean_content = clean_content.replace(f"<@!{self.client.user.id}>", "")

        return re.sub(r"\s+", " ", clean_content).strip()

    async def handle_auto_verification(self, message, server_id: str, personality, mentioned_bots=None):
        """Handle automatic verification when bot name is mentioned."""
        bot_id = self.get_bot_id()

        self.log.info(
            "Starting response generation",
            extra={
                "bot_id": bot_id,
                "user_id": message.author.id,
                "server_id": server_id,
                "channel_id": message.channel.id,
                "message_id": message.id,
                "original_content_length": len(message.content),
            },
        )

        # Log if this is a bot interaction
        is_markov_bot = message.author.bot and "markov" in message.author.name.lower()
        is_other_grugthink_bot = message.author.bot and not is_markov_bot

        if is_markov_bot:
            self.log.info(
                "Markov bot interaction",
                extra={
                    "bot_id": bot_id,
                    "markov_bot_name": message.author.name,
                    "server_id": server_id,
                    "message_length": len(message.content),
                },
            )
        elif is_other_grugthink_bot:
            self.log.info(
                "GrugThink bot interaction",
                extra={
                    "bot_id": bot_id,
                    "other_bot_name": message.author.name,
                    "server_id": server_id,
                    "message_length": len(message.content),
                },
            )

        # Clean the message content for verification
        self.log.debug(
            "Cleaning message content",
            extra={"bot_id": bot_id, "original_content": message.content, "content_length": len(message.content)},
        )
        bot_name = personality.chosen_name or personality.name
        clean_content = self._clean_mention_content(message, personality)

        self.log.debug(
            "Content cleaning complete",
            extra={"bot_id": bot_id, "cleaned_content": clean_content, "cleaned_length": len(clean_content)},
        )

        # Prepare contextual info from other bots (only for human messages that
        # don't mention other bots)
        cross_bot_context = ""
        mentioned_bots = mentioned_bots or []

        if not message.author.bot and not mentioned_bots:
            topic_context = self.get_cross_bot_topic_context(clean_content, bot_name)

            if topic_context:
                cross_bot_context = topic_context
                log.info(
                    "Adding cross-bot topic context to response",
                    extra={
                        "bot_id": self.get_bot_id(),
                        "topic_context": topic_context[:50],
                    },
                )

        # Skip if the remaining content is too short or just punctuation
        if len(clean_content) < 5 or not re.search(r"[a-zA-Z]", clean_content):
            # Respond with a personality-appropriate acknowledgment
            if is_markov_bot:
                # Special responses for Markov bot interactions
                if personality.response_style == "caveman":
                    response = f"{bot_name} hear robot friend call!"
                elif personality.response_style == "british_working_class":
                    response = "alright robot mate, wot you sayin, nuff said"
                else:
                    response = "Hello fellow bot! What would you like me to verify?"
            elif is_other_grugthink_bot:
                # Special responses for other GrugThink bot interactions
                other_bot_name = message.author.display_name or message.author.name
                if personality.response_style == "caveman":
                    response = f"{bot_name} hear {other_bot_name} call! What {other_bot_name} want know?"
                elif personality.response_style == "british_working_class":
                    response = f"alright {other_bot_name} mate, wot you after then, nuff said"
                else:
                    response = f"Hello {other_bot_name}! What would you like me to verify?"
            else:
                # Normal human responses - include cross-bot context if available
                if personality.response_style == "caveman":
                    response = f"{bot_name} hear you call!{cross_bot_context}"
                elif personality.response_style == "british_working_class":
                    response = f"wot you want mate, nuff said{cross_bot_context}"
                else:
                    response = f"I'm listening. What would you like me to verify?{cross_bot_context}"

            await message.channel.send(response)
            return

        # Send thinking message
        bot_name_display = personality.chosen_name or personality.name
        if is_markov_bot:
            if personality.response_style == "caveman":
                thinking_msg = f"{bot_name_display} think about robot friend words..."
            elif personality.response_style == "british_working_class":
                thinking_msg = f"{bot_name_display} checkin wot robot mate said..."
            else:
                thinking_msg = f"{bot_name_display} analyzing bot input..."
        elif is_other_grugthink_bot:
            other_bot_name = message.author.display_name or message.author.name
            if personality.response_style == "caveman":
                thinking_msg = f"{bot_name_display} think about {other_bot_name} words..."
            elif personality.response_style == "british_working_class":
                thinking_msg = f"{bot_name_display} checkin wot {other_bot_name} said..."
            else:
                thinking_msg = f"{bot_name_display} considering {other_bot_name}'s statement..."
        else:
            thinking_msg = f"{bot_name_display} thinking..."

        thinking_message = await message.channel.send(thinking_msg)

        try:
            # Get the server-specific database
            server_db = self.get_server_db(message.guild.id if message.guild else "dm")

            # Run verification in executor to avoid blocking
            self.log.debug(
                "About to call query_model",
                extra={
                    "bot_id": bot_id,
                    "clean_content_length": len(clean_content),
                    "clean_content_preview": clean_content[:100],
                    "server_id": server_id,
                },
            )

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, query_model, clean_content, server_db, server_id, self.personality_engine, self.get_bot_id()
            )

            self.log.debug(
                "query_model completed",
                extra={
                    "bot_id": bot_id,
                    "result_exists": result is not None,
                    "result_length": len(result) if result else 0,
                    "result_preview": result[:100] if result else "None",
                },
            )

            if result:
                # Apply personality style to response
                styled_result = self.personality_engine.get_response_with_style(server_id, result)

                # Add cross-bot context if available
                if cross_bot_context:
                    styled_result = styled_result + cross_bot_context

                # Delete the thinking message and post final response as new message
                # This ensures Discord triggers on_message for cross-bot detection
                await thinking_message.delete()
                await message.channel.send(styled_result)

                self.log.info(
                    "Auto-verification completed",
                    extra={
                        "bot_id": bot_id,
                        "user_id": str(message.author.id),
                        "server_id": server_id,
                        "statement_length": len(clean_content),
                        "result_length": len(styled_result),
                        "is_markov_bot": is_markov_bot,
                        "is_grugthink_bot": is_other_grugthink_bot,
                        "author_name": message.author.name if (is_markov_bot or is_other_grugthink_bot) else None,
                    },
                )
            else:
                # API call failed - provide helpful error message
                self.log.warning(
                    "Query model returned None",
                    extra={
                        "bot_id": bot_id,
                        "server_id": server_id,
                        "use_gemini": config.USE_GEMINI,
                        "has_gemini_key": bool(config.GEMINI_API_KEY),
                        "ollama_urls": config.OLLAMA_URLS if not config.USE_GEMINI else "N/A",
                    },
                )

                # Safely delete thinking message
                try:
                    await thinking_message.delete()
                except Exception as delete_error:
                    self.log.warning(
                        "Failed to delete thinking message",
                        extra={"bot_id": bot_id, "error": str(delete_error), "error_type": type(delete_error).__name__},
                    )

                # Provide more helpful error message based on configuration
                try:
                    if config.USE_GEMINI and not config.GEMINI_API_KEY:
                        await message.channel.send("❌ Gemini API key not configured. Please set GEMINI_API_KEY.")
                    elif config.USE_GEMINI:
                        await message.channel.send(
                            "❌ Gemini API unavailable. Please check configuration or try again later."
                        )
                    elif not config.OLLAMA_URLS:
                        await message.channel.send("❌ No AI service configured. Please configure Gemini or Ollama.")
                    else:
                        # Use personality-appropriate error message as fallback
                        error_msg = self.personality_engine.get_error_message(server_id)
                        await message.channel.send(f"❓ {error_msg}")
                except Exception as send_error:
                    self.log.error(
                        "Failed to send error message to user",
                        extra={
                            "bot_id": bot_id,
                            "error": str(send_error),
                            "error_type": type(send_error).__name__,
                            "channel_id": str(message.channel.id),
                        },
                    )

        except Exception as exc:
            import traceback

            self.log.error(
                "Auto-verification error",
                extra={
                    "bot_id": bot_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "traceback": traceback.format_exc(),
                    "user_id": str(message.author.id),
                    "server_id": server_id,
                    "clean_content_length": len(clean_content),
                    "clean_content_preview": clean_content[:100] if clean_content else "Empty",
                },
            )
            # Use personality for error message
            error_msg = self.personality_engine.get_error_message(server_id)
            try:
                await thinking_message.delete()
            except Exception as delete_error:
                self.log.warning(
                    "Failed to delete thinking message during error handling",
                    extra={"bot_id": bot_id, "error": str(delete_error)},
                )
            await message.channel.send(f"💥 {error_msg}")

    async def handle_natural_chat_engagement(self, message, server_id, personality, bot_name):
        """Handle natural bot chat engagement based on conversation flow."""
        bot_id = self.get_bot_id()

        self.log.info(
            "Evaluating natural chat engagement",
            extra={
                "bot_id": bot_id,
                "server_id": server_id,
                "user_id": message.author.id,
                "channel_id": message.channel.id,
            },
        )

        # Skip if bot is mentioned (that's handled separately)
        if self.is_bot_mentioned(message.content, bot_name):
            self.log.info("Skipping natural chat - bot mentioned", extra={"bot_id": bot_id})
            return

        # Note: We don't check if the user is rate limited for natural chat
        # because the bot is initiating the conversation, not responding to the user

        # Get chat frequency setting for this server (default 0%)
        chat_frequency = self.chat_frequencies.get(server_id, 0)
        self.log.info(
            "Chat frequency check", extra={"bot_id": bot_id, "server_id": server_id, "chat_frequency": chat_frequency}
        )

        if chat_frequency == 0:
            self.log.info(
                "Natural chat disabled - frequency is 0%",
                extra={
                    "bot_id": bot_id,
                    "server_id": server_id,
                    "help_text": "Use /set-chat-frequency command to enable natural chat",
                },
            )
            return

        # Track recent messages for context analysis
        if server_id not in self.last_messages:
            self.last_messages[server_id] = []

        self.last_messages[server_id].append(
            {
                "author": message.author.display_name or message.author.name,
                "content": message.content,
                "timestamp": time.time(),
                "is_bot": message.author.bot,
            }
        )

        # Keep only last 10 messages for context
        self.last_messages[server_id] = self.last_messages[server_id][-10:]

        # Analyze conversation context to determine engagement likelihood
        recent_messages = self.last_messages[server_id]

        # Calculate engagement factors
        engagement_score = self.calculate_engagement_score(recent_messages, bot_name, chat_frequency)

        random_roll = random.randint(1, 100)
        self.log.info(
            "Natural chat engagement decision",
            extra={
                "bot_id": bot_id,
                "server_id": server_id,
                "engagement_score": engagement_score,
                "random_roll": random_roll,
                "will_engage": random_roll <= engagement_score,
                "recent_message_count": len(recent_messages),
            },
        )

        # Use randomness based on engagement score
        if random_roll <= engagement_score:
            self.log.info(
                "Triggering natural chat response",
                extra={
                    "bot_id": bot_id,
                    "server_id": server_id,
                    "engagement_score": engagement_score,
                    "random_roll": random_roll,
                },
            )
            # Generate natural response
            await self.generate_natural_response(message, server_id, personality, recent_messages)

    def calculate_engagement_score(self, recent_messages, bot_name, base_frequency):
        """Calculate how likely the bot should be to engage naturally."""
        bot_id = self.get_bot_id()

        if not recent_messages:
            self.log.info("No recent messages for engagement calculation", extra={"bot_id": bot_id})
            return 0

        score = base_frequency
        self.log.info(
            "Starting engagement calculation",
            extra={"bot_id": bot_id, "base_frequency": base_frequency, "recent_message_count": len(recent_messages)},
        )

        # Recent activity boost (more messages = higher engagement)
        recent_count = len([msg for msg in recent_messages if time.time() - msg["timestamp"] < 300])  # 5 minutes
        activity_boost = 0
        if recent_count >= 3:
            activity_boost = 10
        elif recent_count >= 2:
            activity_boost = 5
        score += activity_boost

        # Multiple people chatting boost
        unique_authors = len(set(msg["author"] for msg in recent_messages if not msg["is_bot"]))
        author_boost = 0
        if unique_authors >= 3:
            author_boost = 15
        elif unique_authors >= 2:
            author_boost = 8
        score += author_boost

        # Topic relevance boost (look for keywords the bot might care about)
        content_lower = " ".join(msg["content"].lower() for msg in recent_messages)
        topic_keywords = ["food", "drink", "football", "fight", "cave", "stone", "mammoth", "beer", "pie"]
        topic_boost = 0
        for keyword in topic_keywords:
            if keyword in content_lower:
                topic_boost = 5
                break

        score += topic_boost

        # Don't engage too frequently - check if bot spoke recently
        bot_spoke_recently = any(msg["author"].lower() == bot_name.lower() for msg in recent_messages[-3:])
        frequency_penalty = 0
        if bot_spoke_recently and base_frequency < 100:
            # Only apply penalty if frequency is not at maximum
            if base_frequency >= 75:
                frequency_penalty = 5  # Very small penalty for high frequency settings
            elif base_frequency >= 50:
                frequency_penalty = 15  # Medium penalty for medium frequency
            else:
                frequency_penalty = 30  # Full penalty for normal/low frequency
            score = max(0, score - frequency_penalty)

        # For 100% frequency, allow up to 100%, otherwise cap at 95%
        if base_frequency >= 100:
            final_score = min(score, 100)  # Allow 100% for maximum setting
        else:
            final_score = min(score, 95)  # Cap at 95% for other settings

        self.log.info(
            "Engagement score calculated",
            extra={
                "bot_id": bot_id,
                "base_frequency": base_frequency,
                "activity_boost": activity_boost,
                "author_boost": author_boost,
                "topic_boost": topic_boost,
                "frequency_penalty": frequency_penalty,
                "bot_spoke_recently": bot_spoke_recently,
                "final_score": final_score,
                "recent_count": recent_count,
                "unique_authors": unique_authors,
            },
        )

        return final_score

    async def generate_natural_response(self, message, server_id, personality, recent_messages):
        """Generate a natural conversational response."""
        try:
            # Build context from recent messages
            context_lines = []
            for msg in recent_messages[-5:]:  # Last 5 messages for context
                if not msg["is_bot"] or "markov" in msg["author"].lower():
                    context_lines.append(f"{msg['author']}: {msg['content']}")

            context = "\n".join(context_lines)

            # Create a natural engagement prompt
            prompt = f"""You are {personality.name} with {personality.response_style} personality.

Recent conversation:
{context}

Respond naturally as if you're part of the conversation. Keep it short (1-2 sentences), casual, and in character.
Don't repeat what others said. Add your own perspective or reaction.

IMPORTANT: Respond conversationally, NOT with TRUE/FALSE statements. Just chat naturally.

Response:"""

            # v2 engine: generate via the owned spark-gateway (llm.chat), not
            # Gemini. Degrade to a canned in-character line if the gateway is
            # unavailable, so a chat never crashes the bot.
            try:
                raw_response = await llm.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                f"You are {personality.name}. Stay fully in "
                                "character, casual, 1-2 sentences. Chat naturally "
                                "- never emit TRUE/FALSE."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.7,
                    max_tokens=120,
                )
            except llm.LLMError as gateway_error:
                self.log.warning(
                    "spark-gateway chat unavailable; canned reply",
                    extra={"bot_id": self.get_bot_id(), "error": str(gateway_error)},
                )
                raw_response = (
                    "Grug think about cave things..."
                    if "grug" in personality.name.lower()
                    else "Hmm, Grug brain quiet right now."
                )

            if raw_response and len(raw_response.strip()) > 0:
                # Apply personality style using existing system
                response = self.personality_engine.get_response_with_style(server_id, raw_response)

                # Send with natural delay to seem more organic
                await asyncio.sleep(random.uniform(1, 3))
                try:
                    await message.channel.send(response)
                    self.log.info(
                        "Natural response sent successfully",
                        extra={"bot_id": self.get_bot_id(), "response_length": len(response)},
                    )
                except Exception as send_error:
                    self.log.error(
                        "Failed to send natural response",
                        extra={"bot_id": self.get_bot_id(), "error": str(send_error), "response": response[:100]},
                    )

                log.info(
                    "Natural chat engagement",
                    extra={
                        "bot_id": self.get_bot_id(),
                        "server_id": server_id,
                        "trigger_message": message.content[:100],
                        "response": response[:100],
                    },
                )

        except Exception as e:
            log.error(
                "Failed to generate natural response",
                extra={
                    "bot_id": self.get_bot_id(),
                    "server_id": server_id,
                    "error": str(e),
                },
            )

    async def handle_intelligent_bot_conversation(
        self, message, server_id, personality, bot_name, channel_id, current_time
    ):
        """Handle intelligent bot-to-bot conversations based on activity thresholds and memory sharing."""
        bot_id = self.get_bot_id()

        # Only trigger for human messages (bots handle their own responses)
        if message.author.bot:
            return

        # Skip if this message mentions any bot (they'll handle that separately)
        if self.is_bot_mentioned(message.content, bot_name):
            return

        activity = self.channel_activity.get(channel_id, {})

        # Calculate time since last human activity
        time_since_last_human = current_time - activity.get("last_human_message", current_time)
        time_since_last_bot = current_time - activity.get("last_bot_message", 0)

        self.log.info(
            "Evaluating intelligent bot conversation triggers",
            extra={
                "bot_id": bot_id,
                "server_id": server_id,
                "channel_id": channel_id,
                "time_since_last_human": round(time_since_last_human, 1),
                "time_since_last_bot": round(time_since_last_bot, 1),
                "recent_author_count": len(activity.get("recent_authors", set())),
                "recent_authors": list(activity.get("recent_authors", set())),
            },
        )

        # Intelligent conversation triggers
        should_initiate_conversation = False
        conversation_type = None

        # Trigger 1: Channel has been quiet for a while (5-15 minutes)
        if time_since_last_human > 300 and time_since_last_bot > 600:  # 5min human, 10min bot
            should_initiate_conversation = True
            conversation_type = "break_silence"
            self.log.info(
                "Intelligent bot conversation trigger: break silence",
                extra={
                    "bot_id": bot_id,
                    "trigger_type": conversation_type,
                    "time_since_last_human": round(time_since_last_human, 1),
                    "time_since_last_bot": round(time_since_last_bot, 1),
                },
            )

        # Trigger 2: Multiple people active but no bot engagement recently (3-5 minutes)
        elif len(activity.get("recent_authors", set())) >= 2 and time_since_last_bot > 180:
            should_initiate_conversation = True
            conversation_type = "join_conversation"
            self.log.info(
                "Intelligent bot conversation trigger: join conversation",
                extra={
                    "bot_id": bot_id,
                    "trigger_type": conversation_type,
                    "recent_author_count": len(activity.get("recent_authors", set())),
                    "time_since_last_bot": round(time_since_last_bot, 1),
                },
            )

        # Trigger 3: Random low-frequency engagement (1-3% chance)
        elif random.randint(1, 100) <= 2 and time_since_last_bot > 120:
            should_initiate_conversation = True
            conversation_type = "random_engagement"
            self.log.info(
                "Intelligent bot conversation trigger: random engagement",
                extra={
                    "bot_id": bot_id,
                    "trigger_type": conversation_type,
                    "time_since_last_bot": round(time_since_last_bot, 1),
                },
            )

        if should_initiate_conversation:
            # Rate limiting: max 1 bot conversation every 10 minutes per channel
            last_bot_conversation = getattr(self, "_last_bot_conversations", {}).get(channel_id, 0)
            time_since_last_conversation = current_time - last_bot_conversation

            if time_since_last_conversation < 600:  # 10 minutes
                self.log.debug(
                    "Intelligent conversation rate limited",
                    extra={
                        "bot_id": bot_id,
                        "channel_id": channel_id,
                        "time_since_last_conversation": round(time_since_last_conversation, 1),
                        "rate_limit_seconds": 600,
                    },
                )
                return

            if not hasattr(self, "_last_bot_conversations"):
                self._last_bot_conversations = {}
            self._last_bot_conversations[channel_id] = current_time

            self.log.info(
                "Initiating intelligent bot conversation",
                extra={
                    "bot_id": bot_id,
                    "server_id": server_id,
                    "channel_id": channel_id,
                    "conversation_type": conversation_type,
                },
            )

            await self.initiate_bot_conversation(message, server_id, personality, bot_name, conversation_type)

    async def initiate_bot_conversation(self, message, server_id, personality, bot_name, conversation_type):
        """Initiate a bot-to-bot conversation with memory sharing."""
        try:
            # Get cross-bot memories and facts
            cross_bot_context = await self.get_cross_bot_context(server_id)

            # Build conversation starter prompt based on type
            recent_context = await self.get_recent_channel_context(message.channel, limit=5)

            if conversation_type == "break_silence":
                prompt_base = "The chat has been quiet. Start a casual conversation with another bot."
            elif conversation_type == "join_conversation":
                prompt_base = "Join the ongoing conversation naturally."
            else:  # random_engagement
                prompt_base = "Make a casual comment or ask another bot something interesting."

            prompt = f"""{prompt_base}

Your personality: {personality.response_style}
Your name: {bot_name}

Recent chat context:
{recent_context}

{cross_bot_context}

Start a brief, natural conversation. You can:
- Ask another bot about their day or interests
- Share a quick observation or thought
- Make friendly banter or light teasing
- Bring up a shared memory or experience

Keep it short (1-2 sentences) and in character. Don't force it if there's nothing natural to say.

Response:"""

            # Get response using existing infrastructure
            server_db = self.get_server_db(message.guild.id if message.guild else "dm")
            loop = asyncio.get_running_loop()
            raw_response = await loop.run_in_executor(
                None, query_model, prompt, server_db, server_id, self.personality_engine, self.get_bot_id()
            )

            if raw_response and len(raw_response.strip()) > 0:
                response = self.personality_engine.get_response_with_style(server_id, raw_response)

                # Add natural delay and send
                await asyncio.sleep(random.uniform(2, 5))
                await message.channel.send(response)

                log.info(
                    "Bot conversation initiated",
                    extra={
                        "bot_id": self.get_bot_id(),
                        "server_id": server_id,
                        "conversation_type": conversation_type,
                        "response": response[:100],
                    },
                )

        except Exception as e:
            log.error(
                "Failed to initiate bot conversation",
                extra={
                    "bot_id": self.get_bot_id(),
                    "server_id": server_id,
                    "error": str(e),
                },
            )

    async def get_cross_bot_context(self, server_id):
        """Get context about other bots and their memories for conversation."""
        return await cross_bot.get_cross_bot_context(self, server_id)

    async def get_bot_memories_summary(self, bot_id, server_id, limit=3):
        """Get a summary of another bot's interesting memories."""
        return await cross_bot.get_bot_memories_summary(self, bot_id, server_id, limit)

    async def get_recent_channel_context(self, channel, limit=5):
        """Get recent channel message context."""
        try:
            messages = []
            async for msg in channel.history(limit=limit):
                if not msg.author.bot or "markov" in msg.author.name.lower():
                    author_name = msg.author.display_name or msg.author.name
                    messages.append(f"{author_name}: {msg.content[:100]}")

            messages.reverse()  # Chronological order
            return "\n".join(messages) if messages else "No recent context."

        except Exception as e:
            log.warning(f"Failed to get channel context: {e}")
            return "No recent context."


def main():
    log.info("Connecting to Discord gateway...")

    # Discord client setup
    intents = discord.Intents.default()
    intents.message_content = True
    client = commands.Bot(command_prefix="/", intents=intents)

    # Add the GrugThinkBot cog
    from unittest.mock import MagicMock

    bot_instance_mock = MagicMock()  # This will be replaced by actual instance in bot_manager
    bot_instance_mock.personality_engine = get_personality_engine()
    bot_instance_mock.server_manager = server_manager  # Use the global server manager for single bot mode

    @client.event
    async def on_ready():
        log.info("Single bot mode: Logged in to Discord", extra={"user": str(client.user)})
        try:
            await client.tree.sync()
            log.info("Single bot mode: Commands synced")
        except Exception as e:
            log.error("Single bot mode: Failed to sync commands", extra={"error": str(e)})

    @client.event
    async def on_guild_join(guild):
        server_id = str(guild.id)
        personality = get_personality_engine().get_personality(server_id)
        log.info(
            "Single bot mode: Joined new server, personality initialized",
            extra={"guild_id": server_id, "guild_name": guild.name, "personality_name": personality.name},
        )

    async def setup_cog():
        await client.add_cog(GrugThinkBot(client, bot_instance_mock))

    # Run setup_cog in the event loop
    import asyncio

    asyncio.run(setup_cog())

    # Signal handler for graceful shutdown
    def signal_handler(signum, frame):
        log.info("Received signal, shutting down gracefully", extra={"signal": signum})
        server_manager.close_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        client.run(config.DISCORD_TOKEN)
    except discord.LoginFailure:
        log.fatal("Discord bot token is invalid. Please check your config.DISCORD_TOKEN.")
        sys.exit(1)
    except discord.ConnectionClosed as e:
        log.error("Discord connection closed unexpectedly", extra={"error": str(e)})
        # Attempt to reconnect or handle gracefully
    except Exception as exc:
        log.fatal(
            "Unhandled exception in Discord client",
            extra={"error": str(exc), "traceback": traceback.format_exc()},
        )
        sys.exit(1)
    finally:
        server_manager.close_all()  # Ensure all DB connections are closed gracefully if not already by signal
        log.info("GrugThink personality engine has shut down.")
        sys.exit(0)


if __name__ == "__main__":
    main()
