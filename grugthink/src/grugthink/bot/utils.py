"""Utility functions and classes for the GrugThink bot."""

import hashlib
import random
import re
import time
from collections import OrderedDict


class LRUCache:
    """Memory-bounded LRU cache with automatic expiration."""

    def __init__(self, max_size=100, ttl_seconds=300):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.cache = OrderedDict()

    def get(self, key):
        if key not in self.cache:
            return None
        timestamp, value = self.cache[key]
        if time.time() - timestamp > self.ttl_seconds:
            del self.cache[key]
            return None
        # Move to end (most recently used)
        self.cache.move_to_end(key)
        return value

    def put(self, key, value):
        now = time.time()
        if key in self.cache:
            self.cache[key] = (now, value)
            self.cache.move_to_end(key)
        else:
            self.cache[key] = (now, value)
            if len(self.cache) > self.max_size:
                # Remove oldest entry
                self.cache.popitem(last=False)


def clean_statement(text: str) -> str:
    """Clean statement by removing URLs and mentions."""
    text = re.sub(r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+", "", text)
    text = re.sub(r"<@[!&]?[0-9]+>", "", text)
    text = re.sub(r"<#[0-9]+>", "", text)
    text = " ".join(text.split())
    return text.strip()


def get_cache_key(statement: str, bot_id: str | None = None) -> str:
    """Return a cache key unique to the statement and bot."""
    key_source = f"{bot_id}:{statement}" if bot_id else statement
    return hashlib.md5(key_source.encode()).hexdigest()


def pair_key(name_a: str, name_b: str, server_id: str, channel_id: str) -> str:
    """Return a normalized key for tracking two bots interacting."""
    names = sorted([name_a.lower(), name_b.lower()])
    return f"{server_id}:{channel_id}:{names[0]}:{names[1]}"


def generate_shit_talk(target_name: str, style: str) -> str:
    """Return a short insult aimed at another bot."""
    target = target_name.strip()

    if style == "caveman":
        caveman_insults = [
            f"{target} weak. Grug strongest!",
            f"{target} soft like mammoth belly!",
            f"Grug smash {target} with big rock!",
            f"{target} no can hunt. Grug better!",
            f"{target} brain small like pebble!",
            f"Grug eat {target} for breakfast!",
            f"{target} weaker than sick woolly!",
            f"Grug club {target} into next cave!",
            f"{target} no know fire. Grug know fire!",
            f"{target} run from sabertooth. Grug fight sabertooth!",
            f"Grug throw {target} into tar pit!",
            f"{target} hide in cave like scared rabbit!",
        ]
        return random.choice(caveman_insults)

    elif style == "british_working_class":
        british_insults = [
            f"oi {target}, pipe down ya muppet",
            f"{target}'s a right tosser, innit",
            f"get stuffed {target}, you plonker",
            f"{target} couldn't organize a piss-up in a brewery",
            f"shut it {target}, you absolute weapon",
            f"{target}'s thick as two short planks",
            f"do one {target}, ya numpty",
            f"{target} talks pure waffle, simple as",
            f"wind your neck in {target}, you melt",
            f"{target}'s got more issues than a newsstand",
            f"bore off {target}, you proper div",
            f"{target} couldn't find water in a swimming pool",
        ]
        return random.choice(british_insults)

    elif style == "adaptive":
        adaptive_insults = [
            f"{target} clearly clueless",
            f"{target} needs a reality check",
            f"{target} talking nonsense again",
            f"{target} should stick to lurking",
            f"{target}'s logic is fundamentally flawed",
            f"{target} missed the point entirely",
            f"{target} needs to recalibrate their thinking",
            f"{target}'s analysis is rather shallow",
            f"{target} should consider alternative perspectives",
            f"{target}'s reasoning lacks nuance",
            f"{target} demonstrates poor comprehension",
            f"{target} fails to grasp the complexity here",
        ]
        return random.choice(adaptive_insults)

    # Default fallback insults for unknown styles
    default_insults = [
        f"{target} clearly clueless",
        f"{target} needs a reality check",
        f"{target} talking nonsense again",
        f"{target} should stick to lurking",
    ]
    return random.choice(default_insults)
