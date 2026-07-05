"""Chief ticket-compliance: does a PR's diff address the acceptance
criteria of the issue it claims to close? (#529, epic #522.)

The PR-side twin of the close-completeness guard (which reopens an ISSUE
whose acceptance boxes are unchecked at close). Here, when a PR says
`closes #N`, Chief compares #N's acceptance-criteria checkboxes against
the PR's diff signals and flags criteria that look unaddressed - an
advisory nudge, never a gate.

All pure: the dispatch gathers the facts (PR body, linked-issue body,
changed file paths) and these functions decide. The heuristic is
deliberately CONSERVATIVE - a criterion is flagged unaddressed ONLY when
NONE of its distinctive tokens appear anywhere in the diff signals, so
the false-positive rate the issue warns about stays low.
"""

from __future__ import annotations

import re

# `closes/fixes/resolves` CLAIM closure; `refs/part of/blocked by` do not,
# so only the closing verbs trigger the compliance check.
_CLOSES_RE = re.compile(r"\b(?:closes|closed|close|fixes|fixed|fix|resolves|resolved|resolve)\s+#(\d+)\b", re.I)
# UNCHECKED acceptance-criteria lines only: `- [ ] text`. A CHECKED box
# (`- [x]`) is the author asserting that criterion is already done -
# flagging it would contradict them and manufacture false positives
# (Qodo review #535), so only open boxes are cross-checked.
_BOX_RE = re.compile(r"^\s*[-*]\s*\[ \]\s+(.+?)\s*$")
# Words too generic to be distinctive signal.
_STOP = frozenset("""
a an the and or of to in on for with without via per is are be that this it
its into from as at by not no new add adds added remove removes update updates
should must when then than only ever never each any all both onto over under done works work exists present verified check checked
uses use set sets get gets run runs make makes so speaks grug
""".split())
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")


def closes_refs(pr_body: str) -> list[int]:
    """Issue numbers the PR body claims to CLOSE (dedup, order-preserving)."""
    seen: dict[int, None] = {}
    for m in _CLOSES_RE.finditer(pr_body or ""):
        seen.setdefault(int(m.group(1)), None)
    return list(seen)


def acceptance_criteria(issue_body: str) -> list[str]:
    """The UNCHECKED acceptance-criteria lines - the still-open criteria a
    PR claiming to close the issue should address. Checked boxes are
    excluded (the author asserts those are done)."""
    out = []
    for line in (issue_body or "").splitlines():
        m = _BOX_RE.match(line)
        if m:
            out.append(m.group(1).strip())
    return out


def _tokens(text: str) -> set[str]:
    """Distinctive lowercase tokens: split camelCase/paths, drop stopwords
    and 1-2 char noise."""
    raw = text.lower()
    # split path separators and camelCase into word boundaries
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower()
    raw = raw.replace("/", " ").replace("_", " ").replace("-", " ").replace(".", " ")
    return {
        t for t in _TOKEN_RE.findall(raw)
        if len(t) >= 3 and t not in _STOP and not t.isdigit()
    }


def diff_signals(changed_files: list[str], extra_text: str = "") -> set[str]:
    """The token universe the diff touched: path components + basenames of
    every changed file, plus any extra text (PR title/body). A criterion
    is 'addressed' when its distinctive tokens intersect this set."""
    sig: set[str] = set()
    for path in changed_files or []:
        sig |= _tokens(path)
    sig |= _tokens(extra_text)
    return sig


def unaddressed_criteria(criteria: list[str], signals: set[str]) -> list[str]:
    """Criteria whose distinctive tokens have NO overlap with the diff
    signals - conservatively 'looks unaddressed'. A criterion with no
    distinctive tokens of its own (all stopwords) is never flagged (we
    can't judge it)."""
    out = []
    for c in criteria:
        toks = _tokens(c)
        if toks and not (toks & signals):
            out.append(c)
    return out


_MARKER = "<!-- grug-chief:ticket-compliance -->"


def advisory_markdown(issue_number: int, unaddressed: list[str]) -> str | None:
    """The advisory comment body, or None when everything looks addressed
    (nothing to post). Carries a marker so the dispatch can refresh one
    comment instead of duplicating."""
    if not unaddressed:
        return None
    lines = "\n".join(f"- {c}" for c in unaddressed)
    return (
        f"{_MARKER}\n"
        f"**Chief - ticket compliance.** This PR says it closes #{issue_number}, "
        f"but these acceptance criteria don't look addressed by the diff "
        f"(heuristic - Chief may be wrong; a criterion met by a sibling PR or "
        f"already-merged work will show here):\n\n{lines}\n\n"
        f"Advisory only - it does not gate the merge. So speaks Grug."
    )
