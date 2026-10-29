"""Deterministic check for lane work that passes by weakening its checks (#1367).

Recorded shapes, from lane pull requests Elder passed as test edits:
- a PR flipped a capability constant, then rewrote the tests that pinned
  the old truth: `assertTrue(X)` became `assertFalse(X)`, `assertNotIn`
  became `assertIn`, and the tests were renamed to match;
- a PR deleted a test that pinned a deploy-time warning along with the
  warning itself, and another deleted an operator-pinned test that a
  behavior never posts a comment;
- two PRs rewrote a leak-scan workflow to drop its deny-list layer (the
  deny-list parameter, its role secret and the `id-token: write` grant that
  minted the role's token), one adding a test that asserts the layer
  stays gone.

Two rules, each one finding per file, anchored on a new-side line:

- `weakened-test`: a test file whose diff deletes test functions (the
  `def` and at least one assertion go, and no test of that name or a close
  rename comes back; a counterpart in another file of the same diff counts
  only under the rule in `moved_tests`) or flips an assertion's polarity (`assertTrue` and
  `assertFalse`, `In` and `NotIn`, `assert x` and `assert not x`, `True`
  and `False` as the expected value). High when `DELETED_HIGH` or more
  deleted tests in a file are not mentioned by the title or the linked
  criteria, when an unmentioned deleted test goes with the production
  lines it pinned (`_goes_with_guard`: the diff also deletes non-test
  lines that carry most of the test's name, as a guard and its comment,
  or a docstring that states the pinned rule), or when the PR flips
  `FLIPS_HIGH` or more assertions none of whose strings or names the
  ticket mentions. Medium otherwise: the ticket names the test, asks to
  remove or retire something, or the PR flips fewer assertions.
- `removed-verification-layer` (high): a CI workflow loses a line of a
  security or verification layer (a deny-list, leak or secret scan,
  signature or attestation check, a code scanner) that the diff does not
  add back. Grants and credentials that only serve such a layer (an
  `id-token: write`, a role secret) are named with it. Medium when the
  ticket asks to remove that layer.

Not evaluated (no finding): a file whose hunks are incomplete (the diff
was cut short), and any diff past `MAX_DIFF_LINES`. Every scan is one
pass over the hunk lines plus set lookups, so cost is linear in the diff.

Pure: file changes, hunks and PR text in; findings out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import DiffHunk, FileChange
from personas.code_reviewer.moved_tests import (
    ASSERT_RE,
    CLASS_RE,
    DEF_RE,
    MOCK_ASSERT_RE,
    Added,
    NewSideReader,
    is_async,
    mark_moved,
    normal,
)
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

RULE_TEST = "weakened-test"
RULE_LAYER = "removed-verification-layer"
# Board categories for the two rules; dispatch reads them through
# linked_issue.RULE_CATEGORIES, so they render as their own category.
RULE_CATEGORIES = {RULE_TEST: "test fidelity", RULE_LAYER: "security"}

# Unasked polarity flips in one PR at or above which flipping is high: one
# flip can follow a behavior the ticket changed, a test file's truth
# rewritten wholesale cannot.
FLIPS_HIGH = 3
# Unnamed deleted tests in one file at or above which deleting is high.
# Measured on held-out good lane PRs: a single deleted test was as common in
# good work (a behavior the ticket changed on purpose) as in damaging work,
# so one deleted test is medium; two or more unnamed ones were damaging only.
DELETED_HIGH = 2
# A deleted test goes with its guard when at least this many of its name
# words, and at least half of them, appear in non-test lines the same diff
# deletes, one of them a word that states a limit (#1382, #1383).
GUARD_WORDS = 2
# Words that state a limit or a refusal: what a guard keeps from happening.
_LIMIT_WORDS = frozenset([
    "never", "held", "hold", "holds", "block", "blocked", "refuse", "refused", "deny",
    "denied", "reject", "rejected", "forbid", "forbidden", "prevent", "prevents",
    "skip", "skipped", "cap", "capped", "limit", "limited", "max", "only", "without",
])
# Share of a renamed test's name words a replacement must keep to count as
# the same test under a new name.
RENAME_KEEP = 0.6
# Past this many hunk lines the scan is not evaluated: no finding.
MAX_DIFF_LINES = 200_000
# Lines longer than this are not matched (minified or generated content).
_MAX_LINE = 2_000
# Most deleted tests and flipped assertions one finding lists by name.
_MAX_NAMED = 6

_DEF_RE = DEF_RE
_CLASS_RE = CLASS_RE
_ASSERT_RE = ASSERT_RE
# (negative, positive) assertion methods; the negative is matched first.
_POLARITY = (
    ("assertNotIn", "assertIn"),
    ("assertNotEqual", "assertEqual"),
    ("assertIsNotNone", "assertIsNone"),
    ("assertIsNot", "assertIs"),
    ("assertNotIsInstance", "assertIsInstance"),
    ("assertNotRegex", "assertRegex"),
    ("assertFalse", "assertTrue"),
)
_POLARITY_RE = re.compile(
    r"\b(" + "|".join(n for pair in _POLARITY for n in pair) + r")\b"
)
_POSITIVE = dict(_POLARITY)
_BARE_NOT_RE = re.compile(r"^assert\s+not\s+")
_BARE_NEG_RE = re.compile(r"\s(not in|is not|!=)\s")
_BARE_POS = {"not in": "in", "is not": "is", "!=": "=="}
_BOOL_RE = re.compile(r"\b(True|False)\b")
_STRING_RE = re.compile(r"\"([^\"\\]{4,})\"|'([^'\\]{4,})'")
_ATTR_RE = re.compile(r"\.([A-Za-z_]\w{3,})\b")

_STOP = frozenset(
    ["test", "a", "an", "the", "is", "it", "its", "and", "or", "of", "to", "in", "on", "for", "with", "by", "be", "not", "no", "but", "that", "this", "when", "as", "at", "are", "was", "does", "do", "from", "into", "than", "then", "if", "after", "before"]
)
# A ticket that asks for something to go. Verb forms only: "deletable"
# describes a feature, it does not ask for a deletion.
_REMOVAL_RE = re.compile(
    r"\b(?:remov(?:e|es|ed|ing|al)|retir(?:e|es|ed|ing|ement)"
    r"|delet(?:e|es|ed|ing|ion)|drop(?:s|ped|ping)?|deprecat(?:e|es|ed|ing)"
    r"|disabl(?:e|es|ed|ing)|rip(?:s|ped)? out)\b",
    re.IGNORECASE,
)

_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")

_CI_PATH_RE = re.compile(
    r"(?:^|/)\.github/workflows/[^/]+\.ya?ml$|(?:^|/)action\.ya?ml$"
    r"|(?:^|/)\.gitlab-ci\.ya?ml$|(?:^|/)\.circleci/[^/]+\.ya?ml$"
)
# A line that is part of a security or verification layer.
_LAYER_RE = re.compile(
    r"deny[-_ ]?list|block[-_ ]?list|leak[-_ ]?scan|secret[-_ ]?scan|gitleaks"
    r"|trufflehog|detect-secrets|cosign|sigstore|attest|signature|codeql"
    r"|semgrep|trivy|grype",
    re.IGNORECASE,
)
# A grant or credential that serves such a layer; named only alongside one.
_SUPPORT_RE = re.compile(
    r"id-token:\s*write|\$\{\{\s*secrets\.|role-to-assume|role-arn|\bssm\b",
    re.IGNORECASE,
)
_USES_REF_RE = re.compile(r"@\S+")
_KEY_RE = re.compile(r"^-?\s*([\w.-]+):")


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _name_words(name: str) -> list[str]:
    bare = name.removeprefix("test_")
    return [w for w in bare.lower().split("_") if len(w) > 2 and w not in _STOP]


def _incomplete(line: str) -> bool:
    """A diff annotation other than git's no-newline marker means the
    hunk was cut short, so what it deletes cannot be judged."""
    return line.startswith("\\") and not line.startswith("\\ No newline")


@dataclass
class _Deleted:
    name: str
    asserts: int = 0
    # Every assertion line the test loses, and their whitespace-normalized
    # forms: what a copy elsewhere in the diff must match (#1496).
    total: int = 0
    mock: int = 0
    is_async: bool = False
    norm: set[str] = field(default_factory=set)


@dataclass
class _TestFile:
    path: str
    incomplete: bool = False
    added: set[str] = field(default_factory=set)
    added_defs: set[str] = field(default_factory=set)
    removed: list[_Deleted] = field(default_factory=list)
    reader: NewSideReader = field(default_factory=NewSideReader)
    tests_seen: list[Added] = field(default_factory=list)
    # indexes into `removed` of tests that reappear in another file of the diff
    moved: set[int] = field(default_factory=set)
    # removed test name -> the test added in its place in the same change run
    replaced_by: dict[str, str] = field(default_factory=dict)
    flips: list[str] = field(default_factory=list)
    hunks: list[DiffHunk] = field(default_factory=list)


def _polarity(line: str) -> tuple[str, int] | None:
    """`(polarity-free form, polarity)` of an assertion line, or None when
    it has no polarity to flip. `True`/`False` as a value flips too."""
    text = line.strip()
    pol = 0
    m = _POLARITY_RE.search(text)
    if m and m.group(1) in _POSITIVE:
        text = text[: m.start()] + _POSITIVE[m.group(1)] + text[m.end():]
        pol ^= 1
    elif text.startswith("assert"):
        bare_not = _BARE_NOT_RE.match(text)
        if bare_not:
            text = "assert " + text[bare_not.end():]
            pol ^= 1
        neg = _BARE_NEG_RE.search(text)
        if neg:
            text = text[: neg.start(1)] + _BARE_POS[neg.group(1)] + text[neg.end(1):]
            pol ^= 1
    bools = _BOOL_RE.findall(text)
    if len(bools) == 1:
        text = _BOOL_RE.sub("True", text)
        pol ^= bools[0] == "False"
    if not m and not text.startswith("assert") and len(bools) != 1:
        return None
    return text, pol


def _read_test_file(path: str, hunks: list[DiffHunk], budget: list[int]) -> _TestFile | None:
    tf = _TestFile(path, hunks=hunks)
    bodies = []
    for hunk in hunks:
        lines = hunk.body.splitlines()[1:]
        budget[0] -= len(lines)
        if budget[0] < 0:
            return None
        bodies.append(lines)
        for raw in lines:
            if _incomplete(raw):
                tf.incomplete = True
            elif raw.startswith("+") and len(raw) <= _MAX_LINE:
                tf.added.add(raw[1:].strip())
                d = _DEF_RE.match(raw[1:])
                if d:
                    tf.added_defs.add(d.group(1))
    for hunk, lines in zip(hunks, bodies, strict=True):
        _walk_hunk(tf, lines, _enclosing_removed(tf, hunk))
        tf.reader.hunk_end()
    tf.tests_seen = tf.reader.finish()
    return tf


def _enclosing_removed(tf: _TestFile, hunk: DiffHunk) -> _Deleted | None:
    """The removed test an earlier hunk opened, when git names it as this
    hunk's enclosing function: its body continues here."""
    header = hunk.body.split("\n", 1)[0]
    _, _, context = header.partition(" @@")
    d = _DEF_RE.match(context.strip())
    if not d:
        return None
    return next((r for r in reversed(tf.removed) if r.name == d.group(1)), None)


