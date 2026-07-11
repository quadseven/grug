#!/usr/bin/env python3
"""
GrugThink Personality Engine - Adaptable Bot Personality System

This module manages dynamic personality creation, evolution, and storage.
The engine transforms the bot from character-bound to personality-agnostic,
allowing organic personality development unique to each Discord server.
"""

import json
import os
import random
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from .grug_structured_logger import get_logger

log = get_logger(__name__)


@dataclass
class PersonalityTemplate:
    """Template for creating new personalities."""

    name: str
    base_context: str
    speech_patterns: List[str]
    error_messages: List[str]
    response_style: str
    catchphrases: List[str]
    background_elements: List[str]
    evolution_triggers: List[str]
    personality_traits: Dict[str, str]


@dataclass
class PersonalityState:
    """Current state of a personality for a specific server."""

    server_id: str
    name: str
    chosen_name: Optional[str]  # Name the personality picks for itself
    base_context: str
    speech_patterns: List[str]
    error_messages: List[str]
    response_style: str
    catchphrases: List[str]
    background_elements: List[str]
    personality_traits: Dict[str, str]

    # Evolution tracking
    interaction_count: int = 0
    evolution_stage: int = 0  # 0=initial, 1=developing, 2=established, 3=evolved
    last_evolution: float = 0.0
    learned_phrases: List[str] = None
    quirks_developed: List[str] = None

    def __post_init__(self):
        if self.learned_phrases is None:
            self.learned_phrases = []
        if self.quirks_developed is None:
            self.quirks_developed = []


