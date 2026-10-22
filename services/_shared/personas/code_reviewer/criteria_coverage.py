"""Deterministic criteria coverage for Elder (#1179).

A lane PR whose ticket asked for runtime behavior shipped one helper
that nothing outside its own tests called, plus two unit tests. Elder
read the ticket's three criteria, reported clear, and the PR merged
having met none of them. Reading a criterion is not checking that the
diff can meet it; this module is that check, deterministic so the
outcome does not hang on the model noticing.

Two findings, both anchored on a diff line:
- `dead-code` (medium): a function or method this diff adds to a
  non-test Python file that no non-test code reaches. A helper called
  only by another uncalled new helper is dead too, but an uncalled call
  tree gets ONE marking, on its root, naming the helpers it strands: one
  marking per helper buried a single wiring gap under dozens of threads.
  Exempt, because something other than a by-name call runs them:
  decorated defs (the decorator registers them), dunder methods, methods
  of a class with a base other than `object` (an override the base class
  dispatches to), `upgrade`/`downgrade` hooks in an Alembic revision
  file, and names the PR body declares an entry point (a CLI command, a
  route, an exported API).
- `intent-mismatch` (high): when no non-test hunk can change runtime
  behavior (every production change is docs, imports, or an uncalled new
  function), every linked criterion that describes runtime behavior is
  unmet. One finding names them all, because findings dedup on
  (file, line, rule). Criteria starting "Live:" are checked after deploy
  and are exempt, as are criteria about tests or docs, which test and
  doc hunks can meet.

A new script or module that no other non-test file in the diff imports,
runs or names (#1364) cannot change runtime behavior either: nothing
loads it. A lane PR shipped a 139-line standalone script and a rewritten
README against three live host criteria, and the script's own `__main__`
block counted as a runtime change, so the check stayed silent. Such a
file counts as inert here, unless it sits where a framework finds files
by location (migrations, workflows, plugins) or the PR body declares it
an entry point.

`Live:` criteria are exempt only while the diff changes something that
runs: a deploy can then make them true. When nothing in the diff runs,
no deploy can, and they are unmet like the rest.

Pure: hunks, head file contents, criteria and PR text in; findings out.
Fail-open: a production file whose head text is missing or does not
parse counts as able to change runtime behavior, so it can never cause
an unmet-criterion finding.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from personas.code_reviewer.diff_parser import DiffHunk, added_lines
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
# Repository housekeeping files: they change what git or an editor sees,
# never what runs (#1364).
_INERT_NAMES = frozenset({".gitignore", ".gitattributes", ".dockerignore", ".editorconfig"})
_LIVE_RE = re.compile(r"^\s*live\s*:", re.IGNORECASE)
_TEST_CRITERION_RE = re.compile(
    r"\btest(?:s|ed|ing)?\b|\btest_\w+|\bassert(?:s|ion|ions)?\b|\bcoverage\b"
    r"|\bpytest\b|\bflak(?:y|e|es|iness)\b",
    re.IGNORECASE,
)
_DOC_CRITERION_RE = re.compile(
    r"\b(?:readme|docs?|documentation|documented|documents|changelog|adr"
    r"|runbooks?|guide|guidance|playbooks?|docstrings?)\b",
    re.IGNORECASE,
)
# A criterion about the pull request itself (it lands, its checks pass, its
# threads are answered, an older one is closed): the review process meets
# it, not a hunk.
_PROCESS_CRITERION_RE = re.compile(
    r"\bpull requests?\b|\breview threads?\b|\bchecks? pass", re.IGNORECASE,
)
# A criterion about the state of a running system: a host, a network, a
# deploy. Only a change to what manages that state (config, IaC, the code
# the deploy runs) or an operator action can make it true (#1364).
_LIVE_STATE_RE = re.compile(
    r"^\s*live\s*:"
    r"|\bon (?:the |each |both |every |all )?(?:hosts?|servers?|nodes?|devices?|handsets?"
    r"|phones?|routers?|cluster|machines?)\b"
    # "on build-host-2", "on <host>": a named machine.
    r"|\bon `?<?[a-z0-9]+-[a-z0-9-]+>?`?(?=[\s,.;:)]|$)|\bon <host>"
    r"|\bports?\b|\bvlans?\b|\bacls?\b|\bfirewall\b|\breachab(?:le|ility)\b"
    r"|\bunreachable\b|\bin production\b|\buptime\b|\bkubectl\b|\bcurl\b|\bssh\b"
    # A measurement needs a run; a diff that only adds the tool has not run it.
    r"|\bmeasured\b|\bmeasurements?\b|\brun-rate\b",
    re.IGNORECASE,
)
_ENTRY_POINT_RE = re.compile(
    r"\bentry[ -]?points?\b|\bcli\b|\bcommands?\b|\broutes?\b|"
    r"\bendpoints?\b|\b(?:exported|public)\s+api\b",
    re.IGNORECASE,
)

_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef

# New files of these kinds run only when something names them: an import,
# a `source`, a workflow step, a Makefile target. Compiled languages whose
# build globs a directory (Go, Rust, Swift, C) are left out on purpose.
_SCRIPT_SUFFIXES = (
    ".py", ".sh", ".bash", ".zsh", ".ps1", ".rb", ".pl",
    ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx",
)
# Directories a framework or runner scans by location, so a new file there
# runs without any other file naming it.
_DISCOVERED_DIRS = frozenset({
    ".github", "migrations", "versions", "alembic", "plugins", "hooks",
    "pages", "routes", "commands", "management", "fixtures",
})
# Basenames a runner looks for by name.
_DISCOVERED_NAMES = frozenset({
    "__init__.py", "__main__.py", "conftest.py", "setup.py", "noxfile.py",
    "manage.py", "main.py", "wsgi.py", "asgi.py", "index.js", "index.ts",
})
# Stems too common to prove a reference by name alone: matching them would
# be luck, so a file with such a stem counts as referenced (fail-open).
_GENERIC_STEMS = frozenset({
    "app", "base", "cli", "common", "config", "core", "helpers", "index",
    "lib", "main", "models", "run", "server", "types", "util", "utils",
})

# Alembic loads a revision module and calls these by convention.
_ALEMBIC_MARKERS = frozenset({"revision", "down_revision"})
_ALEMBIC_HOOK_RE = re.compile(r"^(?:upgrade|downgrade)(?:_\w+)?$")


@dataclass(frozen=True, slots=True)
class LinkedCriterion:
    """One acceptance criterion from an issue the PR closes."""

    issue: int
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class _NewDef:
    file: str
    line: int
    name: str
    end: int


@dataclass(frozen=True, slots=True)
class _ParsedFile:
    tree: ast.Module
    added: frozenset[int]


def _is_doc_path(path: str) -> bool:
    return path.lower().endswith(_DOC_SUFFIXES)


def is_production_path(path: str) -> bool:
    return (
        not is_test_file(path) and not _is_doc_path(path)
        and path.rsplit("/", 1)[-1] not in _INERT_NAMES
    )


def _parse(source: str | None) -> ast.Module | None:
    if source is None:
        return None
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _defs(tree: ast.Module) -> Iterator[tuple[_FuncDef, ast.ClassDef | None]]:
    """Module-level functions and the methods of module-level classes,
    each with its enclosing class."""
    for node in tree.body:
        if isinstance(node, _FuncDef):
            yield node, None
        elif isinstance(node, ast.ClassDef):
            yield from ((n, node) for n in node.body if isinstance(n, _FuncDef))


def _has_base(cls: ast.ClassDef | None) -> bool:
    """A base other than `object`: the base class may call any method by
    dispatch, which a by-name search cannot see."""
    if cls is None:
        return False
    return any(not (isinstance(b, ast.Name) and b.id == "object") for b in cls.bases)


def _is_alembic_revision(tree: ast.Module) -> bool:
    """Module-level `revision` and `down_revision` assignments."""
    assigned: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        else:
            continue
        assigned.update(t.id for t in targets if isinstance(t, ast.Name))
    return _ALEMBIC_MARKERS <= assigned


def _new_defs(path: str, parsed: _ParsedFile) -> list[_NewDef]:
    """Defs whose `def` line this diff adds, minus those something other
    than a by-name call runs (see the module docstring)."""
    alembic = _is_alembic_revision(parsed.tree)
    return [
        _NewDef(path, node.lineno, node.name, node.end_lineno or node.lineno)
        for node, cls in _defs(parsed.tree)
        if node.lineno in parsed.added
        and not node.decorator_list
        and not (node.name.startswith("__") and node.name.endswith("__"))
        and not _has_base(cls)
        and not (alembic and cls is None and _ALEMBIC_HOOK_RE.match(node.name))
    ]


def _references(tree: ast.Module) -> Iterator[tuple[str, int]]:
    """(name, line) for every name, attribute, identifier string and import."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id, node.lineno
        elif isinstance(node, ast.Attribute):
            yield node.attr, node.lineno
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.isidentifier():
                yield node.value, node.lineno
        elif isinstance(node, ast.ImportFrom):
            yield from ((a.name, node.lineno) for a in node.names)


