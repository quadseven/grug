"""Tests moved between files of one diff (#1496).

`weakened-test` reads each test file on its own, so a pull request that
splits a test file removes tests from one file and adds them to others, and
every removed test looks deleted. This module reads the new side of each
test file (`NewSideReader`) and matches a removed test to a test the diff
adds in ANOTHER file (`mark_moved`).

A removed test counts as moved only when an ADDED test (its `def` is a `+`
line, never a context line) in another test file of the same diff:

- can run: it is not skipped or xfail (a skip or xfail decorator, possibly
  over several lines; a class, or an enclosing class, that is skipped; a
  leading `pytest.skip()` or `skipTest()` after any docstring; a module
  `pytestmark` that skips, a module-level `pytest.importorskip` or
  `pytest.skip`; a test under `if False:` or `if 0:`; a test a later
  definition of the same name in the same scope shadows), is the same
  kind (async or sync) as the removed test, and keeps at least as many
  assertion lines; its assertions are not all mock call checks unless the
  removed test's were too; and
- matches by one of two routes, each needing at least one shared
  normalized assertion line: the same name and STRICTLY MORE THAN HALF of
  the removed test's normalized assertion lines (strength 0 when the sets
  are equal, else 1); or the same normalized assertion set under any name
  when the removed test has two or more assertions (strength 2; a
  one-assertion test needs its name).

Each counterpart excuses at most one removed test. Pairs are taken
strongest first, then in (path, index) order, so the result does not depend
on the order of the files in the diff. One index per diff, by name and by
assertion set, keeps the cost near linear; each lookup scans at most
`SCAN` counterparts, in (path, index) order. Past that, extras can be marked
even if genuinely moved (more than `SCAN` added tests of one name, or more
than `SCAN` removed tests sharing one assertion set): the safe direction.

Known limits: an aliased skip decorator is not seen, and a hunk is read on
its own (class and `if` scope do not carry across hunks).

Pure: lines in, indexes out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Counterparts scanned per lookup key (see the module docstring).
SCAN = 32

DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
CLASS_RE = re.compile(r"^\s*class\s+(\w+)")
ASSERT_RE = re.compile(
    r"\bself\.assert\w*\(|^\s*assert\b|\bpytest\.raises\(|\.assert_\w+\(|\bexpect\("
)
MOCK_ASSERT_RE = re.compile(r"\.assert_(?:not_)?called\w*\(")

_SKIP_RE = re.compile(
    r"\b(?:unittest\.(?:skip\w*|expectedFailure)|pytest\.mark\.(?:skip\w*|xfail)"
    r"|skip(?:If|Unless)?|expectedFailure)\b"
)
_SKIP_CALL_RE = re.compile(r"\b(?:pytest\.skip|self\.skipTest|unittest\.SkipTest)\(")
_FILE_SKIP_RE = re.compile(
    r"^(?:pytestmark\s*=.*\bpytest\.mark\.(?:skip\w*|xfail)\b"
    r"|.*\bpytest\.(?:importorskip|skip)\()"
)
_DEAD_RE = re.compile(r"^if\s+(?:False|0)\s*:")
_HEADS = ("class", "def", "async")
_TRIPLE = ('"""', "'''")


def normal(line: str) -> str:
    return "".join(line.split())


def is_async(text: str) -> bool:
    return text.lstrip().startswith("async")


@dataclass
class Added:
    """A test the new side shows: name, assertions, and whether it can run."""

    name: str
    scope: tuple[str, ...] = ()
    index: int = 0
    is_async: bool = False
    candidate: bool = True
    total: int = 0
    mock: int = 0
    skipped: bool = False
    norm: set[str] = field(default_factory=set)


def paren_change(line: str, quote: str = "") -> tuple[int, str]:
    """Net `(` minus `)` of a line outside string literals, and the triple
    quote still open at its end (only a triple-quoted string spans lines;
    a comment ends the scan). `quote` is the triple quote open at its start."""
    depth = 0
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and len(quote) == 1:
                i += 2
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
        elif c == "#":
            break
        elif c in "'\"":
            quote = c * 3 if line.startswith(c * 3, i) else c
            i += len(quote)
            continue
        else:
            depth += (c == "(") - (c == ")")
        i += 1
    return depth, quote if len(quote) == 3 else ""


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


