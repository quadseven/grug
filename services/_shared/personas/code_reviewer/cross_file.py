"""Cross-file review context - 1-hop symbol resolution (#468, epic #464
slice 4 = PRD #346 P1.1, the repo-graph review capability tracer).

The Elder sees changed files (whole-file since #336) but nothing else in
the repo. The highest-value review questions are cross-file: "does the
CALLER of this changed function handle the new signature?". This module
resolves the diff's interesting symbols to the UNCHANGED files that
define or call them, so those files ride into the review prompt as extra
context (rendered by `review_evidence`'s reviewer and judge views, flagged never - the
paired `caller-not-updated` rule anchors findings on the DIFF line).

PREMISE NOTE (recorded in DESIGN.md): GitHub's `/search/code` indexes the
DEFAULT BRANCH only, so search is used solely for PATH discovery; each
discovered file's CONTENT is then fetched at the PR's head SHA via the
contents API (SHA-accurate). A caller that exists only on the PR branch
is invisible to search - accepted for the tracer (callers needing update
live on the default branch).

TRACER SCOPE: symbol extraction uses regex for Python, JavaScript,
TypeScript, Go, and C/C++. The lazy tree-sitter repo index is deferred.

FAIL-SAFE contract: every error path degrades to `{}` - today's
diff-only review - and logs `cross_file_context_degraded`. No cache:
The durable snapshot claim already guarantees one completed review per exact
base/head/intent input, so a per-head cache would rarely hit and could return
context for the wrong base or intent.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from urllib.parse import quote

import httpx

from personas.code_reviewer.diff_parser import DiffHunk

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.cross_file")

# Hard budgets. GitHub code-search is ~30 requests/min per token - one
# search per symbol means _MAX_SYMBOLS bounds the rate-limit exposure per
# review; _MAX_FILES/_MAX_FILE_BYTES bound the prompt-size cost.
_MAX_SYMBOLS = 5
_MAX_FILES = 5
_MAX_FILE_BYTES = 60_000
_SEARCH_TIMEOUT = 10  # seconds, per call
# GLOBAL wall-clock budget for the WHOLE cross-file phase (codex
# peer-review HIGH, PR #480): per-call timeouts alone allow up to
# (_MAX_SYMBOLS + _MAX_FILES) x _SEARCH_TIMEOUT ~= 100s of slow-but-not-
# failing responses before the review even starts. The #468 acceptance
# bound is <10s p95 added latency; stop BOTH search and content fetching
# the moment the deadline passes and degrade to whatever was collected.
_TOTAL_BUDGET_SECONDS = 8.0

# Added-line function definition: `+def foo(` / `+    def foo(`.
_PY_DEF = re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)
# ENCLOSING function of a body-only change (codex round 3, PR #480): a
# function that gains a new raise/return inside its body without touching
# its `def` line still changes its CONTRACT - its callers matter. The
# enclosing def shows up as a CONTEXT line (` def foo(`) near the hunk
# start and/or in the hunk section header (`@@ -a,b +c,d @@ def foo(...)`).
_PY_CONTEXT_DEF = re.compile(r"^[ -]\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_PY_SECTION_DEF = re.compile(r"^@@[^@]*@@\s.*?\b(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)
# A call on an added line: `name(` not preceded by `.` (method calls on
# unknown receivers are noise for a 1-hop tracer) or `def `.
_PY_CALL = re.compile(r"^\+.*?(?<![\w.])([A-Za-z_]\w*)\s*\(", re.MULTILINE)

# Python builtins + stdlib-common noise a 1-hop lookup would waste budget
# on. Not exhaustive - just the high-frequency call targets.
_STOP_NAMES = frozenset({
    "print", "len", "str", "int", "float", "bool", "list", "dict", "set",
    "tuple", "frozenset", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "sum", "min", "max", "abs", "round", "open",
    "isinstance", "issubclass", "getattr", "setattr", "hasattr", "type",
    "super", "repr", "hash", "id", "iter", "next", "vars", "format",
    "def", "if", "elif", "while", "for", "return", "yield", "raise",
    "assert", "with", "lambda", "not", "and", "or", "in", "is",
    "async", "await",
})


@dataclass(frozen=True, slots=True)
class _BraceLanguage:
    definition: re.Pattern[str]
    call: re.Pattern[str]
    stop_names: frozenset[str]


_NAME = r"[A-Za-z_]\w*"
_IDENTIFIER_CALL = re.compile(rf"\b({_NAME})\s*\(")
_JS_NAME = r"[A-Za-z_$][\w$]*"
_JS_TYPE_PARAMS = r"(?:<[^>\n]+>)?"
_JS = _BraceLanguage(
    re.compile(
        rf"\s*(?:(?:(?:export|default|async)\s+)*function(?:\s+|\s*\*\s*)"
        rf"({_JS_NAME}){_JS_TYPE_PARAMS}\s*\("
        rf"|(?:export\s+)?const\s+({_JS_NAME})(?:\s*:[^=]+)?\s*=\s*"
        rf"(?:async\s+)?{_JS_TYPE_PARAMS}\s*(?:\([^;]*\)|{_JS_NAME})\s*(?::[^=]+)?=>"
        rf"|(?:(?:public|private|protected|static|async|override|get|set)\s+)*"
        rf"({_JS_NAME}){_JS_TYPE_PARAMS}\s*\([^;]*\)\s*(?::[^;{{]+)?\s*\{{)"
    ),
    re.compile(rf"(?<![\w$])({_JS_NAME})\s*\("),
    frozenset({
        "if", "for", "while", "switch", "catch", "with", "return", "throw", "typeof",
        "void", "delete", "new", "super", "import", "async", "await", "function", "require",
        "eval", "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURI", "decodeURI",
        "encodeURIComponent", "decodeURIComponent", "setTimeout", "setInterval",
        "clearTimeout", "clearInterval", "Array", "BigInt", "Boolean", "Date", "Error",
        "Map", "Number", "Object", "Promise", "RegExp", "Set", "String", "Symbol", "WeakMap",
        "WeakSet", "log", "stringify",
    }),
)
_GO = _BraceLanguage(
    re.compile(rf"\s*func\s+(?:\([^)]*\)\s*)?({_NAME})(?:\[[^\]]+\])?\s*\("),
    _IDENTIFIER_CALL,
    frozenset({
        "if", "for", "switch", "select", "return", "defer", "go", "func", "append", "cap",
        "clear", "close", "complex", "copy", "delete", "imag", "len", "make", "max", "min",
        "new", "panic", "print", "println", "real", "recover", "bool", "byte", "complex64",
        "complex128", "float32", "float64", "int", "int8", "int16", "int32", "int64", "rune",
        "string", "uint", "uint8", "uint16", "uint32", "uint64", "uintptr", "Print", "Printf",
        "Println",
    }),
)
_CXX_TYPE = rf"{_NAME}(?:::{_NAME})*(?:<[^<>;{{}}]*>)?"
_CXX = _BraceLanguage(
    re.compile(
        rf"\s*(?:{_CXX_TYPE}\s+)*{_CXX_TYPE}(?:\s*[*&]+\s*|\s+)"
        rf"(?:{_NAME}::)*({_NAME})\s*\([^;{{}}]*\)\s*"
        r"(?:(?:const|volatile|noexcept|override|final)\b\s*|&\s*)*"
        # The opening brace can be on the next line or outside the hunk.
        r"(?:->\s*[^;{}]+)?(?:\{|$)"
    ),
    _IDENTIFIER_CALL,
    frozenset({
        "if", "for", "while", "switch", "catch", "return", "throw", "new", "delete", "sizeof",
        "alignof", "_Alignof", "typeid", "decltype", "defined", "static_assert", "_Static_assert",
        "constexpr", "noexcept", "static_cast", "dynamic_cast", "const_cast", "reinterpret_cast",
        "assert", "printf", "fprintf", "snprintf", "sprintf", "scanf", "puts", "putchar", "malloc",
        "calloc", "realloc", "free", "memcpy", "memmove", "memset", "strlen", "strcmp", "exit",
        "abort", "bool", "char", "double", "float", "int", "long", "short", "unsigned", "void",
        "size_t", "move", "forward",
    }),
)
_BRACE_LANGUAGES = {
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"), _JS),
    ".go": _GO,
    **dict.fromkeys((".c", ".cc", ".cpp", ".h", ".hpp"), _CXX),
}


# A quoted string literal on a changed line. No quote characters inside, so
# the value can ride into a code-search phrase query unescaped. Only
# natural-language text qualifies (a word character AND whitespace, 4+ chars):
# short literals ("a", "--", "ok") match everywhere, and a whitespace-free
# literal is as likely a removed token or key as a message - it must never
# be sent to code search or logged as a symbol.
_STRING_LITERAL = re.compile(r"""(?:"([^"\\\n]{4,})"|'([^'"\\\n]{4,})')""")
_MAX_LITERALS = 2

