"""Cross-bot interaction logic for GrugThink multi-bot mode.

This module contains all logic for bots to interact with each other, including:
- Detecting and tracking mentions of other bots
- Storing and retrieving cross-bot responses
- Getting context and memories from other bots
- Managing topic-based cross-bot conversations
"""

import os
import re
import time

from ..logging_config import get_logger

log = get_logger(__name__)


def store_bot_response_for_cross_reference(response: str, personality_name: str, cross_bot_topic_responses):
    """Store bot response for other bots to reference when topics are mentioned.

    Args:
        response: The bot's response text to store
        personality_name: Name of the bot making the response
        cross_bot_topic_responses: LRUCache instance for storing topic-based responses

    Returns:
        None
    """
    if not personality_name or not response:
        return

    # Extract key topics/keywords from the response
    response_lower = response.lower()
    topics = []

    # Common topics that bots might argue about
    topic_keywords = {
        "carling": ["carling", "beer", "drink", "pint"],
        "beer": ["beer", "carling", "drink", "pint", "ale"],
        "food": ["pie", "potato", "shepherd", "meat", "food", "grub"],
        "pie": ["pie", "potato", "shepherd", "meat", "food"],
        "fight": ["fight", "beat", "strong", "tough", "battle"],
        "football": ["football", "footy", "norf", "fc", "team"],
        "caveman": ["caveman", "mammoth", "cave", "stone", "hunt"],
    }

    # Check which topics this response relates to
    for topic, keywords in topic_keywords.items():
        if any(keyword in response_lower for keyword in keywords):
            topics.append(topic)

    # Store the response under each relevant topic
    for topic in topics:
        topic_key = f"{topic}:{personality_name.lower()}"
        topic_data = {"bot_name": personality_name, "response": response, "timestamp": time.time(), "topic": topic}
        cross_bot_topic_responses.put(topic_key, topic_data)

        log.info(
            "Stored bot response for cross-reference",
            extra={"bot_name": personality_name, "topic": topic},
        )


def get_cross_bot_personality_info(server_id: str = "global"):
    """Get personality information about other bots in the system.

    Args:
        server_id: Server ID to get personality info for (default: "global")

    Returns:
        dict: Mapping of bot IDs to personality information including:
            - bot_name: Display name of the bot
            - response_style: Communication style (e.g., "caveman", "british_working_class")
            - personality_traits: Dict of personality characteristics
            - background_elements: List of background story elements
    """
    personality_info = {}
    try:
        # In multi-bot mode, try to access bot manager for personality information
        from ..main import get_bot_manager

        bot_manager = get_bot_manager()
        if bot_manager:
            for bot_id, bot_instance in bot_manager.bots.items():
                try:
                    # Get the bot's personality for this server
                    personality = bot_instance.personality_engine.get_personality(server_id)
                    bot_name = personality.chosen_name or personality.name

                    # Create a recognizable key from bot name
                    personality_info[bot_id] = {
                        "bot_name": bot_name,
                        "response_style": personality.response_style,
                        "personality_traits": personality.personality_traits,
                        "background_elements": personality.background_elements,
                    }

                    # Also add by name for easier lookup
                    name_key = bot_name.lower().replace(" ", "_")
                    personality_info[name_key] = personality_info[bot_id]

                except Exception as e:
                    log.debug("Could not access bot personality", extra={"bot_id": bot_id, "error": str(e)})
    except ImportError:
        # Not in multi-bot mode, provide fallback personality info for known bots
        personality_info.update(
            {
                "grug": {
                    "bot_name": "Grug",
                    "response_style": "caveman",
                    "personality_traits": {"strength": "physical", "intelligence": "primitive"},
                    "background_elements": ["lives in cave", "hunts mammoth", "uses primitive tools"],
                },
                "big_rob": {
                    "bot_name": "Big Rob",
                    "response_style": "british_working_class",
                    "personality_traits": {"strength": "opiniated", "intelligence": "street_smart"},
                    "background_elements": ["British working class", "football fan", "strong opinions"],
                },
            }
        )
    except Exception as e:
        log.debug("Could not access bot manager", extra={"error": str(e)})

    return personality_info