@dataclass
class NewSideReader:
    """A line-based state machine over the new side of one test file: added
    and context lines, hunk by hunk. Feed lines in order, call `hunk_end`
    between hunks, then read `tests` (see `finish`)."""

    tests: list[Added] = field(default_factory=list)
    file_skip: bool = False
    # open scopes, innermost last: (indent, class name or None, skips)
    _scopes: list[tuple[int, str | None, bool]] = field(default_factory=list)
    _pending: bool = False
    _deco_depth: int = 0
    _sig_depth: int = 0
    _open_quote: str = ""
    _cur: Added | None = None
    _cur_indent: int = 0
    _body: str = "first"  # first | doc | done
    _quote: str = ""

    def hunk_end(self) -> None:
        self._scopes.clear()
        self._pending = False
        self._deco_depth = 0
        self._sig_depth = 0
        self._open_quote = ""
        self._cur = None

    def _continuation(self, stripped: str) -> bool:
        """A line that continues a signature, a decorator or a docstring."""
        if self._sig_depth > 0:
            change, self._open_quote = paren_change(stripped, self._open_quote)
            self._sig_depth += change
            return True
        if self._deco_depth > 0:
            self._pending = self._pending or bool(_SKIP_RE.search(stripped))
            change, self._open_quote = paren_change(stripped, self._open_quote)
            self._deco_depth += change
            return True
        if self._cur is not None and self._body == "doc":
            if self._quote in stripped:
                self._body = "first"
            return True
        return False

    def _close_scopes(self, ind: int) -> None:
        if self._cur is not None and ind <= self._cur_indent:
            self._cur = None
        while self._scopes and self._scopes[-1][0] >= ind:
            self._scopes.pop()

    def feed(self, text: str, *, added: bool) -> None:
        stripped = text.strip()
        if self._continuation(stripped) or not stripped or stripped.startswith("#"):
            return
        if not stripped.strip("),]:"):
            return  # a line that only closes a bracket is not a statement
        ind = _indent(text)
        if ind == 0 and _FILE_SKIP_RE.match(stripped):
            self.file_skip = True
        self._close_scopes(ind)
        if stripped.startswith("@"):
            self._pending = self._pending or bool(_SKIP_RE.search(stripped))
            change, self._open_quote = paren_change(stripped)
            self._deco_depth = max(0, change)
            return
        self._statement(text, stripped, ind, added=added)
        self._pending = False

    def _statement(self, text: str, stripped: str, ind: int, *, added: bool) -> None:
        inherited = any(s[2] for s in self._scopes)
        head = stripped.startswith(_HEADS)
        cls = CLASS_RE.match(text) if head else None
        d = DEF_RE.match(text) if head else None
        if cls:
            self._scopes.append((ind, cls.group(1), self._pending or inherited))
        elif _DEAD_RE.match(stripped):
            self._scopes.append((ind, None, True))
        elif d and (self._cur is None or ind <= self._cur_indent):
            test = Added(
                d.group(1),
                scope=tuple(s[1] for s in self._scopes if s[1]),
                index=len(self.tests), is_async=is_async(text),
                candidate=added and d.group(1).startswith("test"),
                skipped=self._pending or inherited,
            )
            self.tests.append(test)
            self._cur, self._cur_indent, self._body = test, ind, "first"
            change, self._open_quote = paren_change(stripped)
            self._sig_depth = max(0, change)
        elif self._cur is not None:
            self._body_line(text, stripped)

    def _body_line(self, text: str, stripped: str) -> None:
        cur = self._cur
        if cur is None:
            return
        if self._body == "first":
            quote = next((q for q in _TRIPLE if stripped.startswith(q)), "")
            if quote:
                if stripped.count(quote) < 2:
                    self._body, self._quote = "doc", quote
                return
            self._body = "done"
            if _SKIP_CALL_RE.search(stripped):
                cur.skipped = True
        if ASSERT_RE.search(text):
            cur.total += 1
            cur.mock += bool(MOCK_ASSERT_RE.search(text))
            cur.norm.add(normal(text))

    def finish(self) -> list[Added]:
        """The tests, with the ones a module-level skip, or a later
        definition of the same name in the same scope, keeps from running
        marked skipped."""
        last: dict[tuple[tuple[str, ...], str], int] = {}
        for t in self.tests:
            last[(t.scope, t.name)] = t.index
        for t in self.tests:
            if self.file_skip or last[(t.scope, t.name)] != t.index:
                t.skipped = True
        return self.tests