_PY_LINE_DEF = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")

# Snippet bounds (codex round 7, PR #480): ship only windows AROUND the
# symbol matches, never the whole unchanged file - less data leaves the
# boundary and the prompt stays cheap. Line numbers are ORIGINAL (the
# caller-not-updated rule cites caller path:line).
_SNIPPET_RADIUS = 10
_MAX_SNIPPET_WINDOWS = 3
_MAX_SNIPPET_LINES = 80

# Cross-file context character budget (T1756): total characters across all
# cross-file snippets. Replaces the fixed _MAX_FILES count with a budget
# that fits the prompt window. A file that does not fit is dropped whole.
_MAX_CROSS_FILE_TOTAL_CHARS = 40_000

class FileTag(Enum):
    """Priority order for cross-file context: callers matter most (they
    may break), then tests (they pin contracts), then definitions (for
    understanding the callee)."""
    CALLER = 0
    TEST = 1
    DEFINITION = 2


@dataclass(frozen=True, slots=True)
class CrossFileCandidate:
    """A candidate cross-file context file with its classification and
    matched symbols. Used for pure ranking before budget trimming."""
    path: str
    content: str
    matched_symbols: tuple[str, ...]
    tag: FileTag


def is_test_file(path: str) -> bool:
    """Return True if `path` looks like a test file: under a tests/
    directory, or named test_*.py or *_test.py. Pure."""
    name = path.split("/")[-1]
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    parts = path.split("/")
    return any(p == "tests" for p in parts)