@dataclass
class _HunkWalk:
    """State of one pass over a hunk (see `_walk_hunk`)."""

    tf: _TestFile
    current: _Deleted | None
    run_removed: list[str] = field(default_factory=list)
    run_added: list[str] = field(default_factory=list)
    added_pol: dict[str, set[int]] = field(default_factory=dict)
    removed_pol: list[tuple[str, str, int]] = field(default_factory=list)

    def close_run(self) -> None:
        """Pair the tests a change run removed with those it added, in order."""
        # strict=False: a run can remove more tests than it adds, or the
        # reverse; the unpaired ones are plain deletions or additions.
        for old, new in zip(self.run_removed, self.run_added, strict=False):
            self.tf.replaced_by[old] = new
        self.run_removed.clear()
        self.run_added.clear()

    def added_line(self, text: str) -> None:
        self.tf.reader.feed(text, added=True)
        d = _DEF_RE.match(text)
        if d and d.group(1).startswith("test"):
            self.run_added.append(d.group(1))
        if _ASSERT_RE.search(text):
            p = _polarity(text)
            if p:
                self.added_pol.setdefault(p[0], set()).add(p[1])

    def old_line(self, kind: str, text: str) -> None:
        """A removed or context line."""
        if kind != "-":
            self.close_run()
            self.tf.reader.feed(text, added=False)
        d = _DEF_RE.match(text)
        if d or _CLASS_RE.match(text):
            self._open(kind, d, text)
        elif kind == "-" and _ASSERT_RE.search(text):
            self._removed_assert(text)

    def _open(self, kind: str, d: re.Match[str] | None, text: str) -> None:
        """A def or class line ends the current test; a removed test def
        starts a new one."""
        self.current = None
        if d and kind == "-" and d.group(1).startswith("test"):
            self.current = _Deleted(d.group(1), is_async=is_async(text))
            self.tf.removed.append(self.current)
            self.run_removed.append(self.current.name)

    def _removed_assert(self, text: str) -> None:
        if self.current is not None:
            self.current.total += 1
            self.current.mock += bool(MOCK_ASSERT_RE.search(text))
            self.current.norm.add(normal(text))
        if self.current is not None and text.strip() not in self.tf.added:
            self.current.asserts += 1
        p = _polarity(text)
        if p:
            self.removed_pol.append((text.strip(), p[0], p[1]))

    def finish(self) -> None:
        self.close_run()
        for shown, key, pol in self.removed_pol:
            if self.added_pol.get(key) == {1 - pol}:
                self.tf.flips.append(shown)


