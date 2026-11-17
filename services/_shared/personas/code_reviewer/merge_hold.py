"""Merge holds the change ignores (grug#1766).

A pull request body or the ticket it links can hold the merge on another
issue: "Merge hold: safe only once renewal is proven (#22, still open)",
"blocked on #13", "do not merge until #40 lands". When the cited issue is
still open at review time, the change goes in ahead of a precondition its
own author or ticket named. The review benchmark's `pr_review` suite has
such a case that drew no finding at all.

Pure here: `find_holds` reads hold sentences and the issues they cite out of
markdown, and `hold_findings` turns the holds whose cited issue is open into
one blocking finding each, anchored on the first added line of the diff.
Dispatch does the reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import Finding

RULE = "merge-hold-open"
# Bounds: a body that declares more holds than this is not reviewed for the
# rest, and a quote longer than this is cut.
MAX_HOLDS = 8
MAX_QUOTE_CHARS = 240
# Only this much of a line is read for a hold and its references.
MAX_LINE_CHARS = 1000
# A hold keeps at most this many cited issues, and one review reads at most
# this many cited issues' states, so a long hold list cannot drain the App's
# GitHub rate limit. Past the read cap the check fails closed.
MAX_REFS_PER_HOLD = 8
MAX_ISSUE_READS = 16

# Owner and repository names are bounded as GitHub bounds them, so a long
# run of name characters cannot make each start position rescan the run.
_REF_RE = re.compile(
    r"(?<![\w/])([\w-][\w.-]{0,38})/([\w-][\w.-]{0,99})#(\d{1,9})\b"
    r"|(?<![\w/#])#(\d{1,9})\b",
)

_HOLD_RE = re.compile(
    r"\bmerge[- ]hold\b"
    r"|\bhold(?:s|ing)?\s+(?:the\s+|this\s+)?merge\b"
    r"|\b(?:do\s+not|don't|never)\s+merge\b"
    r"|\bnot\s+(?:yet\s+)?(?:safe\s+)?to\s+merge\b"
    r"|\bblocked\s+(?:on|by|until)\b"
    r"|\bmerge\s+(?:only\s+)?(?:after|once|when)\b",
    re.IGNORECASE,
)
_RELEASED_RE = re.compile(r"\b(?:no\s+longer|not|un)[- ]?blocked\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_DONE_BOX_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]")
_OUT_OF_SCOPE_RE = re.compile(r"\bout\s+of\s+scope\b|\bnon-goals?\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+")


@dataclass(frozen=True)
class Hold:
    """One sentence that holds the merge, where it came from, and the
    same-repository issues it waits on."""

    source: str
    quote: str
    refs: tuple[int, ...]


def _refs(text: str, owner: str, repo: str) -> tuple[int, ...]:
    """Same-repository issue numbers cited in `text`, in order, at most
    MAX_REFS_PER_HOLD."""
    found: dict[int, None] = {}
    for m in _REF_RE.finditer(text[:MAX_LINE_CHARS]):
        if m.group(4):
            found.setdefault(int(m.group(4)), None)
        elif m.group(1).casefold() == owner.casefold() and m.group(2).casefold() == repo.casefold():
            found.setdefault(int(m.group(3)), None)
        if len(found) >= MAX_REFS_PER_HOLD:
            break
    return tuple(found)


def _quote(sentence: str) -> str:
    text = " ".join(sentence.split()).lstrip("-* ").strip()
    return text if len(text) <= MAX_QUOTE_CHARS else text[: MAX_QUOTE_CHARS - 3] + "..."


def find_holds(markdown: str, *, source: str, owner: str, repo: str) -> tuple[Hold, ...]:
    """Every sentence in `markdown` that holds the merge on a cited issue.

    Skipped: sentences under an out-of-scope heading (they hold other work),
    checked boxes (the condition is met), sentences that say the work is no
    longer blocked, and code fences. A heading that holds the merge ("Blocked
    by") makes each line under it a hold until the next heading.
    """
    holds: list[Hold] = []
    in_fence = False
    out_of_scope = False
    heading_holds = False
    for full_line in (markdown or "").splitlines():
        line = full_line[:MAX_LINE_CHARS]
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        heading = _HEADING_RE.match(line)
        if in_fence:
            continue
        if heading:
            title = heading.group(1)
            out_of_scope = bool(_OUT_OF_SCOPE_RE.search(title))
            heading_holds = bool(_HOLD_RE.search(title)) and not _RELEASED_RE.search(title)
            continue
        if out_of_scope or _DONE_BOX_RE.match(line):
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            if _RELEASED_RE.search(sentence):
                continue
            if not (heading_holds or _HOLD_RE.search(sentence)):
                continue
            refs = _refs(sentence, owner, repo)
            if refs:
                holds.append(Hold(source, _quote(sentence), refs))
    return tuple(holds)


def _first_added_line(hunks: tuple[DiffHunk, ...]) -> tuple[str, int] | None:
    for hunk in hunks:
        if hunk.new_lines:
            return hunk.file_path, min(hunk.new_lines)
    return None


def _unchecked_finding(anchor: tuple[str, int], unchecked: int) -> Finding:
    return Finding(
        file=anchor[0],
        line=anchor[1],
        severity="high",
        rule_name=RULE,
        message=(
            f"Merge holds cite more issues than one review reads: {unchecked} "
            f"cited issues past the first {MAX_ISSUE_READS} were not checked, "
            "so the holds cannot be shown met. Trim the hold list to the "
            "issues the merge really waits on."
        ),
        suggestion=None,
        category="correctness",
    )


def hold_findings(
    holds: tuple[Hold, ...], open_refs: frozenset[int],
    hunks: tuple[DiffHunk, ...], *, unchecked: int = 0,
) -> tuple[Finding, ...]:
    """One high finding per hold that waits on a still-open issue, plus one
    when `unchecked` cited issues went past the read cap (fail closed).
    Holds whose every cited issue is closed, or a diff with no added line,
    give none. Duplicate quotes count once."""
    anchor = _first_added_line(hunks)
    if anchor is None:
        return ()
    findings: list[Finding] = (
        [_unchecked_finding(anchor, unchecked)] if unchecked else []
    )
    seen: set[str] = set()
    for hold in holds[:MAX_HOLDS]:
        waiting = [n for n in hold.refs if n in open_refs]
        if not waiting or hold.quote in seen:
            continue
        seen.add(hold.quote)
        issues = ", ".join(f"#{n}" for n in waiting)
        verb = "is" if len(waiting) == 1 else "are"
        findings.append(Finding(
            file=anchor[0],
            line=anchor[1],
            severity="high",
            rule_name=RULE,
            message=(
                f"Merge hold not met: {hold.source} says \"{hold.quote}\", and "
                f"{issues} {verb} still open. Merge after {issues} closes, or "
                "remove the hold from the text and say why it no longer applies."
            ),
            suggestion=None,
            category="correctness",
        ))
    return tuple(findings)