def get_cross_bot_memories(statement: str, server_id: str, server_manager, current_bot_id: str = None):
    """Get memories from other bots in the same server for context.

    Args:
        statement: The statement/query to search for relevant memories
        server_id: ID of the server to search in
        server_manager: GrugServerManager instance for accessing bot databases
        current_bot_id: ID of the current bot (to exclude its own memories)

    Returns:
        str: Formatted string containing relevant memories from other bots
    """
    try:
        # Import here to avoid circular imports
        from ..grug_db import GrugServerManager

        # Try to access other bot databases if available
        # This will only work in multi-bot mode where we can access the bot manager
        cross_bot_context = ""

        # Get personality information about other bots
        personality_info = get_cross_bot_personality_info(server_id)

        # For now, we'll implement a simple system that looks for other bot data directories
        # In a more advanced implementation, this could access the BotManager directly
        data_base_dir = (
            os.path.dirname(server_manager.base_db_path) if hasattr(server_manager, "base_db_path") else "./data"
        )

        if os.path.exists(data_base_dir):
            for bot_dir in os.listdir(data_base_dir):
                bot_data_path = os.path.join(data_base_dir, bot_dir)
                if os.path.isdir(bot_data_path) and bot_dir != current_bot_id:
                    facts_db_path = os.path.join(bot_data_path, "facts.db")
                    if os.path.exists(facts_db_path):
                        temp_manager = None
                        try:
                            # Create a temporary server manager to access other bot's memories
                            temp_manager = GrugServerManager(facts_db_path)
                            temp_db = temp_manager.get_server_db(server_id)
                            relevant_facts = temp_db.search_facts(statement, k=2)
                            if relevant_facts:
                                bot_info = ""
                                # Add personality context if available
                                # Try multiple ways to match bot identity
                                matched_personality = None

                                # Try exact bot_dir match first
                                if bot_dir in personality_info:
                                    matched_personality = personality_info[bot_dir]
                                else:
                                    # Try matching by common bot name patterns
                                    bot_dir_lower = bot_dir.lower()
                                    for key in personality_info:
                                        if (
                                            key in bot_dir_lower
                                            or bot_dir_lower in key
                                            or (key == "grug" and "grug" in bot_dir_lower)
                                            or (key == "big_rob" and ("rob" in bot_dir_lower or "big" in bot_dir_lower))
                                        ):
                                            matched_personality = personality_info[key]
                                            break

                                if matched_personality:
                                    style = matched_personality.get("response_style", "")
                                    traits = matched_personality.get("personality_traits", {})
                                    bot_name = matched_personality.get("bot_name", bot_dir)

                                    if style == "caveman":
                                        bot_info = f" ({bot_name} - caveman who fights sabertooths and hunts mammoth)"
                                    elif style == "british_working_class":
                                        bot_info = f" ({bot_name} - British working class lad with football opinions)"
                                    elif style == "adaptive":
                                        bot_info = f" ({bot_name} - adaptive bot that learns and evolves)"
                                    else:
                                        bot_info = f" ({bot_name})"

                                    # Add specific traits if they exist
                                    if traits:
                                        key_traits = []
                                        if "strength" in traits:
                                            key_traits.append(f"strength: {traits['strength']}")
                                        if "intelligence" in traits:
                                            key_traits.append(f"smarts: {traits['intelligence']}")
                                        if key_traits:
                                            bot_info += f" [{', '.join(key_traits)}]"

                                cross_bot_context += f" {bot_dir}{bot_info} remembers: {' '.join(relevant_facts[:2])}"
                        except Exception as e:
                            log.debug(
                                "Could not access cross-bot memories", extra={"bot_dir": bot_dir, "error": str(e)}
                            )
                        finally:
                            if temp_manager is not None:
                                temp_manager.close_all()

        return cross_bot_context.strip()
    except Exception as e:
        log.debug("Cross-bot memory access failed", extra={"error": str(e)})
        return ""


def detect_cross_bot_mentions(message):
    """Detect mentions of other bot names in a message.

    Args:
        message: Discord message object to check for bot mentions

    Returns:
        list: Names of bots mentioned in the message
    """
    mentioned_bots = []
    content_lower = message.content.lower()

    # Common bot names to look for (more comprehensive list)
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
            mentioned_bots.append(bot_name)
            continue

        # Check variations
        for main_name, variations in name_variations.items():
            if bot_name == main_name:
                for variation in variations:
                    if re.search(rf"\b{re.escape(variation.lower())}\b", content_lower):
                        mentioned_bots.append(bot_name)
                        break

    return list(set(mentioned_bots))  # Remove duplicates


