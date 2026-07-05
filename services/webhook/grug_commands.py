"""Interactive `/grug <verb>` PR-comment command parser (#528, epic #522).

Pure: turn an issue-comment body into a (verb, arg) command or None. The
dispatcher gates (allowlist, collaborator, tpm-enabled) and executes; this
just recognizes the command, so the recognition has a unit-test seam.

Supported verbs (all default-safe - none mutate code):
  /grug recheck        re-run the DoR + review (pre-existing)
  /grug improve        Elder re-reviews and re-posts its findings
  /grug ask <question> scoped Q&A over the PR diff, answered as a reply
  /grug test-gaps      the pr-test lens on demand
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Anchored at line start (MULTILINE) so a command can appear on its own
# line in a longer comment; the verb is a known token, everything after is
# the arg (only `ask` uses it). Case-insensitive.
_CMD_RE = re.compile(
    r"^\s*/grug\s+(recheck|improve|ask|test-gaps|test_gaps)\b[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

_CANON = {"test_gaps": "test-gaps"}
VERBS = frozenset({"recheck", "improve", "ask", "test-gaps"})


@dataclass(frozen=True)
class GrugCommand:
    verb: str          # canonical verb (one of VERBS)
    arg: str = ""      # trailing text (only `ask` consumes it)


def parse_command(body: str) -> GrugCommand | None:
    """First `/grug <verb>` in the comment, or None. If several appear, the
    first wins (a comment is one intent)."""
    m = _CMD_RE.search(body or "")
    if not m:
        return None
    verb = m.group(1).lower()
    verb = _CANON.get(verb, verb)
    return GrugCommand(verb=verb, arg=m.group(2).strip())
