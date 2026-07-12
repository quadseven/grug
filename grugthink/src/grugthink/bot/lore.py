"""Lore extraction and filtering functions for GrugThink bot.

This module handles extraction of factual information from bot responses,
including filtering out emotional/defensive responses and identifying
meaningful facts worth storing in the bot's knowledge base.
"""

import re

from ..logging_config import get_logger

log = get_logger(__name__)


def extract_lore_from_response(response: str, server_db, personality_name: str = None):
    """Extract and save new factual lore to bot's brain, filtering out emotional responses.

    Args:
        response: The bot's response text to extract facts from
        server_db: Server database instance for storing lore
        personality_name: Name of the bot personality (optional)
    """
    try:
        # Extract all sentences from the response after TRUE/FALSE verdict
        # Split on TRUE/FALSE and take everything after the dash
        parts = re.split(r"\b(TRUE|FALSE)\s*[-–—:]\s*", response, flags=re.IGNORECASE)
        if len(parts) >= 3:
            # Extract the explanation part after TRUE/FALSE -
            explanation = parts[2].strip()
            lore_sentences = re.findall(r"[^.!?]+[.!?]", explanation)
        else:
            # Fallback: extract all sentences
            lore_sentences = re.findall(r"[^.!?]+[.!?]", response)

        for sentence in lore_sentences:
            sentence_clean = sentence.strip().capitalize()
            if sentence_clean and len(sentence_clean) > 15:
                # Enhanced filtering for better fact quality
                if not _is_factual_content(sentence_clean):
                    continue

                # Extract and enhance family relationships
                family_fact = _extract_family_relationships(sentence_clean, personality_name)
                if family_fact:
                    sentence_clean = family_fact

                # Skip if it's just filler words
                meaningful_words = _get_meaningful_words(sentence_clean)
                if len(meaningful_words) < 3:
                    continue

                # Add context about who said it if we have personality name
                if personality_name and personality_name.lower() not in sentence_clean.lower():
                    contextual_sentence = f"{personality_name} says: {sentence_clean}"
                else:
                    contextual_sentence = sentence_clean

                if server_db.add_fact(contextual_sentence):
                    log.debug("New factual lore learned", extra={"lore": contextual_sentence})
                else:
                    log.debug("Factual lore already known", extra={"lore": contextual_sentence})
    except Exception as e:
        log.error("Error extracting lore", extra={"error": str(e)})


def _is_factual_content(sentence: str) -> bool:
    """Determine if a sentence contains factual information worth storing.

    Args:
        sentence: The sentence to evaluate

    Returns:
        True if the sentence contains factual content, False otherwise
    """
    sentence_lower = sentence.lower()

    # Skip defensive/emotional responses
    defensive_patterns = [
        "mind yer own business",
        "don't be askin",
        "listen 'ere",
        "ya nosy",
        "ask about",
        "problem",
        "nuff said",
        "simple as",
        "end of",
        "innit",
        "don't appreciate",
        "ya git",
        "shut up",
        "go away",
        "leave me alone",
    ]
    if any(pattern in sentence_lower for pattern in defensive_patterns):
        return False

    # Skip generic responses
    generic_patterns = [
        "dunno wot yer on about",
        "whatever",
        "i don't care",
        "who cares",
        "that's nice",
        "good for you",
        "so what",
    ]
    if any(pattern in sentence_lower for pattern in generic_patterns):
        return False

    # Prioritize factual indicators
    factual_indicators = [
        "is",
        "was",
        "are",
        "were",
        "called",
        "named",
        "lives",
        "works",
        "born",
        "from",
        "in",
        "at",
        "daughter",
        "son",
        "wife",
        "husband",
        "father",
        "mother",
        "brother",
        "sister",
        "grandkid",
        "grandson",
        "granddaughter",
        "plays for",
        "team",
        "footballer",
        "years old",
    ]
    has_factual_content = any(indicator in sentence_lower for indicator in factual_indicators)

    return has_factual_content


def _extract_family_relationships(sentence: str, personality_name: str = None) -> str:
    """Extract and standardize family relationship information.

    Args:
        sentence: The sentence to extract relationship information from
        personality_name: Name of the bot personality (optional)

    Returns:
        Standardized sentence with clear relationship information, or original sentence
    """
    sentence_lower = sentence.lower()

    # Family relationship patterns with standardization
    patterns = [
        # Daughter relationships
        (r"(?:me |my )?daughter['']?s?(?:'s)?\s+(?:called|named|is)\s+(\w+)", r"has daughter named \1"),
        (r"(\w+)(?:'s| is)?\s+(?:me |my )?daughter", r"has daughter named \1"),
        # Son/grandson relationships
        (r"(?:me |my )?(?:grandkid|grandson)(?:'s)?\s+(?:called|named|is)\s+(\w+)", r"has grandson named \1"),
        (r"(\w+)(?:'s| is)?\s+(?:me |my )?(?:grandkid|grandson)", r"has grandson named \1"),
        (r"she named (?:him|'im)\s+(\w+)", r"grandson is named \1"),
        # General family
        (r"(\w+)(?:'s| is)?\s+(?:me |my )?(\w+)", r"has \2 named \1"),
    ]

    for pattern, replacement in patterns:
        match = re.search(pattern, sentence_lower)
        if match:
            # Extract the name and relationship
            if len(match.groups()) >= 1:
                result = re.sub(pattern, replacement, sentence_lower)

                # Clean up the result
                result = result.replace("me ", "").replace("my ", "")
                if personality_name:
                    result = f"{personality_name} {result}"

                return result.capitalize()

    return sentence


def _get_meaningful_words(sentence: str) -> list:
    """Extract meaningful words from a sentence, excluding filler words.

    Args:
        sentence: The sentence to analyze

    Returns:
        List of meaningful words (non-filler words)
    """
    filler_words = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "of",
        "for",
        "to",
        "in",
        "on",
        "it",
        "that",
        "this",
        "and",
        "but",
        "or",
        "so",
        "if",
    }
    return [word for word in sentence.lower().split() if word not in filler_words]
