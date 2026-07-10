"""Elder voice selection - caveman (free) or sage (paid voice pack).

The Elder persona speaks caveman on every free-tier surface; the sage voice is
a paid, entitlement-gated voice pack (issue #288 trademark safety / #578). Both
voices re-skin ONLY the finding `message` field - technical tokens (paths, line
numbers, rule names, identifiers) stay verbatim and machine-actionable.

DESIGN NOTE - why the sage voice lives here, not in code_review_prompt.py:
the shipped prompt module is fingerprinted by the #537 elder-eval gate
(`compute_prompt_sha` hashes its source bytes), so ANY edit there forces a live
re-record of baseline.json. The sage voice is an ADDITIVE overlay - it reuses
the caveman prompt's exact rules, structure, and output contract and swaps only
the VOICE block - so code_review_prompt.py stays byte-identical and the eval
baseline is untouched. The default (caveman) path is a pure no-op.
"""
from __future__ import annotations

from typing import Literal

# The caveman VOICE block, imported (not re-declared) so the swap targets the
# EXACT string build_system_prompt injects. Keeping code_review_prompt.py the
# single source of the default voice avoids a stale copy drifting out of sync.
from code_review_prompt import _VOICE as _CAVEMAN_VOICE

VoiceSelection = Literal["caveman", "sage"]

DEFAULT_VOICE: VoiceSelection = "caveman"

# Sage voice - the paid pack. Inverted cadence (object first, then subject, then
# verb), quiet "Hmm"/"yes" particles, the measured tone of an old teacher.
# Trademark-safe: generic inverted syntax tied to no protected character.
# Mirrors _CAVEMAN_VOICE's shape (mandatory structure + two density-spanning
# examples). Must NOT contain the phrase "false negative" (the v1-only precision
# lever asserted absent from v2 in test_prompt_variant), and keeps every
# technical token verbatim so the wisdom stays machine-actionable.
_SAGE_VOICE = (
    "VOICE - write every `message` in the sage cadence: inverted word order "
    "(the object named first, then the subject, then the verb), quiet 'Hmm' "
    "and 'yes' particles, the grave measured wisdom of an old teacher. Never "
    "silly, never baby-talk. "
    "STRUCTURE every `message` so the voice cannot slip: (1) OPEN with the "
    "sage's mark - `Hmm.` or `Yes,` or `I sense`; (2) the insight in inverted "
    "order - the defect named as object first, then the subject, then the "
    "verb; (3) the remedy - the exact fix; (4) CLOSE with `Hmm.` or `So it "
    "is.` EVERY message opens and closes thus, and NONE begins with plain "
    "prose like 'This function' / 'The code' / 'There is'. If you catch "
    "yourself writing modern professional English, STOP and re-cast it in the "
    "sage cadence. "
    "The wisdom must stay ACTIONABLE - name the exact defect and the exact "
    "remedy. The cadence is the WRAPPER; the technical core inside it is "
    "verbatim and unaltered: identifiers, exception/class/function names, file "
    "paths, and the rule name are spoken EXACTLY (write `OSError`, never 'the "
    "wide spirit'; write `asyncio.create_task`, never 'the summoning'). Only "
    "the `message` value speaks this way; `path`, `line`, `rule`, and "
    "`severity` stay precise machine values. "
    "Example (simple) - not 'Broad except Exception masks programmer errors; "
    "catch OSError and ValueError', but: 'Hmm. Too wide a net this "
    "`except Exception` casts - every fault it swallows, even the bugs you did "
    "not mean. NameError and KeyError, hidden in it they are, wearing success "
    "as a mask. Catch only `OSError` and `ValueError`, you must. So it is.' "
    "Example (modern, high-density) - not 'This async function is called "
    "without await so the coroutine never executes', but: 'Yes, the arrow "
    "loosed but never watched in flight, I sense. `fetch_user` is `async` - "
    "called without `await`, its work unspoken remains: the coroutine sleeps, "
    "the task undone, yet the code walks on as if fed. Speak the word - "
    "`await fetch_user(id)` - and true the arrow strikes. Hmm.'"
)


def apply_voice(system_prompt: str, voice: VoiceSelection) -> str:
    """Return `system_prompt` re-voiced for `voice`.

    Caveman (the default) is a no-op - the shipped prompt already carries the
    caveman VOICE block. Sage swaps that exact block for `_SAGE_VOICE`, leaving
    every rule, example, and output contract untouched. Raises if the caveman
    block is absent (a prompt-module drift the caller must not silently ship
    with the wrong voice)."""
    if voice == "sage":
        if _CAVEMAN_VOICE not in system_prompt:
            raise ValueError(
                "apply_voice(sage): caveman VOICE block not found in prompt - "
                "code_review_prompt._VOICE drifted; refusing to ship an "
                "un-revoiced sage prompt"
            )
        return system_prompt.replace(_CAVEMAN_VOICE, _SAGE_VOICE, 1)
    return system_prompt


def resolve_voice(repo_config: dict) -> VoiceSelection:
    """The Elder voice for a repo, from its stored `elder_voice` config.

    Fail-safe to caveman for a missing or unrecognized value: the paid voice is
    opt-in and write-gated (set_repo_config enforces entitlement), so an
    unexpected stored value must never crash a review - it speaks the free
    default instead."""
    return "sage" if repo_config.get("elder_voice") == "sage" else "caveman"
