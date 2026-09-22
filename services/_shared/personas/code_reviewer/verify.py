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

- ``non_code_file``: a rule whose claim requires EXECUTION (injection,
  event-loop, deadlock, ...; word-boundary matched) anchored in a
  document format (markdown/rst). Docs-class rules (claim drift, typos)
  and secret/credential findings survive - prose genuinely carries
  leaked tokens.
- ``sync_context``: a rule EXPLICITLY claiming async-context execution
  (sync-io-in-async, event-loop) whose anchored line sits inside a
  plain ``def`` in a module containing zero async constructs - nothing
  in-file can put that line on an event loop.
- ``fix_already_present``: every code-ish token (call/attr forms only)
  of the finding's own ``suggestion`` already appears verbatim on the
  anchored line itself - the marking describes a fix that is already
  in the code under review.
- ``mitigation_present``: an interpolation-injection claim (ssrf, url,
  injection, traversal) whose anchored line is an f-string in which EVERY
  interpolation is already wrapped in a canonical sanitizer. This is the
  sibling of ``fix_already_present`` for the case where the mitigation is
  present in a DIFFERENT form than the one the model proposed - the reason
  ``fix_already_present`` misses it, since that check is token-matched
  against the model's own suggestion. Live instance: PR #766 published
  three HIGH ssrf markings against
  ``f"https://api.github.com/repos/{quote(owner, safe='')}/..."``, each
  claiming the value was interpolated "without validation" while
  ``quote(..., safe='')`` sat inline on the very line cited. The
  suggestion said "validate"/"allowlist", which appears nowhere, so no
  token matched and all three shipped.

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
    # "non_code_file" | "sync_context" | "fix_already_present" |
    # "fix_block_present" | "mitigation_present"
    reason: str


# Prose file suffixes: findings about EXECUTING these are category errors.
# NOT .txt (workflow review on PR #710): CMakeLists.txt is executable build
# code and requirements.txt is a dependency manifest with real security
# findings - only unambiguous document formats qualify.
_PROSE_SUFFIXES = (".md", ".markdown", ".rst", ".adoc")

# Markers implying the flagged code EXECUTES, matched with word boundaries
# (workflow review on PR #710: substring matching made "grace" hit "race",
# "nullable" hit "null", "out of sync" hit "sync"). The list is trimmed to
# claims that are execution-only by nature: secret/credential "leak"
# findings are deliberately ABSENT because prose files genuinely carry
# leaked secrets - a README token is a real critical, not a category error.
_CODE_EXECUTION_RE = re.compile(
    r"(?<![a-z0-9])(?:injection|event-loop|deadlock|deref|overflow"
    r"|blocking|thread|sleep|xss|sql)(?![a-z0-9])"
)

# Docs-class rule markers: these rules are ABOUT prose (claim drift, stale
# comments, typos, broken links) and legitimately anchor in markdown - they
# are exempt from the prose kill even when their slug or message quotes
# execution vocabulary like "timeout" (FLINT on PR #710: a
# `doc-async-claim-drift` finding must survive on a .md file).
_DOCS_CLASS_MARKERS = (
    "doc", "claim", "typo", "comment", "link", "readme", "changelog",
)

# Rule-slug markers for rules that EXPLICITLY claim async-context
# execution - only these are sync_context-killable. Narrowed (workflow
# review on PR #710): "missing-await" is OUT because an imported coroutine
# callable in an all-sync module is a real coroutine-never-awaited bug;
# bare "blocking" is OUT because thread deadlocks and unbounded blocking
# calls are real bugs in fully synchronous code.
_ASYNC_FAMILY_MARKERS = ("in-async", "event-loop", "asyncio")

# Code-ish tokens inside a suggestion: identifiers glued to call or attr
# syntax. Ordinary prose words never match. Bare-assign tokens (timeout=)
# are deliberately NOT extracted (FLINT on PR #710): truncating
# `timeout=30` to `timeout=` would match an unrelated `timeout=None` and
# false-kill; without a value-aware representation the assign form cannot
# prove the suggested fix is present.
_CODE_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*\s*\("   # call:  strip(  /  to_thread(
    r"|\.[A-Za-z_][A-Za-z0-9_]*"     # attr:  .strip  /  .casefold
)