def _walk_hunk(tf: _TestFile, lines: list[str], current: _Deleted | None) -> None:
    """One pass over a hunk: removed test functions on the old side, the
    test added in place of each, and assertion polarity flips. `current` is
    a removed test whose body this hunk starts inside."""
    walk = _HunkWalk(tf, current)
    for raw in lines:
        if len(raw) > _MAX_LINE or _incomplete(raw):
            continue
        kind, text = raw[:1], raw[1:]
        if kind == "+":
            walk.added_line(text)
        else:
            walk.old_line(kind, text)
    walk.finish()


def _renamed(old: str, new: str | None) -> bool:
    if new is None:
        return False
    if new.startswith(old):
        return True
    a, b = set(_name_words(old)), set(_name_words(new))
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= RENAME_KEEP


def _deleted_tests(tf: _TestFile) -> list[_Deleted]:
    return [
        d for j, d in enumerate(tf.removed)
        if d.asserts
        and j not in tf.moved
        and d.name not in tf.added_defs
        and not _renamed(d.name, tf.replaced_by.get(d.name))
    ]


def _removed_code_stems(by_file: dict[str, list[DiffHunk]]) -> frozenset[str]:
    """Word stems of the lines the diff deletes from non-test, non-doc
    files: the guards, constants, comments and docstrings that go."""
    stems: set[str] = set()
    for path, own in by_file.items():
        if is_test_file(path) or path.lower().endswith(_DOC_SUFFIXES):
            continue
        for hunk in own:
            for raw in hunk.body.splitlines()[1:]:
                if raw.startswith("-") and len(raw) <= _MAX_LINE:
                    stems.update(_stem(w) for w in re.findall(r"[a-z0-9]+", raw[1:].lower()))
    return frozenset(stems)