def _declared_entry_points(pr_body: str, names: set[str]) -> set[str]:
    """Names the PR body mentions alongside entry-point language."""
    if not _ENTRY_POINT_RE.search(pr_body):
        return set()
    return {n for n in names if re.search(rf"\b{re.escape(n)}\b", pr_body)}


def _entry_point_text(pr_body: str) -> str:
    """The PR body when it uses entry-point language, else nothing: a
    file it then names is declared an entry point."""
    return pr_body if _ENTRY_POINT_RE.search(pr_body or "") else ""


def _reached(
    candidate: _NewDef, dead: set[_NewDef],
    py_refs: dict[str, list[tuple[str, int]]], other_text: str,
) -> bool:
    """A reference to `candidate` from code outside every still-dead def."""
    if re.search(rf"\b{re.escape(candidate.name)}\b", other_text):
        return True
    for path, refs in py_refs.items():
        spans = [(d.line, d.end) for d in dead if d.file == path]
        for name, line in refs:
            if name == candidate.name and not any(a <= line <= b for a, b in spans):
                return True
    return False


def _dead_defs(
    parsed: dict[str, _ParsedFile], py_refs: dict[str, list[tuple[str, int]]],
    other_text: str, pr_body: str,
) -> set[_NewDef]:
    """New defs no non-test code reaches, to a fixpoint: a def reached only
    from inside another dead def stays dead."""
    candidates = {d for path, p in parsed.items() for d in _new_defs(path, p)}
    exempt = _declared_entry_points(pr_body, {d.name for d in candidates})
    dead = {d for d in candidates if d.name not in exempt}
    while True:
        live = {d for d in dead if _reached(d, dead, py_refs, other_text)}
        if not live:
            return dead
        dead -= live