def store_cross_bot_mention(mentioning_source: str, mentioned_bot_names: list, message, cross_bot_mentions):
    """Store cross-bot mentions for later reference.

    Args:
        mentioning_source: Name of the bot or user making the mention
        mentioned_bot_names: List of bot names that were mentioned
        message: Discord message object containing the mention
        cross_bot_mentions: LRUCache instance for storing mentions

    Returns:
        None
    """
    server_id = str(message.guild.id) if message.guild else "dm"
    channel_id = str(message.channel.id)

    for mentioned_bot in mentioned_bot_names:
        # Normalize the mentioned bot name
        mentioned_bot_normalized = mentioned_bot.lower()

        # Create a simpler key structure for easier retrieval
        mention_key = f"{server_id}:{channel_id}:{mentioned_bot_normalized}:{int(time.time())}"
        mention_data = {
            "mentioning_bot": mentioning_source,
            "mentioned_bot": mentioned_bot_normalized,
            "message_content": message.content,
            "message_id": message.id,
            "channel_id": channel_id,
            "server_id": server_id,
            "timestamp": time.time(),
        }
        cross_bot_mentions.put(mention_key, mention_data)

        log.info(
            "Cross-bot mention stored",
            extra={
                "mentioning_source": mentioning_source,
                "mentioned_bot": mentioned_bot_normalized,
                "server_id": server_id,
                "channel_id": channel_id,
                "mention_key": mention_key,
                "message_content": message.content[:100],
                "total_mentions_cached": len(cross_bot_mentions.cache),
            },
        )


def get_recent_mentions_about_bot(bot_name: str, server_id: str, channel_id: str, cross_bot_mentions) -> list:
    """Get recent mentions about this bot from other sources.

    Args:
        bot_name: Name of the bot to find mentions about
        server_id: Server ID to search in
        channel_id: Channel ID to search in
        cross_bot_mentions: LRUCache instance containing mention data

    Returns:
        list: Up to 3 most recent mention data dictionaries
    """
    mentions = []
    bot_name_lower = bot_name.lower()

    # Also check for name variations
    name_to_check = [bot_name_lower]
    if "rob" in bot_name_lower:
        name_to_check.extend(["big rob", "rob"])
    elif "grug" in bot_name_lower:
        name_to_check.extend(["grug", "grugthink"])

    log.info(
        "Checking for cross-bot mentions",
        extra={
            "bot_name": bot_name,
            "names_to_check": name_to_check,
            "server_id": server_id,
            "channel_id": channel_id,
            "cache_size": len(cross_bot_mentions.cache),
        },
    )

    # Check all stored mentions for ones about this bot
    for key, mention_data in cross_bot_mentions.cache.items():
        if mention_data and isinstance(mention_data, tuple):
            _, data = mention_data
            mentioned_bot = data.get("mentioned_bot", "").lower()

            if (
                any(name in mentioned_bot or mentioned_bot in name for name in name_to_check)
                and data.get("server_id") == server_id
                and data.get("channel_id") == channel_id
            ):
                mentions.append(data)
                log.info(
                    "Found cross-bot mention",
                    extra={
                        "mentioned_bot": data.get("mentioned_bot"),
                        "mentioning_source": data.get("mentioning_bot"),
                        "content": data.get("message_content", "")[:100],
                    },
                )

    # Sort by timestamp, most recent first
    mentions.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return mentions[:3]  # Return up to 3 most recent mentions


def get_cross_bot_topic_context(statement: str, current_bot_name: str, cross_bot_topic_responses):
    """Get context from other bots about topics mentioned in the statement.

    Args:
        statement: The statement to check for relevant topics
        current_bot_name: Name of the current bot (to exclude its own responses)
        cross_bot_topic_responses: LRUCache instance containing topic-based responses

    Returns:
        str: Formatted context string from other bots about the topic, or empty string
    """
    statement_lower = statement.lower()
    current_bot_lower = current_bot_name.lower()

    # Topics to check for
    topic_keywords = {
        "carling": ["carling", "beer", "drink", "pint"],
        "beer": ["beer", "carling", "drink", "pint", "ale"],
        "food": ["pie", "potato", "shepherd", "meat", "food", "grub"],
        "pie": ["pie", "potato", "shepherd", "meat", "food"],
        "fight": ["fight", "beat", "strong", "tough", "battle"],
        "football": ["football", "footy", "norf", "fc", "team"],
        "caveman": ["caveman", "mammoth", "cave", "stone", "hunt"],
    }

    # Check which topics this statement relates to
    relevant_topics = []
    for topic, keywords in topic_keywords.items():
        if any(keyword in statement_lower for keyword in keywords):
            relevant_topics.append(topic)

    if not relevant_topics:
        return ""

    # Look for responses from other bots about these topics
    other_bot_responses = []
    for topic in relevant_topics:
        for key, topic_data in cross_bot_topic_responses.cache.items():
            if topic_data and isinstance(topic_data, tuple):
                _, data = topic_data
                if data.get("topic") == topic and data.get("bot_name", "").lower() != current_bot_lower:
                    other_bot_responses.append(data)

    if not other_bot_responses:
        return ""

    # Sort by timestamp and get the most recent response
    other_bot_responses.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    latest_response = other_bot_responses[0]

    other_bot_name = latest_response.get("bot_name", "another bot")
    other_response = latest_response.get("response", "")

    log.info(
        "Found cross-bot topic context",
        extra={
            "current_bot": current_bot_name,
            "other_bot": other_bot_name,
            "topic": latest_response.get("topic"),
            "other_response": other_response[:100],
        },
    )

    # Format context based on current bot's personality
    if "grug" in current_bot_lower:
        return f" Grug hear {other_bot_name} say: '{other_response[:80]}'. "
    elif "rob" in current_bot_lower:
        return f" Heard {other_bot_name} chattin: '{other_response[:80]}' - "
    else:
        return f" {other_bot_name} said: '{other_response[:80]}'. "


