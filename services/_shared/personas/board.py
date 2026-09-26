"""One review comment per PR, assembled from per-persona sections.

THE PROBLEM THIS SOLVES. Every persona posts its OWN top-level comment, so
a single PR produced three of them - measured on a private app repo:

    03:38  <!-- grug-teller:walkthrough -->
    03:43  <!-- grug-elder-stack -->
    11:58  <!-- grug-sentinel:abandoned-review -->

GitHub emails on each, hours apart, so the author gets three notifications
for one review and has to reassemble the picture from an inbox.

THE LEVER. GitHub notifies on comment CREATION, not on EDIT. A single
comment that every persona EDITS therefore costs exactly ONE email no
matter how many personas contribute, and later passes refresh it silently.
That is the whole reason this module exists.

HOW IT WORKS. The board body carries one delimited region per persona:

    <!-- grug-board -->
    ...header...
    <!-- grug-sec:elder -->
    <details>...Elder's markdown...</details>
    <!-- /grug-sec:elder -->

`upsert_section` replaces exactly one region and leaves every other byte
untouched, so two personas writing concurrently can only clobber each
other's section, never each other's content. Sections render in a FIXED
order (`SECTION_ORDER`) regardless of arrival order, so the comment does
not reshuffle itself between passes - a board whose layout moves is one
nobody learns to skim.

Pure string in, pure string out: no HTTP, no store, no clock. The dispatch
layer owns fetching the existing body and PATCHing the new one, which is
what makes every rule here unit-testable without a network.
"""

from __future__ import annotations

import re

BOARD_MARKER = "<!-- grug-board -->"

# Fixed render order. A persona missing from a given pass simply has no
# region; it does not shift the others.
SECTION_ORDER: tuple[str, ...] = (
    "chief",      # DoR / ticket readiness - gate first
    "teller",     # walkthrough - what changed
    "elder",      # the review itself
    "guard",      # security / dependency
    "smasher",    # execution-class findings
    "sentinel",   # safety-net warnings - last, loudest
)

_OPEN = "<!-- grug-sec:{key} -->"
_CLOSE = "<!-- /grug-sec:{key} -->"


def _region(key: str) -> re.Pattern[str]:
    """Matches one persona's whole region INCLUDING both delimiters.

    `re.escape` on the delimiters, and a non-greedy body, so a section whose
    markdown happens to contain another section's marker text cannot make
    the match run past its own close tag.
    """
    return re.compile(
        re.escape(_OPEN.format(key=key)) + r".*?" + re.escape(_CLOSE.format(key=key)),
        re.DOTALL,
    )


def render_section(key: str, markdown: str) -> str:
    """One delimited region. Delimiters always on their own lines so a
    section ending in a fenced block cannot swallow the close tag."""
    return (
        f"{_OPEN.format(key=key)}\n{markdown.rstrip()}\n{_CLOSE.format(key=key)}"
    )


def section_keys(body: str) -> list[str]:
    """Persona keys currently present, in board order. Unknown keys (an
    older grug wrote a section this version does not know about) are kept
    and reported last rather than dropped - losing a section on upgrade
    would silently delete a persona's output."""
    found = set(re.findall(r"<!-- grug-sec:([a-z0-9_-]+) -->", body))
    known = [k for k in SECTION_ORDER if k in found]
    return known + sorted(found - set(SECTION_ORDER))


def upsert_section(body: str, key: str, markdown: str) -> str:
    """Return `body` with `key`'s region replaced, or inserted in order.

    Replacing in place is what keeps this concurrency-safe at the section
    level: a persona rewrites only the bytes between its own delimiters.
    """
    section = render_section(key, markdown)
    pat = _region(key)
    if pat.search(body):
        return pat.sub(lambda _m: section, body, count=1)

    # New section: insert so the result is in SECTION_ORDER, rather than
    # appending. Append order would depend on which persona happened to
    # finish first, so the board would look different on every PR.
    existing = section_keys(body)
    order = {k: i for i, k in enumerate(SECTION_ORDER)}
    after = [k for k in existing if order.get(k, 10_000) > order.get(key, 10_000)]
    if not after:
        return body.rstrip() + "\n\n" + section + "\n"
    first_after = after[0]
    anchor = _OPEN.format(key=first_after)
    idx = body.index(anchor)
    return body[:idx] + section + "\n\n" + body[idx:]


def new_board(header: str = "") -> str:
    """An empty board. The marker is FIRST so the comment-finder can match
    on it without parsing anything else."""
    head = f"\n{header.rstrip()}\n" if header.strip() else "\n"
    return f"{BOARD_MARKER}{head}"


def is_board(body: str) -> bool:
    return BOARD_MARKER in (body or "")


def collapse(summary: str, markdown: str, *, open_by_default: bool = False) -> str:
    """A GitHub-collapsible block.

    The blank lines around the body are REQUIRED: without them GitHub does
    not render markdown inside <details>, and the section silently degrades
    to literal asterisks and pipe characters. That is the single most common
    way a folded section looks broken.
    """
    attr = " open" if open_by_default else ""
    return (
        f"<details{attr}>\n<summary>{summary}</summary>\n\n"
        f"{markdown.strip()}\n\n</details>"
    )