def _callees(
    dead: list[_NewDef], py_refs: dict[str, list[tuple[str, int]]],
) -> dict[_NewDef, set[_NewDef]]:
    """For each dead def, the other dead defs it names inside its body."""
    by_name: dict[str, list[_NewDef]] = {}
    for d in dead:
        by_name.setdefault(d.name, []).append(d)
    edges: dict[_NewDef, set[_NewDef]] = {d: set() for d in dead}
    for caller in dead:
        for name, line in py_refs.get(caller.file, ()):
            if caller.line <= line <= caller.end:
                edges[caller].update(c for c in by_name.get(name, ()) if c != caller)
    return edges


def _call_trees(
    dead: list[_NewDef], py_refs: dict[str, list[tuple[str, int]]],
) -> list[tuple[_NewDef, list[_NewDef]]]:
    """Each uncalled root with the dead helpers it reaches, in `dead`'s
    order. A root is a dead def no other dead def names; a cycle with no
    such root takes its first def as the root, so every dead def is
    reported once at least."""
    edges = _callees(dead, py_refs)
    called: set[_NewDef] = set().union(*edges.values())
    roots = [d for d in dead if d not in called]
    covered: set[_NewDef] = set()
    trees: list[tuple[_NewDef, list[_NewDef]]] = []
    # Roots first; then any def left over (a cycle) roots its own tree.
    for root in roots + [d for d in dead if d in called]:
        if root in covered:
            continue
        seen, stack = {root}, [root]
        while stack:
            for callee in edges[stack.pop()] - seen:
                seen.add(callee)
                stack.append(callee)
        covered |= seen
        trees.append((root, [d for d in dead if d in seen and d != root]))
    order = {d: i for i, d in enumerate(dead)}
    return sorted(trees, key=lambda t: order[t[0]])


