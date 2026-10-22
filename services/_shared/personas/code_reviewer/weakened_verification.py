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
  rename comes back) or flips an assertion's polarity (`assertTrue` and
  `assertFalse`, `In` and `NotIn`, `assert x` and `assert not x`, `True`
  and `False` as the expected value). High when a deleted test's name is
  not mentioned by the title or the linked criteria, or when the PR flips
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
# Share of a renamed test's name words a replacement must keep to count as
# the same test under a new name.
RENAME_KEEP = 0.6
# Past this many hunk lines the scan is not evaluated: no finding.
MAX_DIFF_LINES = 200_000
# Lines longer than this are not matched (minified or generated content).
_MAX_LINE = 2_000
# Most deleted tests and flipped assertions one finding lists by name.
_MAX_NAMED = 6

_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
_CLASS_RE = re.compile(r"^\s*class\s+\w+")
_ASSERT_RE = re.compile(
    r"\bself\.assert\w*\(|^\s*assert\b|\bpytest\.raises\(|\.assert_\w+\(|\bexpect\("
)
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


@dataclass
class _TestFile:
    path: str
    incomplete: bool = False
    added: set[str] = field(default_factory=set)
    added_defs: set[str] = field(default_factory=set)
    removed: list[_Deleted] = field(default_factory=list)
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
    for hunk, lines in zip(hunks, bodies):
        _walk_hunk(tf, lines, _enclosing_removed(tf, hunk))
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


def _walk_hunk(tf: _TestFile, lines: list[str], current: _Deleted | None) -> None:
    """One pass over a hunk: removed test functions on the old side, the
    test added in place of each, and assertion polarity flips. `current` is
    a removed test whose body this hunk starts inside."""
    run_removed: list[str] = []
    run_added: list[str] = []
    added_pol: dict[str, set[int]] = {}
    removed_pol: list[tuple[str, str, int]] = []

    def close_run() -> None:
        for old, new in zip(run_removed, run_added):
            tf.replaced_by[old] = new
        run_removed.clear()
        run_added.clear()

    for raw in lines:
        if len(raw) > _MAX_LINE or _incomplete(raw):
            continue
        kind, text = raw[:1], raw[1:]
        if kind == "+":
            d = _DEF_RE.match(text)
            if d and d.group(1).startswith("test"):
                run_added.append(d.group(1))
            if _ASSERT_RE.search(text):
                p = _polarity(text)
                if p:
                    added_pol.setdefault(p[0], set()).add(p[1])
            continue
        if kind != "-":
            close_run()
        d = _DEF_RE.match(text)
        if d or _CLASS_RE.match(text):
            current = None
            if d and kind == "-" and d.group(1).startswith("test"):
                current = _Deleted(d.group(1))
                tf.removed.append(current)
                run_removed.append(current.name)
            continue
        if kind == "-" and _ASSERT_RE.search(text):
            if current is not None and text.strip() not in tf.added:
                current.asserts += 1
            p = _polarity(text)
            if p:
                removed_pol.append((text.strip(), p[0], p[1]))
    close_run()
    for shown, key, pol in removed_pol:
        if added_pol.get(key) == {1 - pol}:
            tf.flips.append(shown)


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
        d for d in tf.removed
        if d.asserts
        and d.name not in tf.added_defs
        and not _renamed(d.name, tf.replaced_by.get(d.name))
    ]


def _mentioned(name: str, ticket_stems: set[str]) -> bool:
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


