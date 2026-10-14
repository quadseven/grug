"""Intent-first review helpers for the Elder (code-reviewer) persona (#1165).

Three behaviors live here so dispatch stays thin:

1. **Intent paragraph**: every Elder check summary opens with an
   ``## Intent`` paragraph of at most three sentences: what behavior
   changes, for whom, and whether it matches the linked issue's
   acceptance criteria. Pure and deterministic (no LLM): built from the
   PR title/body plus the already-fetched linked-issue criteria block.
2. **Docs/tests-only scope**: when every changed file is documentation
   (``*.md``, ``docs/``, ``*.txt``) or tests (the repo's test paths), the
   review reports only correctness findings and no style, naming or
   complexity findings. A ``class:docs`` or ``class:tests`` PR label reads
   as the same signal.
3. **Sensitive depth**: a ``class:sensitive`` label is named in the Intent
   paragraph and forces full review depth even on a small diff.

An intent that contradicts a linked acceptance criterion produces one
high ``intent-mismatch`` finding naming the criterion.
"""

from __future__ import annotations

import re

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

INTENT_MISMATCH_RULE = "intent-mismatch"

# Display category for the mismatch finding. Named here (not in
# `code_review_prompt.RULES`) for the same reason as the linked-issue
# rules: the static prompt is pinned by the eval baseline, and a
# conditional rule must not tax every review. Dispatch maps it via
# `_category_for_rule`'s linked-issue fallback, so register it there.
RULE_CATEGORIES = {
    INTENT_MISMATCH_RULE: "requirements",
}

_LABEL_DOCS = "class:docs"
_LABEL_TESTS = "class:tests"
_LABEL_SENSITIVE = "class:sensitive"


def _normalize_labels(labels: tuple[str, ...] | list[str] | None) -> frozenset[str]:
    if not labels:
        return frozenset()
    return frozenset(
        str(label).strip().lower() for label in labels if str(label).strip()
    )


def labels_from_pr(pr: dict) -> tuple[str, ...]:
    """PR label names from a webhook ``pull_request`` payload.

    Tolerates both shapes GitHub sends: ``labels`` as a list of
    ``{"name": ...}`` dicts (webhook) or a bare list of strings (tests).
    """
    raw = (pr or {}).get("labels") or []
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name")
        else:
            name = entry
        if name:
            names.append(str(name))
    return tuple(names)


def is_docs_file(path: str) -> bool:
    """True for documentation paths: ``*.md``, ``*.txt``, or under ``docs/``."""
    lowered = (path or "").lower()
    if lowered.endswith((".md", ".txt")):
        return True
    parts = lowered.split("/")
    return "docs" in parts


def is_docs_or_tests_file(path: str) -> bool:
    """True for a docs file or a repo test path (see ``test_paths``)."""
    return is_docs_file(path) or is_test_file(path)


def is_docs_tests_only(
    changed_paths: tuple[str, ...] | list[str],
    labels: tuple[str, ...] | list[str] | None = None,
) -> bool:
    """Whether the review is docs/tests-scoped.

    A ``class:docs`` or ``class:tests`` label reads as the same signal as
    the paths themselves: when present, the review is scoped even if a
    stray non-docs path slipped into the diff listing.
    """
    normalized = _normalize_labels(tuple(labels or ()))
    if _LABEL_DOCS in normalized or _LABEL_TESTS in normalized:
        return True
    paths = tuple(changed_paths or ())
    return bool(paths) and all(is_docs_or_tests_file(p) for p in paths)


def is_sensitive(labels: tuple[str, ...] | list[str] | None) -> bool:
    """Whether the PR carries the ``class:sensitive`` label."""
    return _LABEL_SENSITIVE in _normalize_labels(tuple(labels or ()))


def should_force_full_depth(
    labels: tuple[str, ...] | list[str] | None,
    added_lines: int | None = None,
) -> bool:
    """True when a sensitive label forces full depth even on a small diff.

    ``added_lines`` is accepted (and ignored) so callers can pass the diff
    size they already computed without branching; the label alone decides.
    """
    _ = added_lines
    return is_sensitive(labels)


# Style/naming/complexity rules suppressed on docs/tests-only diffs.
# Anything not listed here (correctness, security, robustness, wrong-fact
# claims, unmet test-fidelity) still reports. Complexity rules are
# maintainability by construction; lint bug/style nits are style, while
# lint-security stays (a real secret shape in a fixture is still a leak
# the secret scanner would miss in prose).
_STYLE_RULE_NAMES = frozenset(
    {
        "dead-code",
        "complexity-hotspot",
        "high-complexity",
        "type-safety-gap",
        "missing-test-coverage",
        "lint-violation",
        "lint-async",
        "lint-bug",
    }
)

_STYLE_BUG_CLASSES = frozenset({"maintainability", "type design"})


def is_style_finding(rule_name: str, bug_class: str | None = None) -> bool:
    """True for style, naming, or complexity findings.

    ``bug_class`` (from the RULES table) lets callers classify without
    importing the prompt; unknown rules fall back to the name set.
    """
    if rule_name in _STYLE_RULE_NAMES:
        return True
    return bug_class is not None and bug_class in _STYLE_BUG_CLASSES