def _is_discovered(path: str) -> bool:
    parts = path.split("/")
    return parts[-1] in _DISCOVERED_NAMES or any(
        p in _DISCOVERED_DIRS for p in parts[:-1]
    )


def _name_tokens(path: str) -> set[str]:
    """Spellings another file would use to load `path`: the path, the
    basename, the stem and, for Python, the dotted module path."""
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    tokens = {path, name}
    if len(stem) >= 4:
        tokens.add(stem)
    if path.endswith(".py"):
        tokens.add(path[: -len(".py")].replace("/", "."))
    return tokens


# Bounds on the reference scan, so a huge or adversarial diff cannot make
# a review hang: past either, the scan is NOT EVALUATED and no file is
# called inert, which leaves the #1179 behavior (the check can then only
# stay silent about inert files, never call a diff clean because of them).
MAX_REFERENCE_CANDIDATES = 200
MAX_REFERENCE_SCAN_CHARS = 4_000_000
# Path-like words in added text: one linear pass splits a file into these.
_PATH_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


def _word_keys(word: str) -> set[str]:
    """The spellings of a file one path-like word can name: the word, its
    last path segment, that segment without its extension, and the last
    part of a dotted Python import (`pkg.mod.name` names `name.py`)."""
    word = word.rstrip(".-/")
    tail = word.rsplit("/", 1)[-1]
    return {word, word.lstrip("./"), tail, tail.rsplit(".", 1)[0], tail.rsplit(".", 1)[-1]}


def _mentions(text: str, index: dict[str, set[str]]) -> set[str]:
    """Candidate files the text names, by one pass over its words."""
    found: set[str] = set()
    for match in _PATH_WORD_RE.finditer(text):
        for key in _word_keys(match.group(0)):
            found |= index.get(key, set())
    return found


def _reference_candidates(new_files: frozenset[str]) -> set[str]:
    """New production scripts and modules outside discovered locations,
    with a stem specific enough to prove a mention."""
    return {
        path for path in new_files
        if is_production_path(path)
        and path.endswith(_SCRIPT_SUFFIXES)
        and not _is_discovered(path)
        and path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() not in _GENERIC_STEMS
    }


def _added_text_by_file(
    hunks: tuple[DiffHunk, ...], counts: Callable[[str, str], bool] | None = None,
) -> dict[str, str] | None:
    """Each file's added text (only the lines `counts` accepts, when given),
    or None past `MAX_REFERENCE_SCAN_CHARS`."""
    lines_by_file: dict[str, list[str]] = {}
    size = 0
    for hunk in hunks:
        for _, text in added_lines(hunk):
            size += len(text) + 1
            if counts is None or counts(hunk.file_path, text):
                lines_by_file.setdefault(hunk.file_path, []).append(text)
        if size > MAX_REFERENCE_SCAN_CHARS:
            return None
    return {path: "\n".join(lines) for path, lines in lines_by_file.items()}