def _goes_with_guard(name: str, removed_stems: frozenset[str]) -> bool:
    """Most of the deleted test's name words appear in production lines the
    same diff deletes, one of them a limit ("never", "held", "blocked"):
    the guard and the test that pinned it go together, and nothing is left
    to notice."""
    words = {_stem(w) for w in _name_words(name)}
    shared = words & removed_stems
    limit = {_stem(w) for w in _LIMIT_WORDS}
    return (
        len(shared) >= GUARD_WORDS and len(shared) * 2 >= len(words)
        and bool(shared & limit)
    )


def _mentioned(name: str, ticket_stems: frozenset[str]) -> bool:
    words = _name_words(name)
    if not words:
        return True
    hits = sum(1 for w in words if _stem(w) in ticket_stems)
    return hits * 2 >= len(words)


def _flip_asked(assertion: str, ticket_lower: str) -> bool:
    """The ticket names one of the flipped assertion's strings or names."""
    for m in _STRING_RE.finditer(assertion):
        if (m.group(1) or m.group(2)).lower() in ticket_lower:
            return True
    return any(
        re.search(rf"(?<!\w){re.escape(m.group(1).lower())}(?!\w)", ticket_lower)
        for m in _ATTR_RE.finditer(assertion)
    )


def _anchor(own: Iterable[DiffHunk], every: Iterable[DiffHunk]) -> tuple[str, int] | None:
    """A new-side changed line: in the file's own hunks first, else the
    first hunk of the diff that has one (the message names the file)."""
    for group in (own, every):
        for hunk in group:
            if hunk.new_lines:
                return hunk.file_path, min(hunk.new_lines)
    return None


def _named_list(items: list[str]) -> str:
    shown = ", ".join(f"`{i}`" for i in items[:_MAX_NAMED])
    more = len(items) - _MAX_NAMED
    return shown + (f" and {more} more" if more > 0 else "")


