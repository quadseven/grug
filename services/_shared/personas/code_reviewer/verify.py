"""Repo-grounded verification pass for Elder findings (#708, epic #707).

The #707 scoreboard measured 64% of Elder's classified inline markings
getting REJECTED by PR authors, and the rejected class shares one root
cause: markings are published straight from model judgment over the diff,
with no step that checks the load-bearing claim against the repository.
Every same-day rejected example (PRs #694/#698/#706) was refutable with
one cheap, deterministic repo interrogation.

This module is that interrogation. It runs AFTER the exploitability judge
and BEFORE publication, over the `file_contents` snapshot the dispatch
already fetched (zero extra network), and kills findings whose claim the
evidence contradicts:

- ``non_code_file``: a code-execution-class rule (injection, async/sync,
  null-deref, ...) anchored in prose (docs/markdown). Prose-class rules
  (claim drift, typos) survive on prose files.
- ``sync_context``: an async-blocker-family rule (sync-io-in-async,
  missing-await, event-loop stalls) whose anchored line sits inside a
  plain ``def`` with no ``async`` anywhere in the enclosing chain -
  nothing there can block an event loop or need an ``await``.
- ``fix_already_present``: every code-ish token of the finding's own
  ``suggestion`` already appears verbatim in the anchored line span -
  the marking describes a fix that is already in the code under review.

Inconclusive is NOT a kill: a missing file, an unparseable module, a
module-level line, or a suggestion with no code tokens all keep the
finding. The bias is asymmetric on purpose - a false kill silently
hides a real bug, while a false keep costs one judged-and-rejected
comment - so every check must positively CONTRADICT the claim to kill.

Kills are returned with machine-readable reasons; the dispatch logs one
structured row per kill (``code_review_verification_killed``) so the
scoreboard can track verification's precision contribution and,
symmetrically, hunt false kills.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import cycle guard: persona imports nothing from here
    from personas.code_reviewer.persona import Finding


@dataclass(frozen=True, slots=True)
class KilledFinding:
    finding: "Finding"
    reason: str  # "non_code_file" | "sync_context" | "fix_already_present"


# Prose file suffixes: findings about EXECUTING these are category errors.
_PROSE_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")

# Rule-slug markers implying the flagged code EXECUTES. Substring-matched
# against rule_name; a prose file cannot have an event loop, an injection
# sink, or a null deref.
_CODE_EXECUTION_MARKERS = (
    "injection", "await", "async", "sync", "event-loop", "blocking",
    "deadlock", "race", "leak", "null", "deref", "except", "error-handling",
    "timeout", "overflow", "sleep", "thread",
)

# Docs-class rule markers: these rules are ABOUT prose (claim drift, stale
# comments, typos, broken links) and legitimately anchor in markdown - they
# are exempt from the prose kill even when their slug or message quotes
# execution vocabulary like "timeout" (CodeRabbit on PR #710: a
# `doc-async-claim-drift` finding must survive on a .md file).
_DOCS_CLASS_MARKERS = (
    "doc", "claim", "typo", "comment", "link", "readme", "changelog",
)

# Rule-slug markers for the async-blocker family (the sync_context check).
_ASYNC_FAMILY_MARKERS = (
    "await", "async", "event-loop", "sync-io", "sync-in-async", "blocking",
)

# Code-ish tokens inside a suggestion: identifiers glued to call or attr
# syntax. Ordinary prose words never match. Bare-assign tokens (timeout=)
# are deliberately NOT extracted (CodeRabbit on PR #710): truncating
# `timeout=30` to `timeout=` would match an unrelated `timeout=None` and
# false-kill; without a value-aware representation the assign form cannot
# prove the suggested fix is present.
_CODE_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*\s*\("   # call:  strip(  /  to_thread(
    r"|\.[A-Za-z_][A-Za-z0-9_]*"     # attr:  .strip  /  .casefold
)


def _is_prose_file(path: str) -> bool:
    return path.lower().endswith(_PROSE_SUFFIXES)


def _rule_matches(rule_name: str, markers: tuple[str, ...]) -> bool:
    slug = rule_name.lower()
    return any(m in slug for m in markers)


def _enclosing_chain_is_sync(source: str, line: int) -> bool | None:
    """True if `line` sits inside function defs and NONE of the enclosing
    chain is async. None = inconclusive (unparseable, or module level)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    chain: list[ast.AST] = []

    def walk(node: ast.AST, stack: list[ast.AST]) -> None:
        nonlocal chain
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = child.lineno
                end = getattr(child, "end_lineno", start)
                if start <= line <= end:
                    candidate = stack + [child]
                    # innermost wins: deeper chain replaces shallower
                    if len(candidate) > len(chain):
                        chain = candidate
                    walk(child, candidate)
                    continue
            walk(child, stack)

    walk(tree, [])
    if not chain:
        return None
    return not any(isinstance(n, ast.AsyncFunctionDef) for n in chain)