def detect_cross_bot_mentions_in_text(text: str) -> list:
    """Detect mentions of other bot names in text content.

    Args:
        text: Text content to check for bot mentions

    Returns:
        list: Names of bots mentioned in the text
    """
    mentioned_bots = []
    content_lower = text.lower()

    # Common bot names to look for (more comprehensive list)
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
            mentioned_bots.append(bot_name)
            continue

        # Check variations
        for main_name, variations in name_variations.items():
            if bot_name == main_name:
                for variation in variations:
                    if re.search(rf"\b{re.escape(variation.lower())}\b", content_lower):
                        mentioned_bots.append(bot_name)
                        break

    return list(set(mentioned_bots))  # Remove duplicates


async def get_cross_bot_context(bot_instance, server_id):
    """Get context about other bots and their memories for conversation.

    Args:
        bot_instance: The bot instance requesting context (must have bot_manager attribute)
        server_id: Server ID to get context for

    Returns:
        str: Formatted context string about other bots and their memories
    """
    if not bot_instance.bot_manager:
        return ""

    context_parts = []

    try:
        # Get other active bots in this server
        other_bots = []
        for bot_id, other_bot_instance in bot_instance.bot_manager.bots.items():
            if (
                bot_id != bot_instance.get_bot_id()
                and other_bot_instance.runtime_status == "running"
                and hasattr(other_bot_instance, "config")
            ):
                bot_config = other_bot_instance.config
                personality_id = getattr(bot_config, "personality", None) or getattr(
                    bot_config, "force_personality", None
                )
                other_bots.append({"name": bot_config.name, "personality": personality_id, "bot_id": bot_id})

        if other_bots:
            context_parts.append("Other bots in this server:")
            for bot in other_bots[:3]:  # Limit to 3 bots to avoid token explosion
                # Try to get some memories about this bot
                bot_memories = await get_bot_memories_summary(bot_instance, bot["bot_id"], server_id)
                context_parts.append(f"- {bot['name']} ({bot['personality']}): {bot_memories}")

    except Exception as e:
        log.warning(f"Failed to get cross-bot context: {e}")
        return ""

    return "\n".join(context_parts) if context_parts else ""


async def get_bot_memories_summary(bot_instance, bot_id, server_id, limit=3):
    """Get a summary of another bot's interesting memories.

    Args:
        bot_instance: The bot instance requesting memories (must have bot_manager)
        bot_id: ID of the bot to get memories from
        server_id: Server ID to get memories for
        limit: Maximum number of memories to return (default: 3)

    Returns:
        str: Summary of interesting memories or "No recent memories."
    """
    try:
        if not bot_instance.bot_manager or bot_id not in bot_instance.bot_manager.bots:
            return "No recent memories."

        other_bot_instance = bot_instance.bot_manager.bots[bot_id]
        if not hasattr(other_bot_instance, "server_manager"):
            return "No recent memories."

        # Access the other bot's database
        server_db = other_bot_instance.server_manager.get_or_create_server(
            int(server_id) if server_id.isdigit() else server_id
        )

        # Get recent interesting memories (facts about people, events, relationships)
        memories = server_db.search_lore("", limit=limit * 3)  # Get more to filter

        # Filter for interesting personal facts and relationships
        interesting_memories = []
        for memory in memories:
            content = memory.get("content", "").lower()
            # Look for personal facts, relationships, interests, etc.
            if any(
                keyword in content
                for keyword in [
                    "likes",
                    "dislikes",
                    "family",
                    "job",
                    "hobby",
                    "friend",
                    "relationship",
                    "birthday",
                    "lives",
                    "works",
                    "studies",
                    "plays",
                    "enjoys",
                ]
            ):
                interesting_memories.append(memory.get("content", ""))
                if len(interesting_memories) >= limit:
                    break

        return "; ".join(interesting_memories) if interesting_memories else "No recent personal memories."

    except Exception as e:
        log.warning(f"Failed to get bot memories for {bot_id}: {e}")
        return "No recent memories."