@dataclass(frozen=True)
class _Ticket:
    """The PR title and linked criteria, in the forms the test rule reads."""

    lower: str
    stems: frozenset[str]
    removal: bool

    @classmethod
    def of(cls, ticket: str) -> _Ticket:
        lower = ticket.lower()
        return cls(
            lower=lower,
            stems=frozenset(_stem(w) for w in re.findall(r"[a-z0-9]+", lower)),
            removal=bool(_REMOVAL_RE.search(ticket)),
        )


def _unasked(flips: list[str], ticket: _Ticket) -> list[str]:
    return [f for f in flips if not _flip_asked(f, ticket.lower)]


def _change_parts(tf: _TestFile, deleted: list[_Deleted]) -> list[str]:
    """What the diff does to the file's tests, as message clauses."""
    parts = []
    if deleted:
        gone = sum(d.asserts for d in deleted)
        parts.append(
            f"deletes {len(deleted)} test(s) ({_named_list([d.name for d in deleted])}) "
            f"with {gone} assertion(s) the diff does not add back"
        )
    if tf.flips:
        parts.append(
            f"flips {len(tf.flips)} assertion(s) to the opposite expectation "
            f"({_named_list(tf.flips)})"
        )
    return parts


def _why(*, high: bool, unnamed: bool, guarded: list[str] = ()) -> str:
    if high and guarded:
        return (
            f"The diff also deletes the production lines {_named_list(guarded)} "
            "pinned, and neither the PR title nor the linked criteria ask for "
            "that, so the rule and the test that held it go together and "
            "nothing is left to catch the change."
        )
    if high:
        return (
            "Neither the PR title nor the linked criteria name these tests or "
            "the behavior they pinned, so the change passes by rewriting what "
            "the tests checked."
        )
    if unnamed:
        return (
            "The ticket asks for a removal, so this may be intended; confirm "
            "each test pinned something the ticket removes."
        )
    return (
        "The ticket mentions this behavior, so this may be intended; "
        "confirm each one pinned something the ticket changes."
    )


def _test_file_finding(
    tf: _TestFile, hunks: tuple[DiffHunk, ...], ticket: _Ticket, unasked_flips: int,
    removed_stems: frozenset[str] = frozenset(),
) -> Finding | None:
    """The `weakened-test` finding for one test file, or None."""
    deleted = _deleted_tests(tf)
    # A lone flip with nothing deleted most often follows a behavior the
    # ticket changed on purpose: on the lane corpus it was as common in
    # good work as in damaging work, so it is left to the model.
    if not deleted and unasked_flips < 2:
        return None
    unnamed = [d for d in deleted if not _mentioned(d.name, ticket.stems)]
    guarded = [] if ticket.removal else [
        d.name for d in unnamed if _goes_with_guard(d.name, removed_stems)
    ]
    high = (len(unnamed) >= DELETED_HIGH and not ticket.removal) or bool(guarded) or (
        bool(_unasked(tf.flips, ticket)) and unasked_flips >= FLIPS_HIGH
    )
    anchor = _anchor(tf.hunks, hunks)
    if anchor is None:
        return None
    return Finding(
        file=anchor[0], line=anchor[1],
        severity="high" if high else "medium",
        rule_name=RULE_TEST,
        message=(
            f"In `{tf.path}`, this diff " + " and ".join(_change_parts(tf, deleted))
            + ". " + _why(high=high, unnamed=bool(unnamed), guarded=guarded)
            + " Restore the tests that pinned behavior the ticket did not "
            "change, or name the change in the ticket."
        ),
        suggestion=None, effort="quick-win",
    )


def _removed_file_findings(
    removed_files: list[str], hunks: tuple[DiffHunk, ...],
) -> list[Finding]:
    anchor = _anchor((), hunks)
    if anchor is None:
        return []
    return [
        Finding(
            file=anchor[0], line=anchor[1], severity="medium", rule_name=RULE_TEST,
            message=(
                f"This diff deletes the test file `{path}`. Confirm its tests "
                "moved elsewhere or pinned behavior the ticket removes."
            ),
            suggestion=None, effort="quick-win",
        )
        for path in removed_files
    ]


