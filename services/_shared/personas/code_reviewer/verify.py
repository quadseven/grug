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
- ``collection_lockstep``: a ``caller-not-updated`` claim whose only
  evidence is that one collection grew, so a SECOND collection must grow
  in lockstep (registry gained an entry, therefore the allowlist must
  too), anchored on a line that is not a def/raise/return. The rule is
  scoped to a changed signature, return shape or raise; two lists that
  share vocabulary do not share a membership rule. Live instance (#763):
  PR #762 grew the DoR rule registry and Elder demanded the persona-purity
  call-target allowlist grow with it, while the new rule dispatches inside
  ``run_all`` and the purity attester passed on the reviewed head.
- ``return_value_used``: a "return value is discarded / ignored / never
  read" claim naming its callee, where every call site of that callee in
  the fetched files consumes the result (a condition, an assignment, a
  return, an argument) and none is a bare statement. Live instance:
  mod-overseer PR 1083 marked ``GearReadOneEquipSpell``'s result as
  discarded while its only caller read
  ``if (!GearReadOneEquipSpell(spell.effects, resolved)) ...``. Callers in
  files the dispatch did not fetch are a residual risk, accepted under the
  inconclusive-keeps bias like ``sync_context``'s cross-file callers.
- ``callee_not_async``: a missing-await claim on a Python anchor whose
  every named callee is a plain ``def`` in the fetched files, with no
  ``async def`` of that name, no awaitable return, and no rebinding.
  ``sync_context`` leaves missing-await out on purpose, so nothing checked
  the callee. Live instances: every sampled marking of this class was
  declined (one public PR got the same claim three times); each named a
  plain ``def`` from the same diff, where the ``await`` it asked for would
  raise TypeError.
- ``redaction_artifact``: a non-secret finding built on Grug's OWN input
  redaction. The ``env-secret`` pattern in ``redact.py`` matches plain
  code (``api_key: fake_achievements``, ``token = get_app_jwt()``), so
  the model sees ``[REDACTED:env-secret]`` where a name sits in the head
  file. Live instance: a test lambda
  ``lambda steam_id, app_id, api_key: fake_achievements.get(...)`` drew
  two high markings (a NameError, and "str has no .get") on a test that
  passes. Kills only when the pattern re-matches the raw lines near the
  anchor and every value it would hide there is a name the module uses
  as code; a quoted literal is a real secret and keeps the finding.

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

from redact import SECRET_PATTERNS

if TYPE_CHECKING:  # import cycle guard: persona imports nothing from here
    from personas.code_reviewer.persona import Finding


@dataclass(frozen=True, slots=True)
class KilledFinding:
    finding: "Finding"
    # "non_code_file" | "sync_context" | "fix_already_present" |
    # "fix_block_present" | "mitigation_present" | "collection_lockstep" |
    # "return_value_used" | "callee_not_async" | "redaction_artifact"
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


# collection_lockstep (#763). The rule this kill is scoped to: a changed
# signature, return shape or raise with a stale caller.
_CALLER_NOT_UPDATED = "caller-not-updated"

# Collection nouns. The kill needs TWO mentions: the lockstep shape names
# the list that grew AND the list that supposedly must follow it. One
# collection plus its consumer (an enum gained a member that a match does
# not handle) is a different claim and is kept.
_COLLECTION_NOUN_RE = re.compile(
    r"(?<![a-z0-9])(?:allow-?list|allowed list|deny-?list|white-?list"
    r"|registry|list|set|frozenset|tuple|enum|mapping)(?![a-z0-9])"
)

# Lockstep-growth vocabulary: "A grows, so B must also add ...".
_LOCKSTEP_RE = re.compile(
    r"(?<![a-z0-9])(?:grows?|grew|grown|lockstep|in step|must also"
    r"|also (?:add|include|list|name)s?|still (?:names|lists|has|holds)"
    r" only|still only)(?![a-z0-9])"
)

# The rule's REAL shape. Any of these in the message means the claim may be
# about a signature/return/raise change, so the kill never applies.
_SIGNATURE_CLAIM_RE = re.compile(
    r"(?<![a-z0-9])(?:signature|arguments?|args?|parameters?|params?"
    r"|kwargs?|keyword|positional|arity|returns?|returned|returning"
    r"|raises?|raised|raising|exceptions?|throws?|thrown)(?![a-z0-9])"
)

# An anchored line that defines a callable or changes what it raises or
# returns may be the rule's real shape, whatever the message says.
_SIGNATURE_LINE_RE = re.compile(
    r"^\s*(?:async\s+def|def|class|function|func|fn|raise|return|throw)\b"
    r"|\blambda\b|=>"
)


def _kills_as_collection_lockstep(finding: "Finding", source: str) -> bool:
    """A caller-not-updated claim that is really "list A grew, so list B
    must grow" (#763, the PR #762 marking).

    Every condition must hold, so the kill only fires on the lockstep shape:
    the rule is caller-not-updated, the message names two collections and a
    lockstep-growth phrase, the message names no signature/return/raise
    change, and the anchored line (which the rule requires to be the changed
    signature or raise) is none of those either.
    """
    if finding.rule_name != _CALLER_NOT_UPDATED:
        return False
    message = finding.message.lower()
    if _SIGNATURE_CLAIM_RE.search(message):
        return False
    if len(_COLLECTION_NOUN_RE.findall(message)) < 2:
        return False
    if not _LOCKSTEP_RE.search(message):
        return False
    lines = source.splitlines()
    if not 1 <= finding.line <= len(lines):
        return False  # anchor outside the snapshot - inconclusive, keep
    return not _SIGNATURE_LINE_RE.search(lines[finding.line - 1])


# return_value_used. The claim family: the result of a call is thrown away.
# Both halves must appear (rule slug or message), so a dead-code marking
# about an unused FUNCTION never reads as a return-value claim.
_DISCARDED_CLAIM_RE = re.compile(
    r"discard|ignor|never (?:read|used|checked)|not (?:read|used|checked)"
    r"|unused[-_ ]return|dead[-_ ]code"
)
_RETURN_SUBJECT_RE = re.compile(r"return(?:ed)?[-_ ]values?|(?<![a-z0-9])results?(?![a-z0-9])")

# The callee the message names, in the three phrasings models use. A
# qualified name (`Foo::bar`, `obj.bar`) and trailing `()` are accepted;
# only the last segment is searched for.
_CALLEE = r"`(?:[A-Za-z_][\w]*(?:::|\.|->))*([A-Za-z_]\w*)(?:\([^`]*\))?`"
_CLAIMED_CALLEE_RES = (
    re.compile(r"return(?:ed)?[-_ ]value of\s+(?:the\s+)?(?:call\s+to\s+)?" + _CALLEE),
    re.compile(_CALLEE + r"(?:'s|\s+call's)\s+return(?:ed)?[-_ ]value"),
    re.compile(r"result of\s+(?:the\s+)?(?:call\s+to\s+)?" + _CALLEE),
)

# Line comments (`//`, `#`) are stripped before call sites are read, so a
# comment naming the callee is neither evidence nor counter-evidence.
_LINE_COMMENT_RE = re.compile(r"//.*$|#.*$")

# The receiver chain right before a call (`obj.`, `self->`, `ns::`), which is
# dropped so the classification reads what precedes the whole expression.
_RECEIVER_RE = re.compile(r"(?:(?:[A-Za-z_]\w*)?(?:::|\.|->))+$")

# What precedes a call whose result is CONSUMED: a condition, assignment,
# comparison, return, boolean operand, argument, subscript or ternary arm.
_CONSUMED_PUNCT = ("=", "(", ",", "[", "!", "?", "&&", "||", "+", "-", "<<")
_CONSUMED_WORDS = frozenset({
    "return", "not", "and", "or", "if", "elif", "while", "assert", "yield",
    "in", "is",
})

# What precedes a BARE statement call: nothing, a block or statement
# boundary, or an explicit discard (`(void)`, `_ =`, `else`, `do`, and
# Go's `go`/`defer`, which drop the result).
_BARE_PUNCT = ("{", "}", ";")
_BARE_WORDS = frozenset({"else", "do", "go", "defer"})
_EXPLICIT_DISCARD_RE = re.compile(r"\(\s*void\s*\)$|(?<![\w.])_\s*=$")

# A trailing identifier that is not a keyword above is a type or a
# definition keyword (`bool X(`, `def X(`, `static int X(`): a declaration,
# which says nothing about callers.
_TRAILING_WORD_RE = re.compile(r"([A-Za-z_]\w*)$")


def _claimed_discarded_callee(finding: "Finding") -> str | None:
    """The callee a "return value discarded" claim names, or None when the
    finding is not that claim or names no callee."""
    text = f"{finding.rule_name} {finding.message}".lower()
    if not (_DISCARDED_CLAIM_RE.search(text) and _RETURN_SUBJECT_RE.search(text)):
        return None
    for pattern in _CLAIMED_CALLEE_RES:
        m = pattern.search(finding.message)
        if m:
            return m.group(1)
    return None


def _call_site_kind(prefix: str) -> str:
    """Classify a call by the text before it on its line: "consumed",
    "bare", "definition", or "unknown" (inconclusive)."""
    head = _RECEIVER_RE.sub("", prefix).rstrip()
    for awaiting in ("co_await", "await"):
        if head.endswith(awaiting) and not head[: -len(awaiting)][-1:].isalnum():
            head = head[: -len(awaiting)].rstrip()
    if not head or head.endswith(_BARE_PUNCT) or _EXPLICIT_DISCARD_RE.search(head):
        return "bare"
    if head.endswith(_CONSUMED_PUNCT):
        return "consumed"
    word = _TRAILING_WORD_RE.search(head)
    if word:
        name = word.group(1)
        if name in _CONSUMED_WORDS:
            return "consumed"
        if name in _BARE_WORDS:
            return "bare"
        return "definition"
    return "unknown"


def _kills_as_return_value_used(
    finding: "Finding", contents: dict[str, str],
) -> bool:
    """A "return value is discarded" claim whose callee is consumed at every
    call site in the fetched files (mod-overseer PR 1083).

    Kills only when at least one site consumes the result and no site is a
    bare statement or unreadable: one bare call proves the claim may be
    right, and an unknown shape (a call inside a string, after a `:`, on a
    continuation line) is no evidence either way.
    """
    callee = _claimed_discarded_callee(finding)
    if callee is None:
        return False
    call_re = re.compile(rf"(?<![\w$]){re.escape(callee)}\s*\(")
    consumed = 0
    for text in contents.values():
        if callee not in text:
            continue
        for raw in text.splitlines():
            line = _LINE_COMMENT_RE.sub("", raw)
            for m in call_re.finditer(line):
                kind = _call_site_kind(line[: m.start()])
                if kind in ("bare", "unknown"):
                    return False
                if kind == "consumed":
                    consumed += 1
    return consumed > 0


# callee_not_async. The claim family: a call's coroutine is never awaited.
_MISSING_AWAIT_RE = re.compile(
    r"missing[-_ ]await|(?:not|never)[-_ ]awaited|un-?awaited"
    r"|without (?:an? )?`?await|must be awaited|needs? to be awaited"
    r"|forgot(?:ten)? to await"
)

# The callees a missing-await message names: the identifier right before
# the first `(` in each backticked span (`a._probe(a.paths[0])` -> _probe).
_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")
_SPAN_CALL_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")

# Return annotations and returned calls that make a plain def hand back an
# awaitable anyway; such a callee still needs its `await`.
_AWAITABLE_HINT_RE = re.compile(r"Awaitable|Coroutine|Future|Task|AsyncIterator|AsyncGenerator")
_AWAITABLE_FACTORIES = frozenset({
    "create_task", "ensure_future", "gather", "wait_for", "run_in_executor",
    "to_thread", "run_coroutine_threadsafe", "sleep", "shield",
})


def _claimed_async_callees(finding: "Finding") -> tuple[str, ...]:
    """The callees a missing-await claim names, or () when the finding is
    not that claim or names no call."""
    if not _MISSING_AWAIT_RE.search(f"{finding.rule_name} {finding.message}".lower()):
        return ()
    names: list[str] = []
    for span in _BACKTICK_SPAN_RE.findall(finding.message):
        m = _SPAN_CALL_RE.search(span)
        if m and m.group(1) != "await" and m.group(1) not in names:
            names.append(m.group(1))
    return tuple(names)


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _returns_awaitable(fn: ast.FunctionDef, async_names: set[str]) -> bool:
    """A plain def that can still hand back an awaitable: an awaitable
    return annotation, an async-ish decorator, or a returned call to an
    async def or a known awaitable factory."""
    if fn.returns is not None and _AWAITABLE_HINT_RE.search(ast.unparse(fn.returns)):
        return True
    if any("async" in ast.unparse(d).lower() for d in fn.decorator_list):
        return True
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            name = _call_name(node.value)
            if name in async_names or name in _AWAITABLE_FACTORIES:
                return True
    return False


def _is_rebound(tree: ast.Module, name: str) -> bool:
    """`name` is assigned anywhere (`x._probe = AsyncMock()`): whatever it
    is bound to may be awaitable, so the def alone proves nothing."""
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for t in targets:
            for sub in ast.walk(t):
                if (isinstance(sub, ast.Name) and sub.id == name) or (
                    isinstance(sub, ast.Attribute) and sub.attr == name
                ):
                    return True
    return False


def _kills_as_callee_not_async(
    finding: "Finding", contents: dict[str, str],
) -> bool:
    """A missing-await claim whose every named callee is provably sync.

    Kills only when each callee has at least one plain ``def`` in the
    fetched Python files and no ``async def`` of that name, no definition
    that may return an awaitable, and no rebinding anywhere. A callee in
    none of the fetched files, or any unparseable file that mentions it,
    is inconclusive and keeps the finding.
    """
    if not finding.file.endswith((".py", ".pyi")):
        return False
    callees = _claimed_async_callees(finding)
    if not callees:
        return False
    trees = _parsed_python(contents, callees)
    if trees is None:
        return False
    async_names = {
        n.name for t in trees for n in ast.walk(t)
        if isinstance(n, ast.AsyncFunctionDef)
    }
    return all(_is_plain_sync_callee(c, trees, async_names) for c in callees)


def _parsed_python(
    contents: dict[str, str], callees: tuple[str, ...],
) -> list[ast.Module] | None:
    """Every fetched Python file, parsed. None (inconclusive) when a file
    that mentions a callee does not parse."""
    trees: list[ast.Module] = []
    for path, text in contents.items():
        if not path.endswith((".py", ".pyi")):
            continue
        try:
            trees.append(ast.parse(text))
        except (SyntaxError, ValueError):
            if any(c in text for c in callees):
                return None
    return trees


def _is_plain_sync_callee(
    callee: str, trees: list[ast.Module], async_names: set[str],
) -> bool:
    """`callee` has a plain def, no async def, no def that may return an
    awaitable, and no rebinding in the fetched files."""
    if callee in async_names:
        return False
    defs = [
        n for t in trees for n in ast.walk(t)
        if isinstance(n, ast.FunctionDef) and n.name == callee
    ]
    if not defs or any(_returns_awaitable(d, async_names) for d in defs):
        return False
    return not any(_is_rebound(t, callee) for t in trees)


# redaction_artifact. Grug's own placeholders, and the one pattern whose
# false positives on plain code produce them. Every other pattern matches a
# real token format, so a placeholder of any other kind is never an
# artifact.
_PLACEHOLDER_RE = re.compile(r"\[REDACTED:([a-z0-9-]+)\]")
_ARTIFACT_KIND = "env-secret"
_ENV_SECRET_RE = next(
    (p for p, repl in SECRET_PATTERNS if f"[REDACTED:{_ARTIFACT_KIND}]" in repl),
    None,
)

# Secret-class claims always keep: a placeholder in a leak finding may be
# hiding exactly the secret the finding is about. Checked with the
# placeholders stripped, since the label itself says "secret".
_SECRET_CLASS_RE = re.compile(
    r"secret|credential|token|leak|passw|hard-?coded|exposed"
)


def _code_names(tree: ast.Module) -> set[str]:
    """Every identifier the module uses as code: names, attributes,
    parameters and definitions. String contents never land here."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
    return out


def _kills_as_redaction_artifact(finding: "Finding", source: str) -> bool:
    """A non-secret claim built on the ``env-secret`` placeholder our own
    input redaction put over plain code.

    Every condition must hold: the anchor is Python; the message cites only
    ``env-secret`` placeholders; the claim is not secret-class; the raw head
    text within 2 lines of the anchor carries no placeholder of its own; and
    the redaction pattern re-matches there with every hidden value an
    unquoted identifier the module uses as code. A quoted literal, or a
    ``key=value`` inside a string, is a real secret and keeps the finding.
    """
    if _ENV_SECRET_RE is None or not finding.file.endswith((".py", ".pyi")):
        return False
    kinds = _PLACEHOLDER_RE.findall(finding.message)
    if not kinds or any(k != _ARTIFACT_KIND for k in kinds):
        return False
    claim = _PLACEHOLDER_RE.sub(" ", f"{finding.rule_name} {finding.message}").lower()
    if _SECRET_CLASS_RE.search(claim):
        return False
    window = _anchor_window(source, finding.line, radius=2)
    if "[REDACTED" in window:
        return False
    matches = list(_ENV_SECRET_RE.finditer(window))
    if not matches:
        return False
    try:
        names = _code_names(ast.parse(source))
    except (SyntaxError, ValueError):
        return False
    for m in matches:
        value = m.group(0)[len(m.group(1)):]
        if not re.fullmatch(r"[A-Za-z_]\w*", value) or value not in names:
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

    # Return-value-used kill (mod-overseer PR 1083): "the result is
    # discarded" while every fetched call site reads it. Takes the whole
    # contents dict, since callers usually live outside the anchored file.
    if not _is_prose_file(finding.file) and _kills_as_return_value_used(
        finding, contents,
    ):
        return "return_value_used"

    # Callee-not-async kill: "this call is never awaited" while every named
    # callee is a plain def in the fetched files. Takes the whole contents
    # dict, since the definition usually lives outside the anchored file.
    if _kills_as_callee_not_async(finding, contents):
        return "callee_not_async"

    # Redaction-artifact kill: the claim reads Grug's own env-secret
    # placeholder as code, where the raw head file has a plain name.
    if _kills_as_redaction_artifact(finding, source):
        return "redaction_artifact"

    # Collection-lockstep kill (#763): "registry grew, so the allowlist must
    # grow" is not a stale caller. Needs the source so the anchored line can
    # be checked for a real signature/raise/return change.
    if _kills_as_collection_lockstep(finding, source):
        return "collection_lockstep"

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


# --- bound-name downgrade (#1227) -----------------------------------------
#
# Live instance 2026-10-17: three blocking markings on one pull request
# claimed NameErrors for `now` and `authors`, both parameters of the
# enclosing method whose signature sat ~40 lines above the hunk. This is a
# DOWNGRADE, not a kill: the claim is contradicted by the post-change file,
# but a name being bound does not prove every neighbouring claim wrong, so
# the finding still publishes - as a non-blocking note that cannot stop a
# correct merge. Same asymmetric bias as the kills above: anything
# inconclusive (no source, unparseable, no extractable name, any named
# identifier not provably bound) keeps the finding exactly as it was.


@dataclass(frozen=True, slots=True)
class DowngradedFinding:
    finding: "Finding"  # the finding as the model produced it
    names: tuple[str, ...]  # the claimed-undefined names, all proven bound


# "is not defined" class: NameError, undefined/unbound/unresolved name.
_NOT_DEFINED_RE = re.compile(
    r"nameerror|not\s+defined|undefined|unbound|unresolved\s+(?:name|reference)"
    r"|undeclared",
    re.IGNORECASE,
)

# Bare identifiers quoted in the message: `now`, 'now', "now".
_QUOTED_NAME_RE = re.compile(r"[`'\"]([A-Za-z_][A-Za-z0-9_]*)[`'\"]")

# The note appended to a downgraded marking so a reader sees why it no
# longer blocks.
_DOWNGRADE_NOTE = (
    " [Downgraded to a note: {names} bound in an enclosing scope of the "
    "post-change file.]"
)

_DOWNGRADED_SEVERITY = "low"
_BLOCKING = ("high", "critical")


def _is_not_defined_claim(finding: "Finding") -> bool:
    return bool(
        _NOT_DEFINED_RE.search(finding.rule_name)
        or _NOT_DEFINED_RE.search(finding.message)
    )


def _claimed_names(message: str) -> tuple[str, ...]:
    seen: list[str] = []
    for name in _QUOTED_NAME_RE.findall(message):
        if name not in seen and name != "NameError":
            seen.append(name)
    return tuple(seen)


def _target_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _import_names(node: ast.AST) -> set[str]:
    return {
        (a.asname or a.name).split(".")[0]
        for a in node.names  # type: ignore[attr-defined]
        if a.name != "*"
    }


def _with_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for item in node.items:  # type: ignore[attr-defined]
        if item.optional_vars is not None:
            out |= _target_names(item.optional_vars)
    return out


def _assign_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for t in node.targets:  # type: ignore[attr-defined]
        out |= _target_names(t)
    return out


# Node type -> the names that node binds at its OWN scope level. An
# annotation without a value (`x: int`) binds nothing.
_BINDERS = {
    ast.Import: _import_names,
    ast.ImportFrom: _import_names,
    ast.Assign: _assign_names,
    ast.AugAssign: lambda n: _target_names(n.target),
    ast.AnnAssign: lambda n: _target_names(n.target) if n.value is not None else set(),
    ast.For: lambda n: _target_names(n.target),
    ast.AsyncFor: lambda n: _target_names(n.target),
    ast.NamedExpr: lambda n: _target_names(n.target),
    ast.With: _with_names,
    ast.AsyncWith: _with_names,
    ast.Global: lambda n: set(n.names),
    ast.Nonlocal: lambda n: set(n.names),
    ast.FunctionDef: lambda n: {n.name},
    ast.AsyncFunctionDef: lambda n: {n.name},
    ast.ClassDef: lambda n: {n.name},
}

# Nodes whose bodies are a different scope: their own name binds, their
# contents do not.
_NEW_SCOPE_NODES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)


def _is_type_checking_test(test: ast.expr) -> bool:
    """`TYPE_CHECKING` or `<module>.TYPE_CHECKING`: a block that never runs."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _bindings_in(stmts: list[ast.stmt], before_line: int | None) -> set[str]:
    """Names bound by `stmts` at their own scope level (not descending into
    nested functions/classes/lambdas/comprehensions). With `before_line`,
    only bindings that START before that line count - a local assigned
    after its use is a real unbound-local bug, not a false marking.

    Two bindings never exist at runtime and are skipped (#1338): the body
    of `if TYPE_CHECKING:` (its `else` still runs), and an `except ... as
    err` name outside its own handler - Python deletes it when the handler
    ends, so only a line inside the handler sees it (never module scope,
    where `before_line` is None)."""
    bound: set[str] = set()
    stack: list[ast.AST] = list(stmts)
    while stack:
        node = stack.pop()
        line = getattr(node, "lineno", None)
        if isinstance(node, ast.ExceptHandler):
            end = node.end_lineno or node.lineno
            if node.name and before_line is not None and node.lineno <= before_line <= end:
                bound.add(node.name)
        binder = _BINDERS.get(type(node))
        if binder is not None and (
            before_line is None or (line is not None and line < before_line)
        ):
            bound |= binder(node)
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            stack.extend(node.orelse)
        elif not isinstance(node, _NEW_SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(node))
    return bound


def _function_chain(tree: ast.Module, line: int) -> list[ast.AST]:
    """Outermost-to-innermost FUNCTIONS whose span holds `line` (class
    bodies are not enclosing scopes for their methods)."""
    chain: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= line <= end:
                chain.append(node)
    chain.sort(key=lambda n: n.lineno)
    return chain


def _params(fn: ast.AST) -> set[str]:
    a = fn.args  # type: ignore[attr-defined]
    names = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    if a.vararg is not None:
        names.add(a.vararg.arg)
    if a.kwarg is not None:
        names.add(a.kwarg.arg)
    return names


def _bound_at(source: str, line: int) -> set[str] | None:
    """Every name provably bound at `line` of `source`: enclosing-function
    parameters, earlier bindings in those functions, module-level bindings
    and builtins. None when the module does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    import builtins

    bound = set(dir(builtins))
    bound |= _bindings_in(tree.body, None)
    for fn in _function_chain(tree, line):
        bound |= _params(fn)
        bound |= _bindings_in(fn.body, line)  # type: ignore[attr-defined]
    return bound


def downgrade_bound_names(
    findings: tuple["Finding", ...], file_contents: dict[str, str],
) -> tuple[tuple["Finding", ...], tuple[DowngradedFinding, ...]]:
    """Downgrade blocking "is not defined" findings whose every named
    identifier is bound in an enclosing scope of the post-change file to a
    non-blocking note (#1227). Returns (findings, downgraded); order is
    preserved and untouched findings are returned as the same objects.
    Never raises: any per-finding error keeps the finding as it was."""
    from dataclasses import replace

    out: list["Finding"] = []
    downgraded: list[DowngradedFinding] = []
    for f in findings:
        try:
            names = _maybe_bound_names(f, file_contents)
        except Exception:  # noqa: BLE001 - verification must never abort a review
            names = None
        if not names:
            out.append(f)
            continue
        shown = ", ".join(f"`{n}`" for n in names)
        verb = "is" if len(names) == 1 else "are"
        note = _DOWNGRADE_NOTE.format(names=f"{shown} {verb}")
        out.append(replace(f, severity=_DOWNGRADED_SEVERITY, message=f.message + note))
        downgraded.append(DowngradedFinding(finding=f, names=names))
    return tuple(out), tuple(downgraded)


def _maybe_bound_names(
    finding: "Finding", file_contents: dict[str, str],
) -> tuple[str, ...] | None:
    if finding.severity not in _BLOCKING:
        return None
    if not finding.file.endswith((".py", ".pyi")):
        return None
    if not _is_not_defined_claim(finding):
        return None
    source = file_contents.get(finding.file)
    if source is None:
        return None
    names = _claimed_names(finding.message)
    if not names:
        return None
    bound = _bound_at(source, finding.line)
    if bound is None or not all(n in bound for n in names):
        return None
    return names
