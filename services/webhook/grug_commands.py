"""Interactive `/grug <verb>` PR-comment command parser (#528, epic #522).

Pure: turn an issue-comment body into a (verb, arg) command or None. The
dispatcher gates (allowlist, collaborator, tpm-enabled) and executes; this
just recognizes the command, so the recognition has a unit-test seam.

Supported verbs (all default-safe - none mutate code, except `learnings
delete` which forgets a taught preference and `fix` which commits to the
PR branch):
  /grug recheck        re-run Chief's DoR check (NOT Elder - use improve)
  /grug improve        Elder re-reviews and re-posts its findings
  /grug ask <question> scoped Q&A over the PR diff, answered as a reply
  /grug test-gaps      the pr-test lens on demand
  /grug resume         lift Elder's auto-pause and review since the last look
  /grug review         one Elder review now; an auto-pause stays (grug#1188)
  /grug learnings list list the repo's remembered markings (usage counts)
  /grug learnings delete <digest>  forget one remembered marking by digest
  /grug accept-removal <path> <sha>  accept one deleted guard path on head <sha> (#1405)
  /grug walkthrough    opt in to Teller walkthrough on this PR
  /grug fix            apply all unresolved committable suggestions as one bot commit
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Anchored at line start (MULTILINE) so a command can appear on its own
# line in a longer comment; the verb is a known token, everything after is
# the arg (only `ask`, `learnings` and `accept-removal` consume it). Case-insensitive.
_CMD_RE = re.compile(
    r"^\s*/grug\s+(recheck|improve|ask|test-gaps|test_gaps|resume|review|learnings|accept-removal|accept_removal|walkthrough|fix)\b[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

_CANON = {"test_gaps": "test-gaps", "accept_removal": "accept-removal"}
VERBS = frozenset({
    "recheck", "improve", "ask", "test-gaps", "resume", "review", "learnings",
    "accept-removal", "walkthrough", "fix",
})

# The verbs that start an ELDER re-run. They are the ones that must not
# overlap with a review already in flight, that must carry the
# `grug:reviewing` label while they run, and that must be KEPT (deferred to
# the GitHub budget's reset) instead of dropped when the installation's
# hourly budget is spent (grug#1270).
ELDER_RERUN_VERBS = frozenset({"improve", "resume", "review"})


@dataclass(frozen=True)
class GrugCommand:
    verb: str          # canonical verb (one of VERBS)
    arg: str = ""      # trailing text (`ask`, `learnings`, `accept-removal`)


def parse_command(body: str) -> GrugCommand | None:
    """First `/grug <verb>` in the comment, or None. If several appear, the
    first wins (a comment is one intent)."""
    m = _CMD_RE.search(body or "")
    if not m:
        return None
    verb = m.group(1).lower()
    verb = _CANON.get(verb, verb)
    return GrugCommand(verb=verb, arg=m.group(2).strip())