def _reached_candidates(candidates: set[str], edges: dict[str, set[str]]) -> set[str]:
    """Candidates some non-candidate file reaches through the name graph."""
    stack = [p for p in edges if p not in candidates]
    seen = set(stack)
    while stack:
        for target in edges.get(stack.pop(), ()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return candidates & seen


def unreferenced_new_files(
    new_files: frozenset[str], hunks: tuple[DiffHunk, ...], pr_body: str,
    *, counts: Callable[[str, str], bool] | None = None,
) -> frozenset[str] | None:
    """New production scripts and modules no other file in the diff names,
    or None when the diff is past the scan bounds (not evaluated).

    A file named only by other unreferenced files is unreferenced too: the
    names form a graph, and a candidate is referenced when some file that
    is not a candidate reaches it. A test or doc that names it counts as a
    reference: a tested or documented tool is meant to be run, and whether
    running it can meet a criterion is the live-state check's question.

    `counts(path, line)`: when given, only the added lines it accepts can
    name a file, so a stricter caller can leave out test files or lines
    that only package a file (`dead_on_arrival`).

    Cost: one index of candidate spellings, one linear word scan of each
    file's added text, one graph traversal."""
    candidates = _reference_candidates(new_files)
    if not candidates:
        return frozenset()
    if len(candidates) > MAX_REFERENCE_CANDIDATES:
        return None
    added = _added_text_by_file(hunks, counts)
    if added is None:
        return None
    index: dict[str, set[str]] = {}
    for path in candidates:
        for token in _name_tokens(path):
            index.setdefault(token, set()).add(path)
    candidates -= _mentions(_entry_point_text(pr_body), index)
    # Edges: file -> candidates its added text names (never itself).
    edges = {path: _mentions(text, index) - {path} for path, text in added.items()}
    return frozenset(candidates - _reached_candidates(candidates, edges))


def _statement_lines(stmt: ast.stmt) -> range:
    first = min([stmt.lineno, *(d.lineno for d in getattr(stmt, "decorator_list", ()))])
    return range(first, (stmt.end_lineno or stmt.lineno) + 1)


def _is_inert(stmt: ast.stmt) -> bool:
    """Imports and bare string expressions (docstrings) run nothing."""
    if isinstance(stmt, ast.Import | ast.ImportFrom):
        return True
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)


def _runtime_lines(tree: ast.Module, dead_lines: set[int]) -> set[int]:
    """Lines of statements that can change behavior when the module runs."""
    statements: list[ast.stmt] = []
    for stmt in tree.body:
        statements.extend(stmt.body if isinstance(stmt, ast.ClassDef) else [stmt])
    lines: set[int] = set()
    for stmt in statements:
        if _is_inert(stmt) or stmt.lineno in dead_lines:
            continue
        lines.update(_statement_lines(stmt))
    return lines


def _hunk_changes_runtime(
    hunk: DiffHunk, parsed: dict[str, _ParsedFile], runtime: dict[str, set[int]],
    inert_files: frozenset[str] = frozenset(),
) -> bool:
    """Whether this production hunk can change runtime behavior."""
    if hunk.file_path in inert_files:
        return False
    if any(raw.startswith("-") for raw in hunk.body.splitlines()[1:]):
        return True
    added = [(n, t) for n, t in added_lines(hunk) if t.strip()]
    if hunk.file_path not in parsed:
        # Not Python, or no parseable head text: fail open.
        return bool(added)
    return any(n in runtime[hunk.file_path] for n, _ in added)


def _is_runtime_criterion(text: str) -> bool:
    return not (
        _LIVE_RE.match(text)
        or _TEST_CRITERION_RE.search(text)
        or _DOC_CRITERION_RE.search(text)
        or _PROCESS_CRITERION_RE.search(text)
    )


def _is_live_criterion(text: str) -> bool:
    """A `Live:` criterion: met by a deploy, so only when something runs."""
    return bool(_LIVE_RE.match(text)) and not _TEST_CRITERION_RE.search(text)


def _is_live_state_criterion(text: str) -> bool:
    """A criterion only a change to running state can meet (see
    `_LIVE_STATE_RE`), and not one a test or doc hunk can meet."""
    return bool(_LIVE_STATE_RE.search(text)) and not (
        _TEST_CRITERION_RE.search(text) or _DOC_CRITERION_RE.search(text)
    )


