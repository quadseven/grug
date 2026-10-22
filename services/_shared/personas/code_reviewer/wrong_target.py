"""Deterministic check for lane work that changes something other than what its ticket named (#1369).

A lane PR on a ticket that asked to keep three named workloads off a
memory-pressured node added a node exclusion to the CI runner pools
instead. Its comments and its new test named the three workloads, so the
diff read as on target, but no line that runs changed for any of them.
Elder read the criteria and passed it.

One high `intent-mismatch` finding when the in-scope criteria and the title
name concrete targets, the diff changes none of them (in a changed path or
a changed line of code), names at least one of them only in comments or
tests, and changes other code. Naming a target in a comment or a test while
the code that runs changes elsewhere is the shape: the lane knew what was
asked and changed something else.

A target is a backticked identifier, path or resource name (not a command
or a phrase), a file path, or two or more hyphenated names in one criterion
that share a first word (`api-web`, `api-worker`: a workload family; a lone
hyphenated word is too often plain English). Targets are read only from
clauses that ask for a change: a `Live:` or verified-on-device criterion
checks the running system, and a clause that keeps, retains, reuses or
removes something can be met without the diff naming it (what was to be
removed may already be gone).

Bounded: one pass over the diff collects its changed text, then each
spelling of each target is one substring search of it, so the cost is
linear in the diff with a constant factor of at most `MAX_TARGETS` times
a handful of spellings. Past `MAX_SCAN_CHARS` of changed text, or
`MAX_TARGETS` targets, the check is not evaluated and reports nothing.

Pure: hunks, file changes and PR text in; findings out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import (
    DiffHunk,
    FileChange,
    is_review_excluded_path,
)
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

RULE = "intent-mismatch"
# Past either bound the check is not evaluated (no finding).
MAX_SCAN_CHARS = 4_000_000
MAX_TARGETS = 40

_BACKTICK_RE = re.compile(r"`+([^`\n]{2,120}?)`+")
_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.(?:py|ya?ml|json|md|sh|ts|tsx|js|jsx|go|tf|"
    r"toml|conf|cfg|ini|sql|rs|swift|kt|java|rb|php|c|cc|cpp|h|hpp|cs|html|css))(?![\w/])"
)
_HYPHENATED_RE = re.compile(r"(?<![\w./<>-])([a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?![\w/<>-])")
_IDENT_RE = re.compile(r"[\w./:@()-]+")
# A criterion about the running system, not the diff.
_LIVE_RE = re.compile(r"^\W*(live\b|live proof|verified\b)", re.IGNORECASE)
# Clause breaks: a target is read from the clause that names it.
_CLAUSE_RE = re.compile(r"\bwhile\b|;|\bbut\b|\bexcept\b|\.\s", re.IGNORECASE)
# A clause that keeps, reuses or removes what it names: the diff can meet it
# without changing (or even naming) the target.
_NOT_A_CHANGE_RE = re.compile(
    r"\b(remov|delet|retir|drop|no longer|retain|remain|still|unchanged|untouched|"
    r"preserv|keep|kept|reuse|contains no|not|never)\w*",
    re.IGNORECASE,
)
# First words that make a hyphenated pair English, not a workload family.
_PREFIX_WORDS = frozenset({
    "all", "anti", "auto", "closed", "co", "cross", "first", "full", "half", "high",
    "in", "long", "low", "multi", "no", "non", "not", "off", "on", "one", "open",
    "out", "over", "per", "post", "pre", "re", "read", "second", "self", "semi",
    "short", "single", "sub", "third", "two", "under", "well", "write",
})
_COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*|--|;|<!--)")
_PROSE_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_TEST_NAME_RE = re.compile(r"(\.test\.|\.spec\.|Test\.\w+$|_test\.\w+$)")


def _is_test(path: str) -> bool:
    return is_test_file(path) or bool(_TEST_NAME_RE.search(path.rsplit("/", 1)[-1]))


def _identifier(span: str) -> str | None:
    """A backticked span that names one concrete thing, or None: a command,
    a phrase, a bare lowercase word or a number is not a target."""
    text = span.strip().strip("\"'").rstrip(":")
    if len(text) < 3 or " " in text or text.startswith("-") or "<" in text:
        return None
    if re.fullmatch(r"[a-z]+|[\d.]+", text) or not _IDENT_RE.fullmatch(text):
        return None
    return text


def _change_clauses(texts: Iterable[str]) -> list[str]:
    clauses: list[str] = []
    for text in texts:
        if _LIVE_RE.search(text):
            continue
        clauses.extend(c for c in _CLAUSE_RE.split(text) if not _NOT_A_CHANGE_RE.search(c))
    return clauses


def ticket_targets(texts: Iterable[str]) -> list[str]:
    """Concrete targets the change clauses of these texts name, in order."""
    found: dict[str, None] = {}
    for clause in _change_clauses(texts):
        for match in _BACKTICK_RE.finditer(clause):
            ident = _identifier(match.group(1))
            if ident:
                found[ident] = None
        plain = _BACKTICK_RE.sub(" ", clause)
        for match in _PATH_RE.finditer(plain):
            found[match.group(1)] = None
        families: dict[str, list[str]] = {}
        for name in _HYPHENATED_RE.findall(plain):
            families.setdefault(name.split("-", 1)[0], []).append(name)
        for head, names in families.items():
            if head not in _PREFIX_WORDS and len(set(names)) >= 2:
                found.update(dict.fromkeys(names))
    return list(found)


def _spellings(target: str) -> set[str]:
    """Lowercase spellings a diff may use for a target: the target, its last
    path segment, a dotted module as a path, the other separator, a
    call's name without its arguments, and a test id's test name."""
    t = target.lower().lstrip("/")
    out = {t, t.replace("-", "_"), t.replace("_", "-")}
    if "/" in t:
        out.add(t.rsplit("/", 1)[-1])
    elif "." in t and not re.search(r"\.\w{1,4}$", t):
        out.add(t.replace(".", "/"))
    if "::" in t:
        out.add(t.rsplit("::", 1)[-1])
    call = re.match(r"([\w.:]+)\(", t)
    if call:
        out.add(call.group(1))
    return {s for s in out if len(s) >= 3}


def _is_test_target(target: str) -> bool:
    t = target.lower()
    return t.startswith("test") or "::" in t or "tests/" in t


class _ChangedText:
    """The diff's changed text in three parts, from one pass: file paths,
    changed lines of code, and changed lines that only name things
    (comments, and every line of a test file)."""

    def __init__(self, changes: Iterable[FileChange], hunks: Iterable[DiffHunk]) -> None:
        paths: list[str] = []
        self.code_files: set[str] = set()
        for change in changes:
            paths.append(change.path)
            if change.previous_path:
                paths.append(change.previous_path)
        self.paths = "\n".join(paths).lower()
        code: list[str] = []
        side: list[str] = []
        size = len(self.paths)
        self.bounded = True
        for hunk in hunks:
            path = hunk.file_path
            test = _is_test(path)
            prose = path.lower().endswith(_PROSE_SUFFIXES)
            for raw in hunk.body.splitlines()[1:]:
                if not raw or raw[0] not in "+-":
                    continue
                text = raw[1:]
                size += len(text) + 1
                if test or (not prose and _COMMENT_RE.match(text)):
                    side.append(text)
                else:
                    code.append(text)
                    if text.strip() and not is_review_excluded_path(path):
                        self.code_files.add(path)
            if size > MAX_SCAN_CHARS:
                self.bounded = False
                return
        self.code = "\n".join(code).lower()
        self.side = "\n".join(side).lower()

    def changes(self, target: str) -> bool:
        spellings = _spellings(target)
        if any(s in self.paths or s in self.code for s in spellings):
            return True
        return _is_test_target(target) and any(s in self.side for s in spellings)

    def only_names(self, target: str) -> bool:
        return any(s in self.side for s in _spellings(target))


def _anchor(hunks: Iterable[DiffHunk], code_files: set[str]) -> tuple[str, int] | None:
    for hunk in hunks:
        if hunk.file_path in code_files and hunk.new_lines:
            return hunk.file_path, min(hunk.new_lines)
    return None


def scan_wrong_target(
    changes: Iterable[FileChange],
    hunks: tuple[DiffHunk, ...],
    *,
    criteria: tuple[LinkedCriterion, ...],
    pr_title: str,
) -> tuple[Finding, ...]:
    """One finding when the diff changes none of the targets the ticket names
    but names one only in comments or tests while changing other code."""
    if not criteria:
        return ()
    targets = ticket_targets([pr_title or "", *(c.text for c in criteria)])
    if not targets or len(targets) > MAX_TARGETS:
        return ()
    diff = _ChangedText(changes, hunks)
    if not diff.bounded or not diff.code_files:
        return ()
    if any(diff.changes(t) for t in targets):
        return ()
    named = [t for t in targets if diff.only_names(t)]
    if not named:
        return ()
    anchor = _anchor(hunks, diff.code_files)
    if anchor is None:
        return ()
    listed = ", ".join(f"`{t}`" for t in targets[:8])
    more = f" and {len(targets) - 8} more" if len(targets) > 8 else ""
    issues = sorted({c.issue for c in criteria})
    refs = ", ".join(f"#{n}" for n in issues)
    return (Finding(
        file=anchor[0],
        line=anchor[1],
        severity="high",
        rule_name=RULE,
        message=(
            f"The linked criteria ({refs}) name {listed}{more}, and this diff changes "
            f"none of them: no changed path or changed line of code names one. It "
            f"names {', '.join(f'`{t}`' for t in named[:4])} only in comments or tests "
            "while the code it changes is elsewhere. Change the named targets, or "
            "say in the ticket why a different target meets it."
        ),
        suggestion=None,
        effort="heavy-lift",
    ),)
