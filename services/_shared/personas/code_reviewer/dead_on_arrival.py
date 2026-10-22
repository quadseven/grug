"""Deterministic checks for lane work that ships code nothing runs (#1366).

Lane pull requests shipped code that never runs and called it a fix: a
pure decision module copied into a container image and imported by
nothing, a config table a page loads but nothing reads, a standalone
script that only its own tests import, a policy class that only tests
construct. Others left scratch files behind
(`test_scratch.txt`, `.loom/test.txt`, `test_write.tmp`). The #1179
dead-code check covers new Python functions only, and counts any file
that names a new script, a test included, as a reference to it.

Four findings, each anchored on a line the diff adds:

- `dead-code` on a new file: a new non-test source file (Python, shell,
  JavaScript, TypeScript and the like) that no other non-test file in
  the diff imports, sources, runs or names, and that the PR body does
  not declare an entry point. A doc that names it counts (a documented
  tool is meant to be run by hand); a test that imports it does not. A
  line that only packages the file (a Dockerfile `COPY`, a bare path in
  a YAML list such as a configMapGenerator or a workflow `paths:`
  filter) ships it without running it, so it does not count either.
  Medium; high on a lane PR when the file is its only production
  change, because the PR then changes no behavior at all. Reuses
  `criteria_coverage.unreferenced_new_files` with a stricter reference
  filter, so the location and generic-stem exemptions and the scan
  bounds are the same.
- `dead-code` on a script a page loads but nothing reads: a new
  JavaScript file only `<script src>` tags load, whose top level (inside
  a scoping wrapper such as `(function (NS) {`) runs no call and only
  declares names, none of which any other non-test file in the diff
  mentions. Medium only: a file outside the diff could read a global.
- `dead-code` on a class nothing constructs: a plain new class (no base,
  no decorator but `dataclass`) in a new production Python file that
  every non-test line outside its own body uses only as an import or a
  type annotation. Medium. Needs the file's head text.
- `stray-file` (medium): a new scratch or temporary file (`*.tmp`,
  `*.bak`, `test*.txt`, `scratch*`, a `tmp`/`temp` path component, a
  non-config file under `.loom/`). Test and fixture directories are
  exempt: a sample file there is test data.

Not double-reported: a file an `intent-mismatch` finding already names
gets no file finding; the #1179 per-function `dead-code` findings in a
file this check reports whole are superseded by that one finding
(`drop_superseded`); and a class whose body already holds a per-function
finding gets no class finding.

Cost: linear in the diff. Past the bounds of `unreferenced_new_files`
the dead-code checks are not evaluated and only stray files are
reported.

Pure: file changes, hunks, PR body and prior findings in; findings out.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass

from personas.code_reviewer.criteria_coverage import (
    is_production_path,
    unreferenced_new_files,
)
from personas.code_reviewer.diff_parser import (
    DiffHunk,
    FileChange,
    added_lines,
    is_review_excluded_path,
)
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

DEAD_RULE = "dead-code"
STRAY_RULE = "stray-file"

# Test files beyond `is_test_file`'s Python conventions.
_TEST_NAME_RE = re.compile(
    r"(?:^|/)(?:__tests__|spec|specs)/|\.(?:test|spec)\.[cm]?[jt]sx?$"
    r"|(?:^|/)test_[^/]*\.(?:sh|bash)$|_test\.(?:sh|bash|[cm]?[jt]sx?)$"
)
_DOCKERFILE_RE = re.compile(r"(?:^|/)(?:Dockerfile|Containerfile)[^/]*$|\.dockerfile$", re.IGNORECASE)
# A Dockerfile line that only copies files in: the instruction itself, or a
# continuation line of bare paths.
_DOCKER_PACKAGING_RE = re.compile(
    r"^\s*(?:COPY|ADD)\b"
    r"|^\s*(?!(?:RUN|CMD|ENTRYPOINT|FROM|ENV|ARG|WORKDIR|USER|SHELL|HEALTHCHECK|ONBUILD)\b)"
    r"[\w./*${}-]+(?:\s+[\w./*${}-]+)*\s*\\?\s*$",
    re.IGNORECASE,
)
# A YAML list item that is a bare path: a configMapGenerator file list, a
# workflow `paths:` filter. It ships or watches a file; it does not run it.
_YAML_PACKAGING_RE = re.compile(r"""^\s*-\s*["']?[\w./*${}-]+["']?\s*(?:#.*)?$""")
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*\bsrc\s*=", re.IGNORECASE)
_MARKUP_SUFFIXES = (".html", ".htm", ".vue", ".svelte", ".jsx", ".tsx", ".php", ".erb", ".j2")
_JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx")
_JS_DECL_RE = re.compile(
    r"(?:export\s+)?(?:default\s+)?(?:const|let|var|class|(?:async\s+)?function\*?)"
    r"\s+([A-Za-z_$][\w$]*)"
    # `window.name = ...`, or `NS.name = ...` on a namespace a wrapper got.
    r"|[A-Za-z_$][\w$]*\.([A-Za-z_$][\w$]*)\s*=(?!=)"
)
# A wrapper that runs once to scope the file: `(function (NS) {`,
# `(() => {`. Its body counts as the file's top level.
_JS_WRAPPER_RE = re.compile(r"^[;!]?\(\s*(?:async\s+)?(?:function\b[^{]*|\([^)]*\)\s*=>\s*)\{\s*$")
# A top-level statement that runs something when the script loads.
_JS_TOP_CALL_RE = re.compile(r"^(?:[A-Za-z_$][\w$.]*\s*\(|[;!]?\()")
_JS_KEYWORDS = frozenset({"if", "for", "while", "switch", "return", "typeof", "catch"})
_JS_WORD_RE = re.compile(r"[A-Za-z_$][\w$]*")
# Fewest characters a declared name needs to prove a read by name alone.
_MIN_NAME = 4
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_DATACLASS_RE = re.compile(r"(?:dataclasses\.)?dataclass(?:\(.*\))?", re.DOTALL)
_IMPORT_LINE_RE = re.compile(r"^\s*(?:from\s+\S+\s+)?import\b")

_STRAY_NAME_RE = re.compile(
    r"\.(?:tmp|temp|bak|orig|rej|swp|swo)$|~$"
    r"|^(?:test|tmp|temp)(?:[_-][^/]*)?\.(?:txt|log|out|tmp)$"
    r"|^scratch",
    re.IGNORECASE,
)
_STRAY_DIR_RE = re.compile(r"^(?:tmp|temp)(?:[_-].*)?$|^scratch", re.IGNORECASE)
_LOOM_CONFIG_RE = re.compile(r"\.(?:conf|toml|ya?ml|json|ini|cfg)$", re.IGNORECASE)
_DATA_DIRS = frozenset({
    "tests", "test", "testing", "testdata", "fixtures", "__fixtures__",
    "__tests__", "spec", "specs", "examples", "samples",
})


def _is_test_like(path: str) -> bool:
    return is_test_file(path) or bool(_TEST_NAME_RE.search(path))


def _is_packaging_line(path: str, text: str) -> bool:
    """The line ships, lists or watches a file without running it."""
    if not text.strip():
        return True
    if _DOCKERFILE_RE.search(path):
        return bool(_DOCKER_PACKAGING_RE.match(text))
    if path.endswith((".yml", ".yaml")):
        return bool(_YAML_PACKAGING_RE.match(text))
    return False


def _is_script_tag(path: str, text: str) -> bool:
    return path.lower().endswith(_MARKUP_SUFFIXES) and bool(_SCRIPT_TAG_RE.search(text))


def _strict_reference(path: str, text: str) -> bool:
    """A line that can load or name a file for a reader to run: in a
    non-test file, and not one that only packages it or loads it from a
    page."""
    return not (
        _is_test_like(path)
        or _is_packaging_line(path, text)
        or _is_script_tag(path, text)
    )


def _loading_reference(path: str, text: str) -> bool:
    """As `_strict_reference`, but a `<script src>` tag loads the file."""
    return _is_script_tag(path, text) or _strict_reference(path, text)


def _by_file(hunks: Iterable[DiffHunk]) -> dict[str, list[DiffHunk]]:
    out: dict[str, list[DiffHunk]] = {}
    for hunk in hunks:
        out.setdefault(hunk.file_path, []).append(hunk)
    return out


def _packaging_only(path: str, own: list[DiffHunk]) -> bool:
    """Every line the diff changes in this file only ships or lists files."""
    lines = [
        raw[1:] for hunk in own for raw in hunk.body.splitlines()[1:]
        if raw[:1] in "+-"
    ]
    return bool(lines) and all(_is_packaging_line(path, t) for t in lines)


def _anchor(path: str, own: list[DiffHunk]) -> tuple[str, int] | None:
    for hunk in own:
        if hunk.new_lines:
            return path, min(hunk.new_lines)
    return None


def _js_top_level(lines: list[str]) -> list[str] | None:
    """The file's top-level code lines, with a scoping wrapper unwrapped;
    None when one of them runs a call at load time."""
    code = [t for t in lines if t.strip() and not t.lstrip().startswith(("//", "/*", "*"))]
    indent = 0
    if code and _JS_WRAPPER_RE.match(code[0]):
        body = code[1:]
        indent = len(body[0]) - len(body[0].lstrip()) if body else 0
        code = body
    top: list[str] = []
    for text in code:
        if len(text) - len(text.lstrip()) != indent or text.lstrip()[:1] in "})]":
            continue
        line = text.strip()
        call = _JS_TOP_CALL_RE.match(line)
        if call and line.split("(", 1)[0].strip() not in _JS_KEYWORDS:
            return None
        top.append(line)
    return top


def _js_declared(own: list[DiffHunk]) -> set[str]:
    """Names a load-time-inert script declares at its top level; empty
    when it runs a call at load time or declares nothing nameable."""
    top = _js_top_level([t for h in own for _, t in added_lines(h)])
    if top is None:
        return set()
    names = set()
    for line in top:
        decl = _JS_DECL_RE.match(line)
        if decl:
            names.add(decl.group(1) or decl.group(2))
    return {n for n in names if len(n) >= _MIN_NAME}


def _unread_scripts(
    candidates: set[str], by_file: dict[str, list[DiffHunk]],
) -> set[str]:
    """Candidate scripts none of whose declared names any other non-test
    file's added lines mention: one index, one word pass over the diff."""
    owners: dict[str, set[str]] = {}
    for path in candidates:
        for name in _js_declared(by_file.get(path, [])):
            owners.setdefault(name, set()).add(path)
    unread = {p for names in owners.values() for p in names}
    for path, own in by_file.items():
        if _is_test_like(path):
            continue
        for hunk in own:
            for _, text in added_lines(hunk):
                for word in _JS_WORD_RE.findall(text):
                    unread -= owners.get(word, set()) - {path}
    return unread


@dataclass(frozen=True, slots=True)
class _NewClass:
    file: str
    line: int
    end: int
    name: str


def _is_plain_class(node: ast.ClassDef) -> bool:
    """No base but `object` and no decorator but `dataclass`: nothing a
    base class, metaclass or registry does can construct it unseen."""
    bases_ok = all(isinstance(b, ast.Name) and b.id == "object" for b in node.bases)
    decorators_ok = all(
        _DATACLASS_RE.fullmatch(ast.unparse(d)) for d in node.decorator_list
    )
    return bases_ok and not node.keywords and decorators_ok


def _new_classes(
    paths: Iterable[str], file_contents: dict[str, str],
) -> list[_NewClass]:
    """Plain module-level classes in new production Python files, read
    from their head text; a file with no head text or that does not parse
    is skipped."""
    found: list[_NewClass] = []
    for path in paths:
        try:
            tree = ast.parse(file_contents.get(path) or "")
        except (SyntaxError, ValueError):
            continue
        found.extend(
            _NewClass(path, node.lineno, node.end_lineno or node.lineno, node.name)
            for node in tree.body
            if isinstance(node, ast.ClassDef) and len(node.name) >= _MIN_NAME
            and _is_plain_class(node)
        )
    return found


def _only_typed(text: str, name: str) -> bool:
    """Every use of `name` on this line is an import or a type annotation,
    never a call, a subclass, an attribute read or a value passed on."""
    code = text.split("#", 1)[0]
    if _IMPORT_LINE_RE.match(code):
        return True
    for match in re.finditer(rf"(?<![\w.]){re.escape(name)}(?!\w)", code):
        before = code[: match.start()].rstrip()
        after = code[match.end():].lstrip()
        if not (before.endswith((":", "->", "|", "[")) or after.startswith(("|", "]"))):
            return False
    return True


def _unconstructed_classes(
    classes: list[_NewClass], by_file: dict[str, list[DiffHunk]],
    file_contents: dict[str, str],
) -> list[_NewClass]:
    """New classes no non-test, non-doc line outside their own body uses
    as anything but an import or a type: one index, one word pass over the
    diff's added lines, plus the full head text of each class's own file
    (its diff may be cut short)."""
    by_name: dict[str, list[_NewClass]] = {}
    for cls in classes:
        by_name.setdefault(cls.name, []).append(cls)
    homes = {c.file for c in classes}
    sources: list[tuple[str, Iterable[tuple[int, str]]]] = [
        (path, enumerate(file_contents.get(path, "").splitlines(), start=1))
        for path in sorted(homes)
    ]
    sources.extend(
        (path, (pair for h in own for pair in added_lines(h)))
        for path, own in by_file.items() if path not in homes
    )
    live: set[_NewClass] = set()
    for path, lines in sources:
        if _is_test_like(path) or path.lower().endswith(_DOC_SUFFIXES):
            continue
        for lineno, text in lines:
            for word in set(_JS_WORD_RE.findall(text)) & by_name.keys():
                if _only_typed(text, word):
                    continue
                live.update(
                    c for c in by_name[word]
                    if not (c.file == path and c.line <= lineno <= c.end)
                )
    return [c for c in classes if c not in live]


def _unconstructed_finding(cls: _NewClass) -> Finding:
    return Finding(
        file=cls.file,
        line=cls.line,
        severity="medium",
        rule_name=DEAD_RULE,
        message=(
            f"New class `{cls.name}` is never constructed outside tests: the "
            "rest of this diff only imports it or names it in type "
            "annotations, so none of its behavior runs. Construct it where "
            "the behavior is needed, or say in the PR body what does."
        ),
        suggestion=None,
        effort="quick-win",
    )


def _already_named(path: str, prior: Iterable[Finding]) -> bool:
    """An `intent-mismatch` finding already names this file as inert."""
    return any(
        f.rule_name == "intent-mismatch" and f"`{path}`" in f.message for f in prior
    )


def _dead_file_finding(anchor: tuple[str, int], *, only: bool) -> Finding:
    tail = (
        " It is the only production change, so this PR changes no behavior."
        if only else ""
    )
    return Finding(
        file=anchor[0],
        line=anchor[1],
        severity="high" if only else "medium",
        rule_name=DEAD_RULE,
        message=(
            f"New file `{anchor[0]}` is not imported, sourced, run or named by "
            "any other non-test file in this diff (a test, a doc or a line "
            "that only copies it into an image does not load it), and the PR "
            f"body declares no entry point, so nothing this PR ships runs it.{tail} "
            "Wire it into the code path that needs it, or say in the PR body "
            "how it runs (a CLI command, a route, a scheduled job)."
        ),
        suggestion=None,
        effort="quick-win",
    )


def _unread_script_finding(anchor: tuple[str, int]) -> Finding:
    return Finding(
        file=anchor[0],
        line=anchor[1],
        severity="medium",
        rule_name=DEAD_RULE,
        message=(
            f"New script `{anchor[0]}` is loaded by a `<script>` tag but only "
            "declares names, and no other file in this diff reads any of them, "
            "so loading it changes nothing. Read its values where the behavior "
            "lives, or drop it."
        ),
        suggestion=None,
        effort="quick-win",
    )


def _stray_finding(anchor: tuple[str, int]) -> Finding:
    return Finding(
        file=anchor[0],
        line=anchor[1],
        severity="medium",
        rule_name=STRAY_RULE,
        message=(
            f"`{anchor[0]}` looks like a scratch or temporary file left in the "
            "commit, and nothing in this diff uses it. Delete it, or say in the "
            "PR body why it belongs in the repository."
        ),
        suggestion=None,
        effort="quick-win",
    )


def is_stray_path(path: str) -> bool:
    """A scratch or temporary file outside test and fixture directories."""
    parts = path.split("/")
    if any(p.lower() in _DATA_DIRS for p in parts[:-1]):
        return False
    if parts[0] == ".loom" and len(parts) > 1:
        return not _LOOM_CONFIG_RE.search(parts[-1])
    return bool(_STRAY_NAME_RE.search(parts[-1])) or any(
        _STRAY_DIR_RE.match(p) for p in parts[:-1]
    )


def scan_dead_on_arrival(
    changes: Iterable[FileChange],
    hunks: tuple[DiffHunk, ...],
    *,
    pr_body: str,
    prior: tuple[Finding, ...] = (),
    file_contents: dict[str, str] | None = None,
    lane: bool = True,
) -> tuple[Finding, ...]:
    """Dead new files, unconstructed new classes and stray scratch files
    (see the module docstring).

    `file_contents`: head text by path; a new Python file missing from it
    gets no class check. `lane`: the PR comes from a lane, which claims to
    fix its ticket; a dead file is high only there, since a person adding
    a tool to run by hand is the common case elsewhere.

    `prior`: the findings other lane-work checks already made, so a file
    an `intent-mismatch` finding names is not reported twice. Past the
    scan bounds of `unreferenced_new_files` the dead-file checks are not
    evaluated and only stray files are reported."""
    changes = tuple(changes)
    by_file = _by_file(hunks)
    new_files = frozenset(c.path for c in changes if c.status == "added")
    findings: list[Finding] = []
    stray = {p for p in new_files if is_stray_path(p)}
    for path in sorted(stray):
        anchor = _anchor(path, by_file.get(path, []))
        if anchor is not None:
            findings.append(_stray_finding(anchor))
    strict = unreferenced_new_files(
        new_files, hunks, pr_body or "", counts=_strict_reference,
    )
    if strict is None:
        return tuple(findings)
    loaded = unreferenced_new_files(
        new_files, hunks, pr_body or "", counts=_loading_reference,
    ) or frozenset()
    dead = {
        p for p in strict & loaded
        if not is_review_excluded_path(p) and not _is_test_like(p)
    }
    unread = _unread_scripts(
        {p for p in strict - loaded if p.endswith(_JS_SUFFIXES)}, by_file,
    )
    others = {
        c.path for c in changes
        if is_production_path(c.path) and not is_review_excluded_path(c.path)
        and c.path not in dead and c.path not in stray
        and not (c.status != "removed" and _packaging_only(c.path, by_file.get(c.path, [])))
    }
    for path in sorted(dead):
        anchor = _anchor(path, by_file.get(path, []))
        if anchor is not None and not _already_named(path, prior):
            findings.append(_dead_file_finding(anchor, only=lane and not others))
    for path in sorted(unread):
        anchor = _anchor(path, by_file.get(path, []))
        if anchor is not None and not _already_named(path, prior):
            findings.append(_unread_script_finding(anchor))
    live_new = sorted(
        p for p in new_files - dead
        if p.endswith(".py") and is_production_path(p) and not _is_test_like(p)
        and not is_review_excluded_path(p)
    )
    marked = {(f.file, f.line) for f in prior if f.rule_name == DEAD_RULE}
    contents = file_contents or {}
    classes = _new_classes(live_new, contents)
    for cls in _unconstructed_classes(classes, by_file, contents):
        if not any(f == cls.file and cls.line <= n <= cls.end for f, n in marked):
            findings.append(_unconstructed_finding(cls))
    return tuple(findings)


def drop_superseded(
    prior: Iterable[Finding], found: Iterable[Finding],
) -> tuple[Finding, ...]:
    """`prior` without the per-function `dead-code` findings in a file one
    of `found` reports whole: one marking per wiring gap."""
    whole = {
        f.file for f in found
        if f.rule_name == DEAD_RULE and f"`{f.file}`" in f.message
    }
    return tuple(
        f for f in prior if not (f.rule_name == DEAD_RULE and f.file in whole)
    )