def _suggestion_tokens(suggestion: str) -> tuple[str, ...]:
    return tuple({m.group(0).strip() for m in _CODE_TOKEN_RE.finditer(suggestion)})


def _module_has_async(source: str) -> bool:
    """True if the module contains ANY async construct. Cheap text probe
    first; ast confirms (a comment mentioning 'async' must not count)."""
    if "async" not in source and "await" not in source:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True  # unparseable + async-ish text: treat as async-capable
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            return True
    return False


def _anchor_window(source: str, line: int, radius: int = 2) -> str:
    lines = source.splitlines()
    lo = max(0, line - 1 - radius)
    hi = min(len(lines), line + radius)
    return "\n".join(lines[lo:hi])


def _verify_one(finding: "Finding", contents: dict[str, str]) -> str | None:
    """Return a kill reason, or None to keep."""
    source = contents.get(finding.file)

    # Prose kill. Docs-class rules (claim drift, typos, stale comments) are
    # exempt FIRST - they legitimately anchor in markdown even when their
    # text quotes execution vocabulary (CodeRabbit on PR #710). For the
    # rest, the execution claim may live in the slug OR the prose (the PR
    # #706 instance was rule `unvalidated-external-input` with "command
    # injection" only in the message) - check both. The evidence here is
    # the PATH itself (present by construction), so this check does not
    # need `source`.
    if (
        _is_prose_file(finding.file)
        and not _rule_matches(finding.rule_name, _DOCS_CLASS_MARKERS)
        and (
            _rule_matches(finding.rule_name, _CODE_EXECUTION_MARKERS)
            or _rule_matches(finding.message, _CODE_EXECUTION_MARKERS)
        )
    ):
        return "non_code_file"

    if source is None:
        return None  # no evidence either way - keep

    # Sync-context kill, tightened per CodeRabbit on PR #710: a lexically
    # sync def could still block a loop if an async caller invokes it
    # directly, so the kill additionally requires the MODULE to contain no
    # async code at all - then nothing in-file can put the flagged line on
    # an event loop. Cross-file async callers of a module with zero async
    # remain a residual risk, accepted under the inconclusive-keeps bias
    # and monitored via the false-kill scoreboard.
    if (
        not _is_prose_file(finding.file)
        and _rule_matches(finding.rule_name, _ASYNC_FAMILY_MARKERS)
        and _enclosing_chain_is_sync(source, finding.line) is True
        and not _module_has_async(source)
    ):
        return "sync_context"

    if finding.suggestion:
        tokens = _suggestion_tokens(finding.suggestion)
        if tokens:
            # Anchor line ONLY (radius 0, CodeRabbit on PR #710): a wider
            # window let an unrelated neighboring `.strip()` prove the
            # wrong claim.
            window = _anchor_window(source, finding.line, radius=0)
            if all(t in window for t in tokens):
                return "fix_already_present"

    return None


def verify_findings(
    findings: tuple["Finding", ...], file_contents: dict[str, str],
) -> tuple[tuple["Finding", ...], tuple[KilledFinding, ...]]:
    """Partition findings into (kept, killed-with-reasons). Order-preserving.

    Never raises on malformed inputs: any per-finding verification error
    keeps the finding (inconclusive-keeps bias, documented above).
    """
    kept: list["Finding"] = []
    killed: list[KilledFinding] = []
    for f in findings:
        try:
            reason = _verify_one(f, file_contents)
        except Exception:  # noqa: BLE001 - verification must never abort a review
            reason = None
        if reason is None:
            kept.append(f)
        else:
            killed.append(KilledFinding(finding=f, reason=reason))
    return tuple(kept), tuple(killed)