# Rule-slug/message markers for claims that an interpolated value reaches a
# sensitive sink unsanitised. Kept deliberately narrow: only families whose
# canonical mitigation is a WRAPPING CALL that is visible on the line.
_INTERPOLATION_INJECTION_MARKERS = (
    "ssrf", "url", "injection", "traversal", "unvalidated", "untrusted",
)

# Canonical sanitizers that neutralise a value being interpolated into a
# URL/path. Only wrapping calls belong here - a flag or a comparison cannot
# be proven correct from one line.
_SANITIZER_CALLS = (
    "quote", "quote_plus", "urlencode", "urllib.parse.quote",
    "shlex.quote", "escape", "quote_from_bytes",
)

# `{...}` interpolation groups inside an f-string. Nested braces are not
# handled on purpose: a nested-brace line is INCONCLUSIVE, and inconclusive
# keeps the finding.
_FSTRING_INTERP_RE = re.compile(r"\{([^{}]+)\}")


def _all_interpolations_sanitized(line: str) -> bool | None:
    """True if EVERY `{...}` on the line is wrapped in a known sanitizer.

    None (inconclusive -> keep) when the line has no interpolation at all,
    or carries nested braces this cannot reason about. Requiring EVERY
    interpolation to be wrapped is the load-bearing part: one bare
    interpolation is exactly the vulnerability being reported, so a
    partially-sanitized line must NOT be killed.
    """
    if "{" not in line:
        return None
    groups = _FSTRING_INTERP_RE.findall(line)
    if not groups:
        return None  # braces present but not a simple interpolation - unknown
    for g in groups:
        expr = g.split("!")[0].split(":")[0].strip()
        if not any(
            expr.startswith(f"{c}(") or expr.startswith(f"{c} (")
            for c in _SANITIZER_CALLS
        ):
            return False
    return True


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


# A suggested block must be at least this many non-blank lines before its
# presence can prove anything: a one- or two-line match ("try:",
# "finally:") is ordinary structure, not evidence.
_MIN_FIX_BLOCK_LINES = 3