def _defines_symbol(content: str, symbol: str) -> bool:
    """Check if `content` defines `symbol` as a function or class. Pure."""
    # Content may have line numbers prefixed (e.g., "1: def foo():")
    # Match optional line number prefix: ^\d+:\s*
    line_prefix = r"(?:\d+:\s*)?"
    # Function definition: def symbol( or async def symbol(
    if re.search(
        rf"^{line_prefix}\s*(?:async\s+)?def\s+{re.escape(symbol)}\s*\(",
        content,
        re.MULTILINE,
    ):
        return True
    # Class definition: class symbol( or class symbol:
    if re.search(
        rf"^{line_prefix}\s*class\s+{re.escape(symbol)}\s*[\(:]",
        content,
        re.MULTILINE,
    ):
        return True
    return False


def tag_candidate(candidate: CrossFileCandidate) -> CrossFileCandidate:
    """Classify a candidate as CALLER, TEST, or DEFINITION based on path
    and content. Pure; returns a new candidate with the tag set."""
    # Test files always tag as TEST (highest priority after caller)
    if is_test_file(candidate.path):
        return replace(candidate, tag=FileTag.TEST)

    # If the content defines any of the matched symbols, it's a DEFINITION
    for sym in candidate.matched_symbols:
        if _defines_symbol(candidate.content, sym):
            return replace(candidate, tag=FileTag.DEFINITION)

    # Otherwise it's a CALLER (references the symbol without defining it)
    return replace(candidate, tag=FileTag.CALLER)


