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
# (LORE review #535), so only open boxes are cross-checked.
_BOX_RE = re.compile(r"^\s*[-*]\s*\[ \]\s+(.+?)\s*$")
# Words too generic to be distinctive signal.
#
# Bug (found 2026-07-24, live audit of grug#730/PR#734): this list used to be
# a small hand-rolled set that MISSED ordinary English connectors like "but"
# and "after" - so e.g. issue #730's unchecked criterion "Failure after model
# completion but before publication is observable..." spuriously overlapped
# a PR body/diff that never touched retry/idempotency at all, just because
# "after" and "but" showed up somewhere incidental. Since `unaddressed_criteria`
# only flags a criterion when its token set is NON-empty AND has ZERO overlap
# with the diff signals, under-stopping can only ever suppress real findings
# (spurious "addressed" matches) - it can never manufacture a false positive,
# so this list should err toward completeness. Now a standard English
# stopword set (NLTK's default `stopwords.words("english")`) merged with the
# original domain-specific additions. The tokenizer (`_TOKEN_RE`) doesn't
# match apostrophes, so contractions split at the apostrophe (e.g. "don't"
# -> "don" + "t") - the stemmed halves are included below too.
_STOP = frozenset("""
i me my myself we our ours ourselves you your yours yourself yourselves
he him his himself she her hers herself it its itself they them their
theirs themselves what which who whom this that these those am is are
was were be been being have has had having do does did doing a an the
and but if or because as until while of at by for with about against
between into through during before after above below to from up down in
out on off over under again further then once here there when where why
how all any both each few more most other some such no nor not only own
same so than too very s t can will just don should now d ll m o re ve y
ain aren couldn didn doesn hadn hasn haven isn ma mightn mustn needn
shan shouldn wasn weren won wouldn
add adds added remove removes update updates onto done works work exists
present verified check checked uses use set sets get gets run runs make
makes speaks grug
""".split())
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")

# Chief's face, matching the Elder portrait already on the inline review
# (dispatch._PERSONA_PORTRAIT). Every persona that speaks in the email should
# be recognisable at a glance; Chief was the one talking without a face.
#
# `align="left"` + width 46 keeps it a little face rather than a banner, and
# the blank line after it is required or GitHub runs the <img> into the
# following paragraph. Verified live: /assets/grug_chief.png is a real
# image/png. Sibling personas (Drum, Haunt, Totem, Teller) have NO asset yet -
# grug.lol returns the SPA index.html for those paths, so adding an <img> for
# them would render a broken image, not a portrait.
CHIEF_PORTRAIT = "https://grug.lol/assets/grug_chief.png"
CHIEF_PORTRAIT_IMG = (
    f'<img src="{CHIEF_PORTRAIT}" width="46" align="left" alt="Grug Chief" />'
)


# Fenced blocks first, THEN inline spans: stripping inline code first would
# leave a fence's interior backticks pairing across its boundary.
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def strip_code_spans(text: str) -> str:
    """Blank out fenced blocks and inline code spans.

    A closing keyword inside code is a MENTION, not a claim. GitHub agrees -
    it does not autolink or close from `#N` inside code - so honouring that
    here is matching the platform, not carving an exception.

    Measured cost of not doing it: a PR body explaining the gate by quoting
    `Closes #775` in backticks was BLOCKED by the gate it was describing, on a
    PR that closed nothing. That is the worst kind of false positive - it fires
    exactly when someone is trying to document the rule.

    Replaced with a space, not "", so stripping cannot fuse the words either
    side into a token that never appeared.
    """
    return _INLINE_CODE_RE.sub(" ", _FENCE_RE.sub(" ", text or ""))


def closes_refs(pr_body: str) -> list[int]:
    """Issue numbers the PR body claims to CLOSE (dedup, order-preserving)."""
    seen: dict[int, None] = {}
    for m in _CLOSES_RE.finditer(strip_code_spans(pr_body)):
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


_MIN_MATCH_TOKENS = 2


def unaddressed_criteria(criteria: list[str], signals: set[str]) -> list[str]:
    """Criteria whose distinctive tokens don't overlap the diff signals
    ENOUGH to call them addressed - conservatively 'looks unaddressed'. A
    criterion with no distinctive tokens of its own (all stopwords) is
    never flagged (we can't judge it).

    Requires at least `_MIN_MATCH_TOKENS` overlapping tokens, not just one.
    Found live (2026-07-24, grug#730/PR#734): a single shared word is weak
    evidence - two DIFFERENT criteria in the same issue can each contain
    one word that also happens to appear in the PR's prose for an
    unrelated reason (e.g. a criterion about failure-recovery matches on
    "failure" alone because the diff mentions an unrelated "capture
    failure" case), so a lone-token match let genuinely-unaddressed
    criteria read as done. A criterion whose own distinctive vocabulary is
    smaller than the threshold (rare) can't be held to a bar it
    structurally cannot clear, so the bar is capped at its own token count.
    """
    out = []
    for c in criteria:
        toks = _tokens(c)
        if not toks:
            continue
        needed = min(_MIN_MATCH_TOKENS, len(toks))
        if len(toks & signals) < needed:
            out.append(c)
    return out


_MARKER = "<!-- grug-chief:ticket-compliance -->"


def advisory_markdown(issue_number: int, unaddressed: list[str]) -> str | None:
    """The advisory comment body, or None when everything looks addressed
    (nothing to post). Carries a marker so the dispatch can refresh one
    comment instead of duplicating."""
    if not unaddressed:
        return None
    lines = "\n".join(f"- [ ] {c}" for c in unaddressed)
    return (
        f"{_MARKER}\n"
        f"{CHIEF_PORTRAIT_IMG}\n\n"
        f"**Chief - ticket compliance.** This PR says it closes #{issue_number}, "
        f"but these acceptance criteria don't look addressed by the diff "
        f"(heuristic - Chief may be wrong; a criterion met by a sibling PR or "
        f"already-merged work will show here):\n\n{lines}\n\n"
        f"Advisory only - it does not gate the merge. So speaks Grug."
    )