# --- the verdict header: this IS the email ---------------------------------
#
# GitHub notifies on comment CREATION, never on edit. So the email a human
# receives is EXACTLY the body at the moment the board is first posted, and
# every later persona pass is silent. Two consequences drive everything here:
#
#   1. The first body must lead with a VERDICT, not with evidence. Today the
#      first thing posted is Teller's walkthrough - a file table plus a
#      mermaid diagram - so the email is maximum noise and zero conclusion.
#   2. Everything that is evidence rather than conclusion must be FOLDED, so
#      the mail client renders a few lines instead of a diff dump.
#
# The header is regenerated on every pass (`set_header`), so the comment stays
# accurate as personas report in - the email just froze the earliest version,
# which is why that version has to be worth reading on its own.

_HEADER_END = "<!-- /grug-header -->"

# Caveman voice, one line, verdict first. Kept deliberately short: this is a
# subject line, not a paragraph.
_VERDICT_BLOCK = "Grug say **WAIT**. Something here bite tribe later."
_VERDICT_ADVISE = "Grug say **go**, but Grug leave few marks for hunter."
_VERDICT_CLEAR = "Grug look hard. **Trail clear.**"
_VERDICT_DEGRADED = "Grug eyes cloudy this pass - **read for self**."
_VERDICT_PARTIAL = "Grug walk most of trail. **Some ground not walked.**"


def verdict_line(
    blocking: int, advisory: int, *, degraded: bool = False, partial: bool = False,
) -> str:
    """One caveman sentence. Never more - the email is a notification, not a
    report, and the report is one click away in the folded sections.

    `degraded` means Elder saw NOTHING. `partial` means it walked most of the
    diff but not all of it - a different fact, and it used to borrow the
    blackout sentence. "Grug eyes cloudy - read for self" on a pass that
    reviewed most cohorts and published real findings tells the author to
    ignore work that was actually done.

    Order matters: a blocking finding is the most actionable thing on the
    board, so it outranks the coverage caveat. Partial coverage only
    displaces `_VERDICT_CLEAR`, which is the one sentence it would make into
    a lie - an all-clear over ground Elder never covered.
    """
    if degraded:
        return _VERDICT_DEGRADED
    if blocking:
        return _VERDICT_BLOCK
    if partial:
        return _VERDICT_PARTIAL
    if advisory:
        return _VERDICT_ADVISE
    return _VERDICT_CLEAR


def _short(sha: str) -> str:
    """First 7 chars of a sha - what `git log --oneline` shows, and short
    enough to read inline."""
    return (sha or "")[:7]


def review_scope_line(
    *, living_range: str = "", base_sha: str = "", head_sha: str = "",
) -> str:
    """What this run actually read (#673 item 2).

    The Living Hunt delta was already disclosed, but only on delta runs - a
    full base..head review said nothing at all about its scope, which is the
    common path and every FIRST review of a PR. "What did you look at" is the
    first question a skeptical reader asks, and it had no answer.

    Both cases name a RANGE, not a single commit: `base..head` is what the
    author pastes into `git diff` to see exactly what Grug saw.
    """
    if living_range:
        return f"`{living_range}`"
    if base_sha and head_sha:
        return f"`{_short(base_sha)}..{_short(head_sha)}`"
    if head_sha:
        return f"up to `{_short(head_sha)}`"
    return ""


def tally_line(blocking: int, advisory: int) -> str:
    """The counts, in words a human can act on. Empty when there is nothing
    to count, so a clean review stays a single sentence."""
    parts = []
    if blocking:
        parts.append(f"**{blocking} block**")
    if advisory:
        parts.append(f"{advisory} advise")
    return " - ".join(parts)


def render_header(
    title: str,
    blocking: int,
    advisory: int,
    *,
    degraded: bool = False,
    partial: bool = False,
) -> str:
    """The whole visible email: a verdict sentence, an optional tally, and the
    PR title. Three lines at most."""
    lines = [
        f"### {verdict_line(blocking, advisory, degraded=degraded, partial=partial)}"
    ]
    tally = tally_line(blocking, advisory)
    if tally:
        lines.append("")
        lines.append(tally)
    if title:
        lines.append("")
        lines.append(f"_{title}_")
    return "\n".join(lines)


def set_header(body: str, header: str) -> str:
    """Replace the header region, leaving every section untouched.

    Regenerated on each pass so the comment stays true as personas report in.
    The EMAIL keeps whatever the header said at creation - which is precisely
    why the creating persona must post a verdict rather than evidence.
    """
    block = f"{BOARD_MARKER}\n{header.rstrip()}\n{_HEADER_END}"
    if _HEADER_END in body:
        idx = body.index(_HEADER_END) + len(_HEADER_END)
        return block + body[idx:]
    if BOARD_MARKER in body:
        idx = body.index(BOARD_MARKER) + len(BOARD_MARKER)
        return block + body[idx:]
    return block + "\n" + body

def extract_section(body: str, key: str) -> str | None:
    """The markdown INSIDE one persona's region, delimiters stripped.

    Needed because a persona may assemble a whole board locally and then hand
    it to the client to MERGE - the client needs that persona's region alone,
    or it would overwrite everyone else's.
    """
    m = _region(key).search(body or "")
    if not m:
        return None
    inner = m.group(0)
    inner = inner.split(_OPEN.format(key=key), 1)[-1]
    inner = inner.rsplit(_CLOSE.format(key=key), 1)[0]
    return inner.strip()


def extract_header(body: str) -> str | None:
    """The header region's markdown, or None when the body has no header."""
    if BOARD_MARKER not in (body or "") or _HEADER_END not in body:
        return None
    start = body.index(BOARD_MARKER) + len(BOARD_MARKER)
    return body[start:body.index(_HEADER_END)].strip() or None