def rank_cross_file_candidates(
    candidates: list[CrossFileCandidate],
    char_budget: int = _MAX_CROSS_FILE_TOTAL_CHARS,
) -> list[CrossFileCandidate]:
    """Rank candidates by tag priority (CALLER > TEST > DEFINITION), then
    by number of matched symbols descending, then trim to `char_budget`
    by dropping the lowest-ranked whole file first. Pure; no I/O."""
    if not candidates:
        return []

    # Tag each candidate first
    tagged = [tag_candidate(c) for c in candidates]

    # Sort by: tag priority (lower enum value = higher priority), then
    # by matched symbol count descending, then by path for determinism.
    tagged.sort(
        key=lambda c: (c.tag.value, -len(c.matched_symbols), c.path)
    )

    # Trim to character budget by keeping highest-ranked files that fit.
    # A file that doesn't fit is dropped entirely (never cut mid-snippet).
    kept: list[CrossFileCandidate] = []
    total = 0
    for c in tagged:
        c_len = len(c.content)
        if total + c_len <= char_budget:
            kept.append(c)
            total += c_len
        else:
            # Drop this file and all lower-ranked ones (they're larger or equal)
            break
    return kept


def _symbol_snippet(content: str, symbols: tuple[str, ...]) -> str | None:
    """Bounded, original-line-numbered windows of `content` around lines
    mentioning any of `symbols`. None when no symbol appears (a search
    false-hit - drop the file rather than ship unrelated code). Pure."""
    lines = content.splitlines()
    hits = [i for i, ln in enumerate(lines) if any(s in ln for s in symbols)]
    if not hits:
        return None
    windows: list[tuple[int, int]] = []
    for i in hits:
        lo = max(0, i - _SNIPPET_RADIUS)
        hi = min(len(lines), i + _SNIPPET_RADIUS + 1)
        if windows and lo <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(hi, windows[-1][1]))
        elif len(windows) < _MAX_SNIPPET_WINDOWS:
            windows.append((lo, hi))
        else:
            break
    out: list[str] = []
    total = 0
    for lo, hi in windows:
        if out:
            out.append("...")
        for j in range(lo, hi):
            if total >= _MAX_SNIPPET_LINES:
                break
            out.append(f"{j + 1}: {lines[j]}")
            total += 1
    return "\n".join(out)


def _definition_name(
    pattern: re.Pattern[str], line: str, stop_names: frozenset[str],
) -> str | None:
    match = pattern.match(line)
    # The JavaScript alternatives each capture the name in a separate group.
    if match:
        return next((name for name in match.groups() if name and name not in stop_names), None)
    return None


def _brace_hunk_symbols(
    body: str, language: _BraceLanguage,
) -> tuple[list[str], list[str], list[str]]:
    added: list[str] = []
    enclosing: list[str] = []
    calls: list[str] = []
    header, *lines = body.splitlines()
    for line in lines:
        if not line or line[0] not in "+ -" or line[1:].lstrip().startswith("//"):
            continue
        name = _definition_name(language.definition, line[1:], language.stop_names)
        if name:
            (added if line[0] == "+" else enclosing).append(name)
        if line[0] == "+":
            calls.extend(language.call.findall(line[1:]))
    section = header.split("@@", 2)[-1]
    name = _definition_name(language.definition, section, language.stop_names)
    if name:
        enclosing.append(name)
    return added, enclosing, calls


def _enclosing_def_from_content(
    content: str, first_added_line: int, pattern: re.Pattern[str], stop_names: frozenset[str],
) -> str | None:
    """Nearest `def` at or above `first_added_line` (1-based) in the full
    file content - the enclosing function of a body-only change whose def
    line sits OUTSIDE the diff context window (codex round 4, PR #480).
    Pure; None when no def precedes the line (module-level change)."""
    lines = content.splitlines()
    for i in range(min(first_added_line, len(lines)) - 1, -1, -1):
        name = _definition_name(pattern, lines[i], stop_names)
        if name:
            return name
    return None


def _is_prose(lit: str) -> bool:
    return bool(re.search(r"\w", lit) and re.search(r"\s", lit))