def _usable(d: Any, path: str, p: str, a: Added) -> bool:
    return (
        p != path and a.total >= d.total and a.is_async == d.is_async
        and (a.mock < a.total or d.mock == d.total)
    )


def _strength(d: Any, a: Added) -> int | None:
    """0 for the same name and the same assertion set, 1 for the same name
    and strictly more than half the removed assertion lines, 2 for the same
    assertion set under another name (two or more assertions), else None."""
    if a.name == d.name:
        shared = len(d.norm & a.norm)
        if shared * 2 > len(d.norm):
            return 0 if a.norm == d.norm else 1
    if d.total > 1 and a.norm == d.norm:
        return 2
    return None


def _collected_never(path: str) -> bool:
    """A conftest.py is never collected as tests (its removed tests still count)."""
    return path.rsplit("/", 1)[-1] == "conftest.py"


def _index_added(files: list[Any]) -> tuple[dict, dict]:
    """Counterpart candidates by assertion set and by name, each list in
    (path, index) order."""
    by_set: dict[frozenset[str], list[tuple[str, Added]]] = {}
    by_name: dict[str, list[tuple[str, Added]]] = {}
    for tf in files:
        if tf.incomplete:
            continue
        for a in tf.tests_seen:
            if a.candidate and a.total and not a.skipped and not _collected_never(tf.path):
                by_set.setdefault(frozenset(a.norm), []).append((tf.path, a))
                by_name.setdefault(a.name, []).append((tf.path, a))
    for index in (by_set, by_name):
        for entries in index.values():
            entries.sort(key=lambda e: (e[0], e[1].index))
    return by_set, by_name


def mark_moved(files: list[Any]) -> None:
    """Fill each file's `moved` with the indexes of its removed tests that
    have a counterpart (see the module docstring). `files` carry `path`,
    `incomplete`, `tests_seen` (`Added`) and `removed` (name, total, mock,
    norm, is_async).

    The pairs are taken in the order (strength, removed path, removed index,
    counterpart path, counterpart index): one pass per strength over the
    removed tests in path order, each taking its first free counterpart."""
    by_set, by_name = _index_added(files)
    taken: set[tuple[str, int]] = set()
    done: set[tuple[str, int]] = set()
    for tier in (0, 1, 2):
        for tf in sorted(files, key=lambda tf: tf.path):
            for j, d in enumerate(tf.removed):
                if not d.total or (tf.path, j) in done:
                    continue
                pool = by_set.get(frozenset(d.norm), ()) if tier == 2 else by_name.get(d.name, ())
                found = _first_free(d, tf.path, pool, tier, taken)
                if found is not None:
                    taken.add(found)
                    done.add((tf.path, j))
                    tf.moved.add(j)


def _first_free(
    d: Any, path: str, pool: list[tuple[str, Added]], tier: int, taken: set[tuple[str, int]],
) -> tuple[str, int] | None:
    """The first counterpart of strength `tier` among the first `SCAN` of
    `pool` that no other removed test has taken."""
    for p, a in pool[:SCAN]:
        if (p, a.index) in taken or not _usable(d, path, p, a):
            continue
        if _strength(d, a) == tier:
            return p, a.index
    return None