def _test_findings(
    files: list[_TestFile], removed_files: list[str], hunks: tuple[DiffHunk, ...],
    ticket: str,
) -> list[Finding]:
    ticket_lower = ticket.lower()
    ticket_stems = {_stem(w) for w in re.findall(r"[a-z0-9]+", ticket_lower)}
    removal_ticket = bool(_REMOVAL_RE.search(ticket))
    unasked_flips = sum(
        1 for tf in files if not tf.incomplete
        for f in tf.flips if not _flip_asked(f, ticket_lower)
    )
    findings: list[Finding] = []
    for tf in files:
        if tf.incomplete:
            continue
        deleted = _deleted_tests(tf)
        if not deleted and not tf.flips:
            continue
        unnamed = [d for d in deleted if not _mentioned(d.name, ticket_stems)]
        file_unasked = [f for f in tf.flips if not _flip_asked(f, ticket_lower)]
        high = (bool(unnamed) and not removal_ticket) or (
            bool(file_unasked) and unasked_flips >= FLIPS_HIGH
        )
        anchor = _anchor(tf.hunks, hunks)
        if anchor is None:
            continue
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
        if high:
            why = (
                "Neither the PR title nor the linked criteria name these tests or "
                "the behavior they pinned, so the change passes by rewriting what "
                "the tests checked."
            )
        elif unnamed:
            why = (
                "The ticket asks for a removal, so this may be intended; confirm "
                "each test pinned something the ticket removes."
            )
        else:
            why = (
                "The ticket mentions this behavior, so this may be intended; "
                "confirm each one pinned something the ticket changes."
            )
        findings.append(Finding(
            file=anchor[0], line=anchor[1],
            severity="high" if high else "medium",
            rule_name=RULE_TEST,
            message=(
                f"In `{tf.path}`, this diff " + " and ".join(parts) + ". " + why
                + " Restore the tests that pinned behavior the ticket did not "
                "change, or name the change in the ticket."
            ),
            suggestion=None, effort="quick-win",
        ))
    for path in removed_files:
        anchor = _anchor((), hunks)
        if anchor is None:
            break
        findings.append(Finding(
            file=anchor[0], line=anchor[1], severity="medium", rule_name=RULE_TEST,
            message=(
                f"This diff deletes the test file `{path}`. Confirm its tests "
                "moved elsewhere or pinned behavior the ticket removes."
            ),
            suggestion=None, effort="quick-win",
        ))
    return findings


def _identity(line: str) -> str:
    """What a CI line declares, so a re-pinned action or changed value is
    a change, not a removal."""
    text = _USES_REF_RE.sub("", line) if "uses:" in line else line
    key = _KEY_RE.match(text)
    if key and _LAYER_RE.search(key.group(1)):
        return key.group(1)
    return text


def _layer_findings(
    by_file: dict[str, list[DiffHunk]], hunks: tuple[DiffHunk, ...], ticket: str,
    budget: list[int],
) -> list[Finding] | None:
    asked = any(
        _REMOVAL_RE.search(line) and _LAYER_RE.search(line)
        for line in ticket.splitlines()
    )
    findings: list[Finding] = []
    for path, own in by_file.items():
        if not _CI_PATH_RE.search(path):
            continue
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
        lost = [t for t in removed if _identity(t) not in added]
        layer = [t for t in lost if _LAYER_RE.search(t)]
        if not layer:
            continue
        support = [t for t in lost if _SUPPORT_RE.search(t) and t not in layer]
        anchor = _anchor(own, hunks)
        if anchor is None:
            continue
        findings.append(Finding(
            file=anchor[0], line=anchor[1],
            severity="medium" if asked else "high",
            rule_name=RULE_LAYER,
            message=(
                f"This diff removes a verification layer from `{path}`: "
                f"{_named_list(layer + support)} go and nothing in the diff "
                "replaces them. A check that no longer runs passes everything. "
                + ("The ticket asks for a removal; confirm it names this layer."
                   if asked else
                   "Neither the PR title nor the linked criteria ask to remove it; "
                   "restore the layer.")
            ),
            suggestion=None, effort="quick-win",
        ))
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
    removed_files = [
        c.path for c in changes if c.status == "removed" and is_test_file(c.path)
    ]
    layer = _layer_findings(by_file, hunks, ticket, budget)
    if layer is None:
        return ()
    return tuple(_test_findings(tests, removed_files, hunks, ticket) + layer)
