"""Redact secret values from config reads before they leave the API (grug#1631).

Every config read the dashboard serves goes through `redact_secrets`, so a
caller who reaches the Service (with or without a login) never reads a bot
token, an API key, the OAuth client secret or the session secret.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

# Keys under `api_keys` that describe an entry rather than hold its value.
_DESCRIPTIVE_KEYS = frozenset({"id", "name", "active", "added_at", "description"})

# Anywhere in the config: a key whose name says it holds a secret.
_SECRET_NAME = re.compile(r"(token|secret|password|passwd|api_?key|private_?key|credential)", re.IGNORECASE)


def _is_secret_name(key: str) -> bool:
    lowered = key.lower()
    # An id that points at a secret (`discord_token_id`) is a reference, not the value.
    if lowered.endswith("_id") or lowered.endswith("_ids"):
        return False
    return bool(_SECRET_NAME.search(key))


def _mask(value: Any) -> Any:
    if isinstance(value, str):
        return REDACTED if value else value
    return value


def _redact(node: Any, *, under_api_keys: bool) -> Any:
    if isinstance(node, dict):
        out: dict[Any, Any] = {}
        for key, value in node.items():
            name = str(key)
            child_under = under_api_keys or name == "api_keys"
            if isinstance(value, (dict, list)):
                out[key] = _redact(value, under_api_keys=child_under)
            elif under_api_keys and name not in _DESCRIPTIVE_KEYS:
                out[key] = _mask(value)
            elif _is_secret_name(name):
                out[key] = _mask(value)
            else:
                out[key] = value
        return out
    if isinstance(node, list):
        return [_redact(item, under_api_keys=under_api_keys) for item in node]
    return node


def redact_secrets(config: Any) -> Any:
    """Return a copy of `config` with every secret string value replaced.

    Every non-empty string under `api_keys` is masked except the descriptive
    fields of an entry (id, name, active, added_at, description); elsewhere a
    key whose name says token, secret, password or api key is masked. Empty
    strings and non-strings are kept, so the dashboard can still tell set
    from unset. The input is not modified.
    """
    return _redact(config, under_api_keys=False)