def _removed_literals(hunks: Iterable[DiffHunk]) -> list[str]:
    """String literals a hunk REMOVES and does not re-add (#847). A changed
    user-facing value has no def or call symbol, so these are the only handle
    for finding an UNCHANGED test that still asserts the old value (the
    `stale-test-assertion` rule). Pure; first-seen order, capped."""
    out: list[str] = []
    for h in hunks:
        lines = h.body.splitlines()
        added = set(_line_literals(ln for ln in lines if ln.startswith("+")))
        for lit in _line_literals(ln for ln in lines if ln.startswith("-")):
            if _is_prose(lit) and lit not in added and lit not in out:
                out.append(lit)
    return out[:_MAX_LITERALS]


def _line_literals(diff_lines: Iterable[str]) -> list[str]:
    """Every whole string literal on `diff_lines` (the +/- marker dropped).
    Whole literals, not substrings: a removed "Welcome back" that only
    survives inside an added "Welcome back, friend" is still gone."""
    return [
        m.group(1) or m.group(2)
        for ln in diff_lines
        for m in _STRING_LITERAL.finditer(ln[1:])
    ]


def extract_symbols(
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Pull cross-file-interesting symbols from supported source files:

    1. Function defs the diff adds/changes on a `+` line (their CALLERS
       elsewhere may break on the new signature) - listed first, they
       are the highest-value lookups.
    2. ENCLOSING functions of body-only changes: a new raise or changed
       return inside an untouched `def` line still changes the contract
       its callers rely on. Found via context-line / hunk section-header
       defs (codex round 3) and - when `file_contents` (the #336
       full-file fetch) is available - by walking UP from the first
       added line to the nearest preceding def, covering defs entirely
       outside the diff context window (codex round 4).
    3. String literals the diff removes (#847): an untouched test may
       still assert the old value.
    4. Called names the diff adds that are NOT defined in-diff (their
       DEFINITION elsewhere tells the reviewer what the call does).

    Pure; capped at `_MAX_SYMBOLS`; dedup preserves first-seen order.
    """
    contents = file_contents or {}
    defs: list[str] = []
    calls: list[str] = []
    defined_here: set[str] = set()
    supported_hunks: list[DiffHunk] = []
    for h in hunks:
        extension = os.path.splitext(h.file_path)[1].lower()
        language = _BRACE_LANGUAGES.get(extension)
        if language:
            added, enclosing, called = _brace_hunk_symbols(h.body, language)
            line_def = language.definition
            stop_names = language.stop_names
        elif extension in {".py", ".pyi", ".pyw"}:
            added = _PY_DEF.findall(h.body)
            enclosing = _PY_CONTEXT_DEF.findall(h.body) + _PY_SECTION_DEF.findall(h.body)
            called = _PY_CALL.findall(h.body)
            line_def = _PY_LINE_DEF
            stop_names = _STOP_NAMES
        else:
            continue
        supported_hunks.append(h)
        content = contents.get(h.file_path)
        if content and h.new_lines:
            name = _enclosing_def_from_content(
                content, min(h.new_lines), line_def, stop_names if language else frozenset(),
            )
            if name:
                enclosing.append(name)
        for name in added + enclosing:
            defined_here.add(name)
            if name not in defs:
                defs.append(name)
        for name in called:
            if name not in stop_names and name not in calls:
                calls.append(name)

    literals = [lit for lit in _removed_literals(supported_hunks) if lit not in defs]
    out: list[str] = list(defs) + literals
    for name in calls:
        # A name defined in-diff needs no definition lookup; as a DEF it
        # is already queued above (callers elsewhere still matter).
        if name not in defined_here and name not in out:
            out.append(name)
    if len(out) > _MAX_SYMBOLS:
        # The removed literals keep their slots (#847): a diff that adds
        # many defs would otherwise push out the only symbol that finds an
        # untouched test still asserting the old value. Order is kept.
        room = _MAX_SYMBOLS - len(literals)
        kept = set([name for name in out if name not in literals][:room])
        out = [name for name in out if name in literals or name in kept]
    return tuple(out)


def fetch_cross_file_context(
    install_token: str,
    owner: str,
    repo: str,
    symbols: tuple[str, ...],
    *,
    head_sha: str,
    exclude_paths: frozenset[str],
) -> dict[str, str]:
    """Resolve `symbols` to UNCHANGED files via code-search (path discovery
    on the default-branch index) and fetch each file's content AT
    `head_sha` (contents API - SHA-accurate). Returns {path: content};
    `exclude_paths` (the diff's own files, already in #336 context) are
    skipped. Results are ranked (CALLER > TEST > DEFINITION, then by
    matched symbol count) and trimmed to a character budget. FAIL-SAFE:
    any error degrades to partial-or-empty, logged.
    """
    if not symbols:
        return {}

    # Elapsed-time deadline (a comparison of monotonic DELTAS, not a
    # zero-sentinel): the whole phase - searches AND content fetches -
    # must finish inside _TOTAL_BUDGET_SECONDS.
    deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS

    # Collect candidate paths from search (per symbol, up to _MAX_FILES each).
    # We gather more than the old _MAX_FILES limit because ranking will
    # trim to the character budget.
    candidate_paths: list[str] = []
    for sym in symbols[:_MAX_SYMBOLS]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.info(
                "cross_file_context_degraded",
                extra={"stage": "budget", "phase": "search"},
            )
            break
        try:
            resp = httpx.get(
                "https://api.github.com/search/code",
                params={
                    "q": f'"{sym}" repo:{owner}/{repo}',
                    "per_page": _MAX_FILES,
                },
                headers={
                    "Authorization": f"Bearer {install_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=min(_SEARCH_TIMEOUT, remaining),
            )
            resp.raise_for_status()
            items = (resp.json() or {}).get("items", [])
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            log.info(
                "cross_file_context_degraded",
                extra={"stage": "search", "symbol": sym, "error": str(e)},
            )
            break
        for item in items:
            p = item.get("path")
            if (
                p
                and p.endswith(".py")
                and p not in exclude_paths
                and p not in candidate_paths
            ):
                candidate_paths.append(p)
        # Continue searching for more symbols even if we have many paths;
        # ranking will select the best ones.

    # Fetch content for candidate paths and build CrossFileCandidate objects.
    candidates: list[CrossFileCandidate] = []
    for path in candidate_paths:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.info(
                "cross_file_context_degraded",
                extra={"stage": "budget", "phase": "content", "collected": len(candidates)},
            )
            break
        try:
            resp = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                params={"ref": head_sha},
                headers={
                    "Authorization": f"Bearer {install_token}",
                    "Accept": "application/vnd.github.raw",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=min(_SEARCH_TIMEOUT, remaining),
            )
            resp.raise_for_status()
            if len(resp.content) > _MAX_FILE_BYTES:
                log.info(
                    "cross_file_context_degraded",
                    extra={"stage": "size", "path": path, "bytes": len(resp.content)},
                )
                continue
            # Extract snippet around symbol matches.
            snippet = _symbol_snippet(resp.text, symbols)
            if snippet is None:
                log.info(
                    "cross_file_context_degraded",
                    extra={"stage": "no_symbol_match", "path": path},
                )
                continue
            # Determine which symbols actually appear in this file's snippet.
            matched = tuple(s for s in symbols if s in snippet)
            if not matched:
                log.info(
                    "cross_file_context_degraded",
                    extra={"stage": "no_symbol_match", "path": path},
                )
                continue
            candidates.append(
                CrossFileCandidate(
                    path=path,
                    content=snippet,
                    matched_symbols=matched,
                    tag=FileTag.CALLER,  # placeholder; will be retagged
                )
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.info(
                "cross_file_context_degraded",
                extra={"stage": "content", "path": path, "error": str(e)},
            )

    # Rank and trim to character budget.
    ranked = rank_cross_file_candidates(candidates)
    return {c.path: c.content for c in ranked}