def _manages_state(production: list[DiffHunk], new_files: frozenset[str]) -> bool:
    """Any production change other than a brand-new script: an edit to an
    existing file, or new config, IaC or code the deploy picks up."""
    return any(
        not (h.file_path in new_files and h.file_path.endswith(_SCRIPT_SUFFIXES))
        for h in production
    )


def _live_state_finding(
    unmet: list[LinkedCriterion], anchor: tuple[str, int],
    scripts: tuple[str, ...], pr_title: str,
) -> Finding:
    named = "; ".join(f'#{c.issue} criterion {c.number} "{c.text}"' for c in unmet)
    if scripts:
        files = ", ".join(f"`{p}`" for p in scripts)
        only = (
            f"its only non-doc change is {files}, a new script someone would "
            "still have to run by hand"
        )
    else:
        only = "it changes only docs and tests"
    claim = f'The PR says "{pr_title}", but this' if pr_title else "This"
    return Finding(
        file=anchor[0],
        line=anchor[1],
        severity="high",
        rule_name="intent-mismatch",
        message=(
            f"No hunk in this diff can satisfy {named}. Each is a check of "
            f"live host, network or deploy state. {claim} diff changes no "
            f"config or code that manages that state: {only}. Make the change "
            "where that state is managed, or move these criteria to an "
            "operator ticket."
        ),
        suggestion=None,
        effort="heavy-lift",
    )


def _dead_code_finding(root: _NewDef, stranded: list[_NewDef]) -> Finding:
    if stranded:
        names = ", ".join(f"`{d.name}`" for d in stranded)
        helpers = (
            f"does the new helper it calls ({names})" if len(stranded) == 1
            else f"do the {len(stranded)} new helpers it calls ({names})"
        )
        claim = (
            f"New function `{root.name}` has no caller outside test files, "
            f"and neither {helpers}, so nothing this PR ships runs them."
        )
    else:
        claim = (
            f"New function `{root.name}` has no caller outside test files, "
            "so nothing this PR ships runs it."
        )
    return Finding(
        file=root.file,
        line=root.line,
        severity="medium",
        rule_name="dead-code",
        message=(
            f"{claim} Call it from the code path that needs it, or say in "
            "the PR body that it is an entry point (a CLI command, a route "
            "or an exported API)."
        ),
        suggestion=None,
        effort="quick-win",
    )


def _mismatch_finding(
    unmet: list[LinkedCriterion], anchor: tuple[str, int],
    dead: list[_NewDef], pr_title: str,
    unreferenced: tuple[str, ...] = (),
) -> Finding:
    named = "; ".join(f'#{c.issue} criterion {c.number} "{c.text}"' for c in unmet)
    causes = []
    if dead:
        names = ", ".join(f"`{d.name}`" for d in dead)
        causes.append(f"only adds {names}, which nothing outside tests calls")
    if unreferenced:
        files = ", ".join(f"`{p}`" for p in unreferenced)
        causes.append(
            f"adds {files}, which no other file in this diff imports, runs or names",
        )
    cause = " and ".join(causes) if causes else "is docs, imports or comments"
    claim = f'The PR says "{pr_title}", but every' if pr_title else "Every"
    return Finding(
        file=anchor[0],
        line=anchor[1],
        severity="high",
        rule_name="intent-mismatch",
        message=(
            f"No hunk in this diff can satisfy {named}. {claim} non-test "
            f"change {cause}, so the runtime behavior these criteria "
            "describe cannot happen. Wire the change into the code path "
            "that runs, or narrow the linked criteria."
        ),
        suggestion=None,
        effort="heavy-lift",
    )


def _anchor(hunks: tuple[DiffHunk, ...], dead: list[_NewDef]) -> tuple[str, int] | None:
    """The first dead def, else the first added production line, else any."""
    if dead:
        return dead[0].file, dead[0].line
    ordered = sorted(hunks, key=lambda h: not is_production_path(h.file_path))
    for hunk in ordered:
        for lineno, _ in added_lines(hunk):
            return hunk.file_path, lineno
    return None