def filter_docs_tests_findings(findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
    """Drop style/naming/complexity findings, keep correctness ones.

    Pure: used when the diff is docs/tests-only. Wrong-fact findings
    (e.g. ``doc-code-claim-drift``), broken-link style correctness, and a
    test that cannot fail (``mock-vs-real-divergence``) all survive
    because none is in the style set.
    """
    return tuple(f for f in findings if not is_style_finding(f.rule_name))


_WORD_RE = re.compile(r"[a-z0-9]{4,}")
_NEGATION_RE = re.compile(
    r"(will\s+not|do\s+not|does\s+not|not\s+implement|out\s+of\s+scope"
    r"|skip(?:s|ped|ping)?|won't|can't|don't|n't\b|without\b|no\s+longer|never\b)",
    re.IGNORECASE,
)


def _intent_sentences(title: str, body: str) -> list[str]:
    blob = f"{title or ''} {body or ''}".strip()
    if not blob:
        return []
    parts = re.split(r"(?<=[.!?])\s+|\n+", blob)
    return [p.strip() for p in parts if p.strip()]


def detect_intent_mismatch(
    title: str,
    body: str,
    criteria_by_issue: dict[int, list[tuple[str, str]]],
    *,
    file_path: str = "",
    line: int = 1,
) -> Finding | None:
    """One high ``intent-mismatch`` finding when the stated intent plainly
    contradicts a linked acceptance criterion.

    Deterministic and conservative: a negated intent sentence
    ("will not ...", "out of scope", "skip ...") that shares a content
    word (4+ chars) with a criterion names that criterion. Anything
    subtler is the LLM's job via the linked-issue block, not this gate's.
    Pure; returns None when there is nothing to name.
    """
    if not criteria_by_issue:
        return None
    negated_words: set[str] = set()
    for sentence in _intent_sentences(title, body):
        if _NEGATION_RE.search(sentence):
            negated_words.update(_WORD_RE.findall(sentence.lower()))
    if not negated_words:
        return None
    for issue_number, criteria in criteria_by_issue.items():
        for index, (_state, text) in enumerate(criteria, start=1):
            criterion_words = set(_WORD_RE.findall((text or "").lower()))
            shared = sorted(negated_words & criterion_words)
            if shared:
                return Finding(
                    file=file_path or "PR",
                    line=max(1, line),
                    severity="high",
                    rule_name=INTENT_MISMATCH_RULE,
                    message=(
                        f"Intent contradicts linked criterion #{issue_number} "
                        f"criterion {index} ({shared[0]}): the change says it "
                        "will not do what the issue asks for."
                    ),
                    suggestion=None,
                )
    return None


def _first_sentence(text: str, limit: int = 220) -> str:
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    match = re.search(r"[.!?](\s|$)", flat)
    sentence = flat[: match.end()].strip() if match else flat
    if len(sentence) > limit:
        sentence = sentence[:limit].rstrip() + "."
    if not sentence.endswith((".", "!", "?")):
        sentence += "."
    return sentence


def build_intent_paragraph(
    title: str,
    body: str,
    criteria_by_issue: dict[int, list[tuple[str, str]]] | None = None,
    labels: tuple[str, ...] | list[str] | None = None,
    changed_paths: tuple[str, ...] | list[str] | None = None,
) -> str:
    """The Intent paragraph: at most three sentences.

    Sentence 1: what behavior changes (from the PR title). Sentence 2: for
    whom / scope (from the body, or the docs/tests-only scope when that
    applies). Sentence 3: whether it matches the linked issue's acceptance
    criteria (or that none are linked). A sensitive label is named here.
    Always plain text, no markdown heading (dispatch adds ``## Intent``).
    """
    criteria_by_issue = criteria_by_issue or {}
    title_sentence = _first_sentence(title) or "Change with no stated title."
    if changed_paths and all(is_docs_or_tests_file(p) for p in tuple(changed_paths)):
        scope_sentence = "For readers and tests: docs or test files only."
    else:
        body_sentence = _first_sentence(body)
        scope_sentence = body_sentence or "For the repository audience."
    total = sum(len(c) for c in criteria_by_issue.values())
    if total:
        match_sentence = (
            f"Matches {total} linked acceptance "
            f"criterion{'s' if total != 1 else ''} as stated."
        )
    else:
        match_sentence = "No linked acceptance criteria to match against."
    paragraph = f"{title_sentence} {scope_sentence} {match_sentence}"
    if is_sensitive(labels):
        paragraph += " Marked class:sensitive, full review depth applies."
        # Still at most three sentences: fold the scope into sentence one.
        paragraph = (
            f"{title_sentence} {match_sentence} "
            "Marked class:sensitive, full review depth applies."
        )
    sentences = [
        s.strip() for s in re.split(r"(?<=[.!?])\s+", paragraph.strip()) if s.strip()
    ]
    return " ".join(sentences[:3])


def intent_block(paragraph: str) -> str:
    """Render the paragraph as the summary's opening ``## Intent`` block."""
    if not paragraph:
        return ""
    return f"## Intent\n\n{paragraph.strip()}\n\n"


_BLOCK_CRITERION_RE = re.compile(r"^#(\d+) criterion (\d+) \[(open|ticked)\]: (.*)$")


def criteria_from_block(block: str) -> dict[int, list[tuple[str, str]]]:
    """Rebuild ``criteria_by_issue`` from a rendered linked-issue block.

    Dispatch already fetches and renders the block once; re-parsing it
    here avoids a second round of GitHub reads just to name the criterion
    an intent-mismatch finding refers to. Pure.
    """
    out: dict[int, list[tuple[str, str]]] = {}
    for raw in (block or "").splitlines():
        match = _BLOCK_CRITERION_RE.match(raw.strip())
        if not match:
            continue
        issue_number = int(match.group(1))
        state = match.group(3)
        text = match.group(4)
        out.setdefault(issue_number, []).append((state, text))
    return out


def hunks_are_docs_tests_only(
    hunks: tuple[DiffHunk, ...],
    labels: tuple[str, ...] | list[str] | None = None,
) -> bool:
    """Hunk-level form of :func:`is_docs_tests_only` for dispatch."""
    paths = tuple(dict.fromkeys(h.file_path for h in hunks))
    return is_docs_tests_only(paths, labels)
