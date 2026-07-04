# MIRRORED — sibling at services/api/personas/code_reviewer/cross_file.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Cross-file review context - 1-hop symbol resolution (#468, epic #464
slice 4 = PRD #346 P1.1, the repo-graph review capability tracer).

The Elder sees changed files (whole-file since #336) but nothing else in
the repo. The highest-value review questions are cross-file: "does the
CALLER of this changed function handle the new signature?". This module
resolves the diff's interesting symbols to the UNCHANGED files that
define or call them, so those files ride into the review prompt as extra
context (rendered by `llm_client._build_messages`, flagged never - the
paired `caller-not-updated` rule anchors findings on the DIFF line).

PREMISE NOTE (recorded in DESIGN.md): GitHub's `/search/code` indexes the
DEFAULT BRANCH only, so search is used solely for PATH discovery; each
discovered file's CONTENT is then fetched at the PR's head SHA via the
contents API (SHA-accurate). A caller that exists only on the PR branch
is invisible to search - accepted for the tracer (callers needing update
live on the default branch).

TRACER SCOPE: symbol extraction is Python-focused regex (defs + calls on
added lines). The lazy tree-sitter repo index is #346 P1.2, deferred.

FAIL-SAFE contract: every error path degrades to `{}` - today's
diff-only review - and logs `cross_file_context_degraded`. No cache:
`claim_review` already guarantees one review per head SHA, so a
per-head_sha cache would never hit.
"""

from __future__ import annotations

import logging
import os
import re
import time
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


_PY_LINE_DEF = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")

# Snippet bounds (codex round 7, PR #480): ship only windows AROUND the
# symbol matches, never the whole unchanged file - less data leaves the
# boundary and the prompt stays cheap. Line numbers are ORIGINAL (the
# caller-not-updated rule cites caller path:line).
_SNIPPET_RADIUS = 10
_MAX_SNIPPET_WINDOWS = 3
_MAX_SNIPPET_LINES = 80


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


def _enclosing_def_from_content(content: str, first_added_line: int) -> str | None:
    """Nearest `def` at or above `first_added_line` (1-based) in the full
    file content - the enclosing function of a body-only change whose def
    line sits OUTSIDE the diff context window (codex round 4, PR #480).
    Pure; None when no def precedes the line (module-level change)."""
    lines = content.splitlines()
    for i in range(min(first_added_line, len(lines)) - 1, -1, -1):
        m = _PY_LINE_DEF.match(lines[i])
        if m:
            return m.group(1)
    return None


def extract_symbols(
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Pull the diff's cross-file-interesting symbols:

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
    3. Called names the diff adds that are NOT defined in-diff (their
       DEFINITION elsewhere tells the reviewer what the call does).

    Pure; capped at `_MAX_SYMBOLS`; dedup preserves first-seen order.
    """
    contents = file_contents or {}
    defs: list[str] = []
    calls: list[str] = []
    defined_here: set[str] = set()
    for h in hunks:
        for name in _PY_DEF.findall(h.body):
            defined_here.add(name)
            if name not in defs:
                defs.append(name)
        enclosing = list(_PY_CONTEXT_DEF.findall(h.body))
        enclosing += _PY_SECTION_DEF.findall(h.body)
        content = contents.get(h.file_path)
        if content and h.new_lines:
            name = _enclosing_def_from_content(content, min(h.new_lines))
            if name:
                enclosing.append(name)
        for name in enclosing:
            # The enclosing def IS defined in this file - queue it for a
            # caller search, never as a call target.
            defined_here.add(name)
            if name not in defs:
                defs.append(name)
        for name in _PY_CALL.findall(h.body):
            if name not in _STOP_NAMES and name not in calls:
                calls.append(name)

    out: list[str] = list(defs)
    for name in calls:
        # A name defined in-diff needs no definition lookup; as a DEF it
        # is already queued above (callers elsewhere still matter).
        if name not in defined_here and name not in out:
            out.append(name)
    return tuple(out[:_MAX_SYMBOLS])


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
    skipped. FAIL-SAFE: any error degrades to partial-or-empty, logged.
    """
    if not symbols:
        return {}

    # Elapsed-time deadline (a comparison of monotonic DELTAS, not a
    # zero-sentinel): the whole phase - searches AND content fetches -
    # must finish inside _TOTAL_BUDGET_SECONDS.
    deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS

    paths: list[str] = []
    for sym in symbols[:_MAX_SYMBOLS]:
        # Clamp the per-call timeout to the REMAINING budget (codex round
        # 2): a call started at deadline-epsilon must not block the full
        # _SEARCH_TIMEOUT past the global cap.
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
            # 403 = rate-limited (code-search is the tightest GitHub
            # bucket); transport = blip. Either way: degrade, log, and
            # STOP searching - further calls would burn the same budget.
            log.info(
                "cross_file_context_degraded",
                extra={"stage": "search", "symbol": sym, "error": str(e)},
            )
            break
        for item in items:
            p = item.get("path")
            # Tracer scope: Python callers only. Also cuts injection/noise
            # surface — a README or docs file mentioning the symbol is
            # attacker-influenceable prose, not a caller (codex round 5).
            if (
                p
                and p.endswith(".py")
                and p not in exclude_paths
                and p not in paths
            ):
                paths.append(p)
        if len(paths) >= _MAX_FILES:
            break

    contents: dict[str, str] = {}
    for path in paths[:_MAX_FILES]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.info(
                "cross_file_context_degraded",
                extra={"stage": "budget", "phase": "content", "collected": len(contents)},
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
                # Prompt-budget guard: an oversized context file costs
                # more than its 1-hop value. Skip it, keep the rest.
                log.info(
                    "cross_file_context_degraded",
                    extra={"stage": "size", "path": path, "bytes": len(resp.content)},
                )
                continue
            # Ship SNIPPETS around the symbol matches, not the whole file
            # (codex round 7): bounds what leaves the boundary + the
            # prompt cost. Original line numbers preserved so the
            # caller-not-updated rule can cite caller path:line. A file
            # where no symbol actually appears (search false-hit) is
            # dropped entirely.
            snippet = _symbol_snippet(resp.text, symbols)
            if snippet is None:
                log.info(
                    "cross_file_context_degraded",
                    extra={"stage": "no_symbol_match", "path": path},
                )
                continue
            contents[path] = snippet
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            # Same per-file degrade contract as #336's _fetch_file_contents:
            # skip this file, keep the rest.
            log.info(
                "cross_file_context_degraded",
                extra={"stage": "content", "path": path, "error": str(e)},
            )
    return contents