def _normalized_nonblank(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def _enclosing_def_span(source: str, line: int) -> tuple[int, int] | None:
    """(first, last) line of the innermost function containing `line`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    best: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= line <= end and (best is None or node.lineno >= best[0]):
            best = (node.lineno, end)
    return best


def _kills_as_fix_block_present(finding: "Finding", source: str) -> bool:
    """The model's whole multi-line suggested block already sits, verbatim
    modulo whitespace, inside the function it flagged - applying the "fix"
    changes nothing, so the claimed defect contradicts the code.

    Complements `fix_already_present`, which is anchor-line-only and so
    cannot see a block the model anchored a few lines away from. Live
    instance 2026-09-22: a function's docstring was flagged as a resource
    leak, with the six-line `conn = _connect()` / `try` /
    `finally: conn.close()` block that already followed it five lines
    later proposed as the fix. Python only, and only inside the anchor's own function, so an
    identical block elsewhere in the module can never prove this claim.
    """
    if not finding.suggestion or not finding.file.endswith(".py"):
        return False
    want = _normalized_nonblank(finding.suggestion)
    if len(want) < _MIN_FIX_BLOCK_LINES:
        return False
    span = _enclosing_def_span(source, finding.line)
    if span is None:
        return False
    body = _normalized_nonblank("\n".join(source.splitlines()[span[0] - 1:span[1]]))
    width = len(want)
    return any(body[i:i + width] == want for i in range(len(body) - width + 1))


def _kills_as_non_code_file(finding: "Finding") -> bool:
    """Execution-class claim anchored in a document format. Docs-class rules
    are exempt FIRST - they legitimately anchor in markdown even when their
    text quotes execution vocabulary (FLINT on PR #710). The execution claim
    may live in the slug OR the message (the PR #706 instance was rule
    `unvalidated-external-input` with "command injection" only in the
    message). Evidence is the PATH itself, so no source is needed.
    """
    if not _is_prose_file(finding.file):
        return False
    if _rule_matches(finding.rule_name, _DOCS_CLASS_MARKERS):
        return False
    return bool(
        _CODE_EXECUTION_RE.search(finding.rule_name.lower())
        or _CODE_EXECUTION_RE.search(finding.message.lower())
    )


def _kills_as_sync_context(finding: "Finding", source: str) -> bool:
    """Async-context claim on a line that nothing in-file can put on a loop.

    Tightened per FLINT on PR #710: a lexically sync def could still block a
    loop if an async caller invokes it directly, so the kill ALSO requires the
    module to contain zero async constructs. Cross-file async callers of an
    all-sync module remain a residual risk, accepted under the
    inconclusive-keeps bias and monitored via the false-kill scoreboard.
    """
    if _is_prose_file(finding.file):
        return False
    if not _rule_matches(finding.rule_name, _ASYNC_FAMILY_MARKERS):
        return False
    return (
        _enclosing_chain_is_sync(source, finding.line) is True
        and not _module_has_async(source)
    )


def _kills_as_mitigation_present(finding: "Finding", source: str) -> bool:
    """Interpolation-injection claim on a line where EVERY interpolation is
    already wrapped in a canonical sanitizer - the mitigation is present in a
    different form than the suggestion named, which is why
    `fix_already_present` cannot see it (PR #766's three ssrf markings).
    """
    if _is_prose_file(finding.file):
        return False
    if not (
        _rule_matches(finding.rule_name, _INTERPOLATION_INJECTION_MARKERS)
        or _rule_matches(finding.message, _INTERPOLATION_INJECTION_MARKERS)
    ):
        return False
    window = _anchor_window(source, finding.line, radius=0)
    return _all_interpolations_sanitized(window) is True


def _verify_one(finding: "Finding", contents: dict[str, str]) -> str | None:
    """Return a kill reason, or None to keep."""
    source = contents.get(finding.file)

    # Prose kill. Docs-class rules (claim drift, typos, stale comments) are
    # exempt FIRST - they legitimately anchor in markdown even when their
    # text quotes execution vocabulary (FLINT on PR #710). For the
    # rest, the execution claim may live in the slug OR the prose (the PR
    # #706 instance was rule `unvalidated-external-input` with "command
    # injection" only in the message) - check both. The evidence here is
    # the PATH itself (present by construction), so this check does not
    # need `source`.
    if _kills_as_non_code_file(finding):
        return "non_code_file"

    if source is None:
        return None  # no evidence either way - keep

    # Sync-context kill, tightened per FLINT on PR #710: a lexically
    # sync def could still block a loop if an async caller invokes it
    # directly, so the kill additionally requires the MODULE to contain no
    # async code at all - then nothing in-file can put the flagged line on
    # an event loop. Cross-file async callers of a module with zero async
    # remain a residual risk, accepted under the inconclusive-keeps bias
    # and monitored via the false-kill scoreboard.
    if _kills_as_sync_context(finding, source):
        return "sync_context"

    if finding.suggestion:
        tokens = _suggestion_tokens(finding.suggestion)
        if tokens:
            # Anchor line ONLY (radius 0, FLINT on PR #710): a wider
            # window let an unrelated neighboring `.strip()` prove the
            # wrong claim.
            window = _anchor_window(source, finding.line, radius=0)
            if all(t in window for t in tokens):
                return "fix_already_present"
        if _kills_as_fix_block_present(finding, source):
            return "fix_block_present"

    # Mitigation-present kill. Same anchor-line-only discipline as
    # fix_already_present (radius 0), and the same asymmetric bias: only a
    # line where EVERY interpolation is wrapped can contradict the claim.
    if _kills_as_mitigation_present(finding, source):
        return "mitigation_present"

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
