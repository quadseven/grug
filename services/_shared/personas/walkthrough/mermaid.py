"""Deterministic PR-diagram builder (#554).

DELIBERATELY not model-authored: LORE and CodeRabbit both render a mermaid
diagram from free-form model output, which means trusting an LLM to emit
syntactically valid mermaid AND to never smuggle a clickable/injectable
node label. Teller's diagram is built ENTIRELY from the changed-file path
list (repo-controlled data the persona itself walked, never user/model
text) - it groups files by top-level directory into mermaid subgraphs.
There is no free-form text in a label beyond a sanitized basename, so
there is no injection surface to defend and no LLM call to degrade.

`build_diagram` still validates its own OWN output before the caller
posts it (balanced brackets is a cheap, real syntax check even though the
generator is deterministic) - a future change to this module must not
ship a broken block silently.
"""

from __future__ import annotations

import re
from collections import defaultdict

# Cap the diagram so a sweeping PR (500 files) doesn't produce a page-long
# block; beyond the cap, no diagram (never a truncated/misleading one).
_MAX_FILES = 40
_MAX_GROUPS = 12

# mermaid node labels tolerate most text inside [...], but brackets and
# quotes inside the label break the node syntax; pipe/backtick can also
# confuse flowchart parsing in some renderers. Strip to a safe set. Also
# strip C0/DEL control bytes (\x00-\x1f, \x7f) - a POSIX filename can
# legally contain a raw newline or other control byte, which would break
# the single-line diagram syntax same as an unescaped bracket (CodeRabbit).
_UNSAFE_LABEL_CHARS = re.compile(r'[\[\]{}()"\'`|\x00-\x1f\x7f]')


def _safe_label(text: str) -> str:
    """A path component, stripped of characters that break mermaid node
    syntax. Never raises; worst case a slightly mangled but safe label."""
    return _UNSAFE_LABEL_CHARS.sub("", text)[:80] or "file"


def _group_key(path: str) -> str:
    """Top-level directory (or the bare filename for a root-level file)."""
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "(root)"


def build_diagram(paths: list[str]) -> str | None:
    """A mermaid `graph TD` grouping `paths` into subgraphs by top-level
    directory. Returns None when there is nothing to show, too much to
    show safely, or the generated text fails its own balance check - the
    caller degrades to no-diagram, never posts a broken block."""
    if not paths:
        return None
    capped = paths[:_MAX_FILES]
    groups: dict[str, list[str]] = defaultdict(list)
    for p in capped:
        groups[_group_key(p)].append(p)
    if len(groups) > _MAX_GROUPS:
        return None

    lines = ["graph TD"]
    node_id = 0
    for group_name in sorted(groups):
        safe_group = _safe_label(group_name)
        lines.append(f'  subgraph "{safe_group}"')
        for path in groups[group_name]:
            basename = path.rsplit("/", 1)[-1]
            lines.append(f'    N{node_id}["{_safe_label(basename)}"]')
            node_id += 1
        lines.append("  end")
    diagram = "\n".join(lines)

    if not _is_balanced(diagram):
        return None
    return diagram


def _is_balanced(text: str) -> bool:
    """Cheap syntax sanity check: every bracket/subgraph opened is closed.
    Not a full mermaid parser - a real renderer is the final authority -
    but catches the generator-bug class (an unterminated subgraph, a
    stray quote) before it ever reaches GitHub.

    Bracket/quote counting is safe against label pollution: `_safe_label`
    already strips `[](){}` and `"` from every label, so those exact
    characters can never appear inside label text. But "subgraph"/"end"
    are ordinary words, NOT stripped - a directory literally named
    `subgraph` produces a label `"subgraph"`, and a substring count of
    the whole diagram text would then count that label's text as an
    extra structural token, causing a false-negative (Qodo #559: a
    perfectly fine diagram gets needlessly dropped). Count matching
    LINES instead, keyed on line-start position, not substring presence."""
    pairs = {"[": "]", "{": "}", "(": ")"}
    stack: list[str] = []
    for ch in text:
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in pairs.values():
            if not stack or stack.pop() != ch:
                return False
    if stack:
        return False
    if text.count('"') % 2 != 0:
        return False
    lines = text.split("\n")
    subgraphs = sum(1 for ln in lines if ln.lstrip().startswith("subgraph "))
    ends = sum(1 for ln in lines if ln.strip() == "end")
    return subgraphs == ends
