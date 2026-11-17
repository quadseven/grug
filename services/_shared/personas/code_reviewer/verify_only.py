"""Findings that only ask the author to verify, ensure or confirm (grug#1749).

"Ensure that X is set", "verify that Y is handled", "make sure this path is
covered": such a finding names no failing input and no line that is wrong,
so it carries no evidence of a defect. It costs the author a round and
teaches people to skim the review. A low or medium one stays in the check
summary and gets no inline comment; a high or critical one is never
demoted by this rule.

Pure: message text in, bool out.
"""
from __future__ import annotations

import re

from personas.code_reviewer.persona import Finding

# One clause that is an ask to check something, with up to three polite or
# hedged lead-ins. Each lead-in ends in a word and the separator class is
# disjoint from it, and the repeat is bounded, so the match cannot backtrack
# across a long run of lead-ins.
_ASK_RE = re.compile(
    r"^(?:(?:please|consider|you\s+(?:should|may|might|could)(?:\s+want\s+to)?|"
    r"it\s+(?:is|would\s+be)\s+(?:worth|wise|good|best)(?:\s+to)?|"
    r"might\s+want\s+to|grug\s+(?:say|ask)s?)[\s,:]{1,3}){0,3}"
    r"(?:ensure|ensuring|verify|verifying|confirm|confirming|make\s+sure|"
    r"double[- ]check|check\s+(?:that|whether|if))\b",
    re.IGNORECASE,
)
# What makes a sentence a defect claim rather than flavor: a concrete
# failure, a wrong result or a cited line.
_CLAIM_RE = re.compile(
    r"\b(?:fails?|failing|failed|raises?|raised|throws?|crash(?:es|ed)?|"
    r"returns?|returned|breaks?|broken|leaks?|drops?|dropped|skips?|skipped|"
    r"loses?|lost|overflows?|deadlocks?|races?|hangs?|hung|times?\s+out|"
    r"never|always|wrong|incorrect|instead\s+of|off[- ]by[- ]one)",
    re.IGNORECASE,
)
# Evidence that counts even inside an ask: a cited line or a concrete input.
_EVIDENCE_RE = re.compile(
    r"\bline\s+\d+|\binputs?\s+(?:of\s+)?`|\be\.g\.|\bfor\s+example\b|"
    r"\bwhen\s+`[^`]+`\s+(?:is|=|==)",
    re.IGNORECASE,
)
# Sentences, and clauses after a semicolon or an "otherwise" / "or else":
# a consequence stated after an ask is a claim of its own.
_SENTENCE_RE = re.compile(
    r"(?<=[.!?;])\s+|\n+|,?\s+(?:otherwise|or\s+else)\b", re.IGNORECASE,
)
_DEMOTABLE = frozenset({"low", "medium"})


# Grug's own bracketed notes on a finding ("[Downgraded to advisory: ...
# confirm by hand.]") are not the reviewer's ask and are not read.
_GRUG_NOTE_RE = re.compile(r"\[[^\[\]]{0,600}\]")


def _sentences(message: str) -> list[str]:
    text = _GRUG_NOTE_RE.sub(" ", message or "")
    return [s.strip(" -*>") for s in _SENTENCE_RE.split(text) if s.strip(" -*>")]


def is_verify_only(message: str) -> bool:
    """True when the message asks to verify, ensure or confirm something,
    no other sentence in it claims a concrete failure, and no sentence
    cites a line or a concrete input. An ask is not searched for failure
    verbs: "ensure X never returns None" is still only an ask."""
    sentences = _sentences(message)
    asks = [s for s in sentences if _ASK_RE.match(s)]
    if not asks or _EVIDENCE_RE.search(message or ""):
        return False
    return not any(_CLAIM_RE.search(s) for s in sentences if s not in asks)


def is_summary_only(finding: Finding) -> bool:
    """A low or medium verify-only finding: summary, no inline comment."""
    return finding.severity in _DEMOTABLE and is_verify_only(finding.message)