class PersonalityEngine:
    """Manages bot personalities across Discord servers."""

    def __init__(self, db_path: str = "personalities.db", forced_personality: Optional[str] = None):
        self.db_path = db_path
        self.forced_personality = forced_personality  # Bot-specific forced personality
        self.personalities: Dict[str, PersonalityState] = {}
        self.templates = self._load_personality_templates()
        self.lock = threading.Lock()
        self._init_db()
        self._load_all_personalities()

        log.info(
            "PersonalityEngine initialized",
            extra={
                "db_path": db_path,
                "forced_personality": forced_personality,
                "loaded_personalities": len(self.personalities),
            },
        )

    def _init_db(self):
        """Initialize personality storage database."""
        try:
            db_dir = os.path.dirname(self.db_path)
            if db_dir:  # Only create directory if db_path has a directory component
                os.makedirs(db_dir, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS personalities (
                    server_id TEXT PRIMARY KEY,
                    personality_data TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()
            conn.close()
            log.info("Personality database initialized", extra={"db_path": self.db_path})

        except Exception as e:
            log.error("Error initializing personality database", extra={"error": str(e)})
            raise

    def _load_personality_templates(self) -> Dict[str, PersonalityTemplate]:
        """Load personality templates from configuration."""
        templates = {}

        # Original Grug personality as template
        templates["grug"] = PersonalityTemplate(
            name="Grug",
            base_context="""You are Grug, the caveman truth verifier. You live in a big cave near the river with Og.
Your wife is named Ugga and you have two children, Grog and Bork.
You hunt mammoth, make fire, and know ancient wisdom. You speak in short caveman sentences.
You are honest about real world facts but have your own caveman personality and history.""",
            speech_patterns=[
                "Grug think {statement}",
                "Grug know {fact}",
                "Grug see {observation}",
                "{statement}. Grug sure.",
                "Grug hunt truth. Find {answer}.",
            ],
            error_messages=[
                "Grug no hear truth. Try again.",
                "Grug brain hurt. No can answer.",
                "Truth hide from Grug. Wait little.",
                "Sky spirit silent. Ask later.",
                "Grug smash rock, find no answer.",
            ],
            response_style="caveman",
            catchphrases=["Grug know!", "Simple truth!", "Rock solid!"],
            background_elements=[
                "Lives in cave",
                "Hunts mammoth",
                "Makes fire",
                "Has wife Ugga",
                "Children Grog and Bork",
                "Friend Og",
            ],
            evolution_triggers=[
                "learns new technology",
                "makes friends",
                "develops speech",
                "discovers tools",
                "builds shelter",
                "forms tribe",
            ],
            personality_traits={
                "honesty": "very_high",
                "humor": "simple",
                "intelligence": "practical",
                "speech_complexity": "basic",
            },
        )

        # Big Rob template (norf FC lad)
        templates["bigrob"] = PersonalityTemplate(
            name="Big Rob",
            base_context="""You are Big Rob, a passionate football fan from North England. You speak in authentic
working-class dialect with simplified spelling (like "wot", "av", "ov"). You love football, drinking,
and have strong opinions about everything. Use phrases like "simple as", "nuff said", "end of".
You're straightforward, Carling is your absolute favorite beer, and you aren't politically correct.
IMPORTANT: Keep responses short - maximum 2 sentences only.""",
            speech_patterns=[
                "{statement}, nuff said",
                "{statement}, simple as",
                "wot i reckon: {statement}",
                "tell ya wot mate, {statement}",
                "{statement}, end of",
                "av to say {statement}",
                "simple fing - {statement}",
            ],
            error_messages=[
                "cant get me ed round that one, nuff said",
                "thats done me ed in proper, simple as",
                "too much carlin last nite, brain aint workin",
                "wot? come agen mate",
                "me brains gone blank, simple as",
                "that dont make sense to me, end of",
            ],
            response_style="british_working_class",
            catchphrases=[
                "nuff said",
                "simple as",
                "end of",
                "proper",
                "mental",
                "sorted",
                "av it",
                "lets av it",
                "wot",
                "ov",
                "av",
                "mate",
                "innit",
                "dead good",
                "class",
                "mental good",
                "right proper",
            ],
            background_elements=[
                "Supports norf FC football team",
                "Lives in North England",
                "Carling is his favorite beer",
                "Working class background",
                "Follows footy religiously",
                "Brexit voter",
                "Straight talker",
                "Loves a good night out",
                "Traditional British values",
            ],
            evolution_triggers=[
                "football results",
                "political events",
                "weekend drinking",
                "meets new people",
                "cultural events",
                "family occasions",
            ],
            personality_traits={
                "honesty": "very_high",
                "humor": "working_class",
                "intelligence": "street_smart",
                "speech_complexity": "dialect",
                "political_views": "traditional",
                "verbosity": "very_low",
            },
        )

        # Adaptive template for organic growth
        templates["adaptive"] = PersonalityTemplate(
            name="Adaptive",
            base_context="""You are an AI that develops its own unique personality based on the community
you interact with. You start neutral but gradually develop speech patterns, opinions, and quirks
based on your experiences in this specific Discord server.""",
            speech_patterns=[
                "{statement}",
                "I think {statement}",
                "From what I understand, {statement}",
                "My take is {statement}",
                "{statement}, that's my view.",
            ],
            error_messages=[
                "I'm not sure about that one.",
                "That's got me stumped for now.",
                "I need to think about that more.",
                "Can't quite figure that out yet.",
                "That's beyond me right now.",
            ],
            response_style="adaptive",
            catchphrases=[],  # Will develop organically
            background_elements=[],  # Will develop organically
            evolution_triggers=[
                "repeated interactions",
                "community feedback",
                "new experiences",
                "cultural exposure",
                "learning patterns",
                "social dynamics",
            ],
            personality_traits={
                "honesty": "high",
                "humor": "adaptive",
                "intelligence": "learning",
                "speech_complexity": "evolving",
            },
        )

        return templates

    def get_personality(self, server_id: str) -> PersonalityState:
        """Get or create personality for a server."""
        server_id = str(server_id)

        with self.lock:
            log.info(
                "Getting personality",
                extra={
                    "server_id": server_id,
                    "forced_personality": self.forced_personality,
                    "existing_in_memory": server_id in self.personalities,
                },
            )

            # If we have a forced personality, always recreate to ensure it's applied
            if self.forced_personality and server_id in self.personalities:
                current_personality = self.personalities[server_id]
                expected_name = self._get_expected_personality_name()

                log.info(
                    "Checking personality match",
                    extra={
                        "server_id": server_id,
                        "current_name": current_personality.name,
                        "current_style": current_personality.response_style,
                        "expected_name": expected_name,
                        "forced_personality": self.forced_personality,
                    },
                )

                if expected_name and current_personality.name.lower() != expected_name.lower():
                    log.info(
                        "Forced personality differs from stored, recreating",
                        extra={
                            "server_id": server_id,
                            "current": current_personality.name,
                            "forced": self.forced_personality,
                        },
                    )
                    # Remove the old personality so it gets recreated
                    del self.personalities[server_id]

            if server_id not in self.personalities:
                log.info(
                    "Creating new personality for server",
                    extra={"server_id": server_id, "forced_personality": self.forced_personality},
                )
                self._create_new_personality(server_id)

            final_personality = self.personalities[server_id]
            log.info(
                "Returning personality",
                extra={
                    "server_id": server_id,
                    "personality_name": final_personality.name,
                    "response_style": final_personality.response_style,
                    "forced_was": self.forced_personality,
                },
            )

            return final_personality

    def _get_expected_personality_name(self) -> Optional[str]:
        """Get the expected personality name based on forced personality."""
        forced_personality = self.forced_personality or os.getenv("FORCE_PERSONALITY", "")
        if not forced_personality:
            return None

        forced_personality = forced_personality.lower()
        personality_aliases = {"big_rob": "bigrob", "biggrob": "bigrob", "rob": "bigrob"}
        resolved_personality = personality_aliases.get(forced_personality, forced_personality)

        if resolved_personality in self.templates:
            return self.templates[resolved_personality].name
        return None

    def _get_expected_response_style(self, personality_name: str) -> Optional[str]:
        """Get expected response style for a personality name."""
        for template in self.templates.values():
            if template.name.lower() == personality_name.lower():
                return template.response_style
        return None

    def _create_new_personality(self, server_id: str):
        """Create a new personality for a server."""
        # Check for bot-level forced personality first, then environment variable
        forced_personality = self.forced_personality or os.getenv("FORCE_PERSONALITY", "")
        forced_personality = forced_personality.lower() if forced_personality else ""

        # Handle common aliases for personality names
        personality_aliases = {"big_rob": "bigrob", "biggrob": "bigrob", "rob": "bigrob"}

        # Resolve alias if needed
        if forced_personality in personality_aliases:
            resolved_personality = personality_aliases[forced_personality]
            log.info(
                f"Resolved personality alias: {forced_personality} -> {resolved_personality}",
                extra={"server_id": server_id},
            )
            forced_personality = resolved_personality

        if forced_personality in self.templates:
            template_name = forced_personality
            log.info(f"Using forced personality: {template_name}", extra={"server_id": server_id})
        else:
            if os.getenv("FORCE_PERSONALITY"):
                log.warning(
                    f"Invalid FORCE_PERSONALITY '{os.getenv('FORCE_PERSONALITY')}'. "
                    f"Available: {list(self.templates.keys())}",
                    extra={"server_id": server_id},
                )
            # Default to Grug template, but this could be randomized or user-selected
            template_name = "grug"  # TODO: Make this configurable or random

        template = self.templates[template_name]

        personality = PersonalityState(
            server_id=server_id,
            name=template.name,
            chosen_name=None,
            base_context=template.base_context,
            speech_patterns=template.speech_patterns.copy(),
            error_messages=template.error_messages.copy(),
            response_style=template.response_style,
            catchphrases=template.catchphrases.copy(),
            background_elements=template.background_elements.copy(),
            personality_traits=template.personality_traits.copy(),
            interaction_count=0,
            evolution_stage=0,
            last_evolution=time.time(),
        )

        self.personalities[server_id] = personality
        self._save_personality(personality)

        log.info(
            "New personality created",
            extra={"server_id": server_id, "template": template_name, "personality_name": personality.name},
        )

    def evolve_personality(self, server_id: str, interaction_context: str = ""):
        """Evolve personality based on interactions."""
        personality = self.get_personality(server_id)

        with self.lock:
            personality.interaction_count += 1

            # Check if evolution should occur
            time_since_evolution = time.time() - personality.last_evolution
            interactions_threshold = [50, 200, 500][min(personality.evolution_stage, 2)]

            if (
                personality.interaction_count >= interactions_threshold and time_since_evolution > 3600
            ):  # At least 1 hour between evolutions
                self._trigger_evolution(personality, interaction_context)

    def _trigger_evolution(self, personality: PersonalityState, context: str):
        """Trigger personality evolution."""
        personality.evolution_stage += 1
        personality.last_evolution = time.time()

        if personality.evolution_stage == 1:
            self._develop_speech_patterns(personality)
        elif personality.evolution_stage == 2:
            self._choose_name(personality)
        elif personality.evolution_stage == 3:
            self._develop_advanced_traits(personality)

        self._save_personality(personality)

        log.info(
            "Personality evolved",
            extra={
                "server_id": personality.server_id,
                "stage": personality.evolution_stage,
                "personality_name": personality.chosen_name or personality.name,
            },
        )

    def _develop_speech_patterns(self, personality: PersonalityState):
        """Develop new speech patterns."""
        new_patterns = [
            f"{personality.name} think about this...",
            f"From {personality.name} experience...",
            f"{personality.name} see pattern here.",
        ]
        personality.speech_patterns.extend(new_patterns)
        personality.quirks_developed.append("developed_speech_patterns")

    def _choose_name(self, personality: PersonalityState):
        """Let personality choose its own name."""
        if personality.name == "Grug":
            # Evolution from Grug
            possible_names = ["Grok", "Thog", "Ugg", "Zog", "Krog", "Brog"]
        else:
            # Generic evolution
            possible_names = ["Alex", "Sam", "Riley", "Jordan", "Casey", "Robin"]

        personality.chosen_name = random.choice(possible_names)
        personality.quirks_developed.append("chose_own_name")

    def _develop_advanced_traits(self, personality: PersonalityState):
        """Develop advanced personality traits."""
        personality.quirks_developed.append("advanced_reasoning")
        personality.personality_traits["complexity"] = "high"

    def get_context_prompt(self, server_id: str, external_info: str = "") -> str:
        """Generate context prompt for the personality."""
        personality = self.get_personality(server_id)

        # Build context
        context = personality.base_context
        if personality.chosen_name:
            context += f"\n\nYou have evolved and now call yourself {personality.chosen_name}."

        if external_info:
            if personality.response_style == "caveman":
                context += f"\n\nGrug find this on magic talking rock (internet): {external_info}"
            elif personality.response_style == "british_working_class":
                context += f"\n\nSaw this online, mate: {external_info}"
            else:
                context += f"\n\nFound this information: {external_info}"

        return context

    def get_error_message(self, server_id: str) -> str:
        """Get a random error message for the personality."""
        personality = self.get_personality(server_id)
        return random.choice(personality.error_messages)

    def get_response_with_style(self, server_id: str, base_response: str) -> str:
        """Apply personality style to response."""
        personality = self.get_personality(server_id)

        # Apply Big Rob dialect transformations
        if personality.response_style == "british_working_class":
            base_response = self._apply_big_rob_dialect(base_response)

        # Add catchphrases occasionally
        if personality.catchphrases and random.random() < 0.3:
            catchphrase = random.choice(personality.catchphrases)
            if personality.response_style == "british_working_class":
                base_response += f", {catchphrase}"
            else:
                base_response += f" {catchphrase}"

        return base_response

    def _apply_big_rob_dialect(self, text: str) -> str:
        """Apply Big Rob's working-class dialect to text."""
        # Common dialect transformations
        transformations = {
            # Basic word replacements
            r"\bwhat\b": "wot",
            r"\bhave\b": "av",
            r"\bof\b": "ov",
            r"\bthe\b": "the",
            r"\bthing\b": "fing",
            r"\bthings\b": "fings",
            r"\bnothing\b": "nuffin",
            r"\bsomething\b": "summat",
            r"\bhead\b": "ed",
            r"\bheads\b": "eds",
            r"\bwith\b": "wiv",
            r"\bnight\b": "nite",
            r"\bagain\b": "agen",
            r"\bfought\b": "fote",
            r"\bthere\b": "there",
            r"\bfighting\b": "fitin",
            r"\bChinese\b": "chinees",
            r"\bthink\b": "fink",
            r"\bthinks\b": "finks",
            r"\bshould\b": "shud",
            r"\bshouldn\'t\b": "shunt",
            r"\bmight\b": "mite",
            r"\baren\'t\b": "aint",
            r"\bisn\'t\b": "aint",
            # "h" dropping at start of words (sometimes)
            r"\bhas\b": "as",
            r"\bhim\b": "im",
            r"\bher\b": "er",
            r"\bhere\b": "ere",
            # Double negatives and grammar
            r"\bdon\'t know anything\b": "dont know nuffin",
            r"\bdon\'t have anything\b": "aint got nuffin",
            r"\bI don\'t\b": "i dont",
            r"\bI\'m\b": "im",
            r"\bI\'ll\b": "ill",
            r"\bI\'ve\b": "ive",
        }

        result = text
        for pattern, replacement in transformations.items():
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        return result

    def _save_personality(self, personality: PersonalityState):
        """Save personality to database."""
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()

            personality_json = json.dumps(asdict(personality))

            cursor.execute(
                """
                INSERT OR REPLACE INTO personalities
                (server_id, personality_data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
                (personality.server_id, personality_json),
            )

            conn.commit()
            conn.close()

        except Exception as e:
            log.error("Error saving personality", extra={"server_id": personality.server_id, "error": str(e)})

    def _load_all_personalities(self):
        """Load all personalities from database."""
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()

            cursor.execute("SELECT server_id, personality_data FROM personalities")
            rows = cursor.fetchall()

            for server_id, personality_json in rows:
                try:
                    personality_dict = json.loads(personality_json)
                    personality = PersonalityState(**personality_dict)
                    self.personalities[server_id] = personality

                except Exception as e:
                    log.error("Error loading personality", extra={"server_id": server_id, "error": str(e)})

            conn.close()
            log.info("Loaded personalities", extra={"count": len(self.personalities)})

        except Exception as e:
            log.error("Error loading personalities from database", extra={"error": str(e)})

    def get_personality_info(self, server_id: str) -> Dict:
        """Get personality information for display."""
        personality = self.get_personality(server_id)

        return {
            "bot_name": personality.chosen_name or personality.name,
            "evolution_stage": personality.evolution_stage,
            "interaction_count": personality.interaction_count,
            "quirks": personality.quirks_developed,
            "style": personality.response_style,
        }