def _parse_production(
    production: list[DiffHunk], file_contents: dict[str, str],
) -> tuple[dict[str, _ParsedFile], str]:
    """Parsed Python production files, and the joined text of the rest."""
    added_by_path: dict[str, set[int]] = {}
    for hunk in production:
        added_by_path.setdefault(hunk.file_path, set()).update(
            n for n, _ in added_lines(hunk)
        )
    parsed: dict[str, _ParsedFile] = {}
    other_text: list[str] = []
    for path, added in added_by_path.items():
        tree = _parse(file_contents.get(path)) if path.endswith(".py") else None
        if tree is not None:
            parsed[path] = _ParsedFile(tree, frozenset(added))
        else:
            other_text.append(file_contents.get(path, ""))
    return parsed, "\n".join(other_text)


def _diff_changes_runtime(
    production: list[DiffHunk], parsed: dict[str, _ParsedFile], dead: list[_NewDef],
    inert_files: frozenset[str] = frozenset(),
) -> bool:
    runtime = {
        path: _runtime_lines(p.tree, {d.line for d in dead if d.file == path})
        for path, p in parsed.items()
    }
    return any(
        _hunk_changes_runtime(h, parsed, runtime, inert_files) for h in production
    )


def _unmet_criteria_finding(
    criteria: tuple[LinkedCriterion, ...], hunks: tuple[DiffHunk, ...],
    production: list[DiffHunk], parsed: dict[str, _ParsedFile],
    dead: list[_NewDef], inert: frozenset[str], new_files: frozenset[str],
    pr_title: str,
) -> Finding | None:
    """One high `intent-mismatch` for the criteria no hunk can meet, or None.

    First: nothing in the diff runs, so every runtime and `Live:` criterion
    is unmet. Else: every such criterion is a live-state check, and nothing
    in the diff manages that state."""
    anchor = _anchor(hunks, dead)
    state = [
        c for c in criteria
        if _is_runtime_criterion(c.text) or _is_live_criterion(c.text)
    ]
    if not (state and anchor):
        return None
    if not _diff_changes_runtime(production, parsed, dead, inert):
        stranded = tuple(sorted(p for p in inert if any(h.file_path == p for h in hunks)))
        return _mismatch_finding(state, anchor, dead, pr_title, stranded)
    if all(_is_live_state_criterion(c.text) for c in state) and not _manages_state(
        production, new_files,
    ):
        scripts = tuple(sorted({h.file_path for h in production}))
        return _live_state_finding(state, anchor, scripts, pr_title)
    return None


def scan_criteria_coverage(
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str],
    *,
    criteria: tuple[LinkedCriterion, ...],
    pr_title: str,
    pr_body: str,
    new_files: frozenset[str] = frozenset(),
) -> tuple[Finding, ...]:
    """Dead new functions, and runtime criteria no hunk can satisfy.

    `new_files`: paths the diff adds (`diff_parser.file_changes`), so a new
    script nothing names can count as inert. Empty keeps the #1179
    behavior."""
    production = [h for h in hunks if is_production_path(h.file_path)]
    parsed, other_text = _parse_production(production, file_contents)
    py_refs = {path: list(_references(p.tree)) for path, p in parsed.items()}
    dead = sorted(
        _dead_defs(parsed, py_refs, other_text, pr_body or ""),
        key=lambda d: (d.file, d.line),
    )
    findings = [
        _dead_code_finding(root, stranded)
        for root, stranded in _call_trees(dead, py_refs)
    ]
    # None (past the scan bounds) means not evaluated: nothing is inert.
    inert = unreferenced_new_files(new_files, hunks, pr_body or "") or frozenset()
    unmet = _unmet_criteria_finding(
        criteria, hunks, production, parsed, dead, inert, new_files, pr_title,
    )
    return tuple(findings + ([unmet] if unmet else []))