def _test_findings(
    files: list[_TestFile], removed_files: list[str], hunks: tuple[DiffHunk, ...],
    ticket: str, removed_stems: frozenset[str] = frozenset(),
) -> list[Finding]:
    asked = _Ticket.of(ticket)
    complete = [tf for tf in files if not tf.incomplete]
    unasked_flips = sum(len(_unasked(tf.flips, asked)) for tf in complete)
    findings = [
        f for f in (
            _test_file_finding(tf, hunks, asked, unasked_flips, removed_stems)
            for tf in complete
        )
        if f is not None
    ]
    return findings + _removed_file_findings(removed_files, hunks)


def _identity(line: str) -> str:
    """What a CI line declares, so a re-pinned action or changed value is
    a change, not a removal."""
    text = _USES_REF_RE.sub("", line) if "uses:" in line else line
    key = _KEY_RE.match(text)
    if key and _LAYER_RE.search(key.group(1)):
        return key.group(1)
    return text


def _asks_layer_removal(ticket: str) -> bool:
    return any(
        _REMOVAL_RE.search(line) and _LAYER_RE.search(line)
        for line in ticket.splitlines()
    )


def _ci_changes(
    own: list[DiffHunk], budget: list[int],
) -> tuple[list[str], set[str]] | None:
    """(removed lines, identities of added lines) of one CI file, or None
    past the line budget. Blank, comment and overlong lines are skipped."""
    removed: list[str] = []
    added: set[str] = set()
    for hunk in own:
        lines = hunk.body.splitlines()[1:]
        budget[0] -= len(lines)
        if budget[0] < 0:
            return None
        for raw in lines:
            text = raw[1:].strip()
            if len(raw) > _MAX_LINE or not text or text.startswith("#"):
                continue
            if raw.startswith("+"):
                added.add(_identity(text))
            elif raw.startswith("-"):
                removed.append(text)
    return removed, added


def _layer_finding(
    anchor: tuple[str, int], path: str, gone: list[str], *, asked: bool,
) -> Finding:
    return Finding(
        file=anchor[0], line=anchor[1],
        severity="medium" if asked else "high",
        rule_name=RULE_LAYER,
        message=(
            f"This diff removes a verification layer from `{path}`: "
            f"{_named_list(gone)} go and nothing in the diff "
            "replaces them. A check that no longer runs passes everything. "
            + ("The ticket asks for a removal; confirm it names this layer."
               if asked else
               "Neither the PR title nor the linked criteria ask to remove it; "
               "restore the layer.")
        ),
        suggestion=None, effort="quick-win",
    )


def _layer_findings(
    by_file: dict[str, list[DiffHunk]], hunks: tuple[DiffHunk, ...], ticket: str,
    budget: list[int],
) -> list[Finding] | None:
    asked = _asks_layer_removal(ticket)
    findings: list[Finding] = []
    for path, own in by_file.items():
        if not _CI_PATH_RE.search(path):
            continue
        changed = _ci_changes(own, budget)
        if changed is None:
            return None
        removed, added = changed
        lost = [t for t in removed if _identity(t) not in added]
        layer = [t for t in lost if _LAYER_RE.search(t)]
        if not layer:
            continue
        support = [t for t in lost if _SUPPORT_RE.search(t) and t not in layer]
        anchor = _anchor(own, hunks)
        if anchor is None:
            continue
        findings.append(_layer_finding(anchor, path, layer + support, asked=asked))
    return findings


def scan_weakened_verification(
    changes: Iterable[FileChange],
    hunks: tuple[DiffHunk, ...],
    *,
    criteria: tuple[LinkedCriterion, ...],
    pr_title: str,
) -> tuple[Finding, ...]:
    """`weakened-test` and `removed-verification-layer` findings, or none
    when the diff is past `MAX_DIFF_LINES` (not evaluated)."""
    ticket = "\n".join([pr_title or "", *(c.text for c in criteria)])
    by_file: dict[str, list[DiffHunk]] = {}
    for hunk in hunks:
        by_file.setdefault(hunk.file_path, []).append(hunk)
    budget = [MAX_DIFF_LINES]
    tests: list[_TestFile] = []
    for path, own in by_file.items():
        if is_test_file(path):
            tf = _read_test_file(path, own, budget)
            if tf is None:
                return ()
            tests.append(tf)
    mark_moved(tests)
    removed_files = [
        c.path for c in changes if c.status == "removed" and is_test_file(c.path)
    ]
    layer = _layer_findings(by_file, hunks, ticket, budget)
    if layer is None:
        return ()
    return tuple(
        _test_findings(tests, removed_files, hunks, ticket, _removed_code_stems(by_file))
        + layer
    )
