"""In-scope check: does the PR's diff stay inside the linked issue's scope?

The linked issue body is the scope oracle. A changed file is "in scope"
when its path tokens overlap the issue body's tokens; anything else is an
out-of-scope rider the issue never asked for. Advisory by default: a
failure warns and names the rider files, never blocks; org/repo config can
promote it to blocking via `advisory_checks` at the publish layer
(see `personas.tpm.persona`).

Activates only when the PR body links an issue (closing keyword). No link
-> pass, gracefully. Fetch failure -> fail-open, marked `skipped` (#782).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from personas.tpm.ticket_compliance import strip_code_spans


log = logging.getLogger("grug.persona.tpm.in_scope")

# Lowercase word tokens from free text or a file path (len >= 3 to drop
# noise like "a", "of", "it"). Mirrors ticket_compliance's heuristic so a
# changed file reads as "in scope" when its path vocabulary appears in the
# linked issue body (the scope oracle), and as an out-of-scope rider
# otherwise.
_TEXT_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Closing keywords: close(s|d)?, fix(es|ed)?, resolve(s|d)? followed by #N.
# Case-insensitive. Same pattern the DoR `linked-issue-completeness` check
# uses, so "the linked issue" is one stable concept across Chief's rules.
# Captures the issue number. Same-repo only (Closes owner/repo#N is out of
# scope per #564).
_CLOSING_KEYWORD_PAT = re.compile(
    r"\b(?:close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?)\s+#(\d+)",
    re.IGNORECASE,
)


def _text_tokens(text: str) -> set[str]:
    """Lowercase word tokens from free text (len >= 3 to drop noise)."""
    raw = (text or "").lower()
    raw = raw.replace("/", " ").replace("_", " ").replace("-", " ").replace(".", " ")
    return {t for t in _TEXT_TOKEN_RE.findall(raw) if len(t) >= 3}


def _linked_issue_numbers(body: str) -> list[int]:
    """Issue numbers the PR body links via a closing keyword
    (`Closes`/`Fixes`/`Resolves #N`), order-preserving and deduped.

    Same extraction the DoR `linked-issue-completeness` check uses, so "the
    linked issue" is one stable concept across Chief's rules. A `Refs #N` /
    `Part of #N` link names related work, not the issue this PR is scoped to,
    so it does not activate the in-scope check."""
    seen: dict[int, None] = {}
    for n in _CLOSING_KEYWORD_PAT.findall(strip_code_spans(body or "")):
        seen.setdefault(int(n), None)
    return list(seen)


def check_in_scope(
    body: str,
    *,
    fetch_issue: IssueFetcher | None = None,
    fetch_changed_files: Callable[[], list[str]] | None = None,
) -> CheckResult:
    """Is every file the PR touches inside the linked issue's scope?

    The linked issue body is the scope oracle. A changed file is "in scope"
    when its path tokens overlap the issue body's tokens; anything else is an
    out-of-scope rider the issue never asked for.

    Activates only when the PR body links an issue (closing keyword). No link
    -> pass, gracefully (mirrors `issue-link`). Fetch failure -> fail-open,
    marked `skipped` (#782): a GitHub blip never freezes merges and never
    reads as an earned pass. Advisory by default: a failure warns and names
    the rider files, never blocks; org/repo config can promote it to blocking
    via `advisory_checks` at the publish layer (see `personas.tpm.persona`).
    """
    # Imported lazily to avoid a circular import: dor_checks imports
    # check_in_scope from this module, so this module must not import
    # CheckResult from dor_checks at module level.
    from personas.tpm.dor_checks import CheckResult  # noqa: PLC0415

    numbers = _linked_issue_numbers(body)
    if not numbers:
        return CheckResult(
            "in-scope", True,
            "no linked issue in body; in-scope not applicable",
        )
    if fetch_issue is None or fetch_changed_files is None:
        return CheckResult(
            "in-scope", True,
            f"linked issue(s) #{numbers} but a fetcher is missing (fail-open)",
            skipped=True,
        )

    issue_tokens: set[str] = set()
    fetch_failures: list[int] = []
    for num in numbers:
        try:
            issue_tokens |= _text_tokens(fetch_issue(num) or "")
        except Exception:  # noqa: BLE001 - a blip must not freeze merges
            log.warning(
                "in_scope_issue_fetch_failed",
                extra={"issue_number": num},
            )
            fetch_failures.append(num)
    # Nothing fetched at all: we have no scope oracle to compare against, so
    # fail open (skipped) rather than comparing changed files to an empty
    # token set and calling everything out of scope.
    if fetch_failures and not issue_tokens:
        return CheckResult(
            "in-scope", True,
            f"linked issue(s) #{numbers}; issue fetch failed (fail-open)",
            skipped=True,
        )

    try:
        changed = fetch_changed_files() or []
    except Exception:  # noqa: BLE001 - same fail-open posture for the diff
        log.warning(
            "in_scope_changed_files_fetch_failed",
        )
        return CheckResult(
            "in-scope", True,
            f"linked issue(s) #{numbers}; changed-files fetch failed (fail-open)",
            skipped=True,
        )

    out_of_scope = [p for p in changed if not (_text_tokens(p) & issue_tokens)]
    if out_of_scope:
        named = ", ".join(out_of_scope)
        detail = (
            f"out of scope vs linked issue(s) #{numbers}: {named} - "
            "these files are not covered by the linked issue's scope; "
            "split to a separate PR or link an issue that covers them"
        )
        return CheckResult("in-scope", False, detail)

    count = len(changed)
    tail = f"; fetch failed for #{fetch_failures}" if fetch_failures else ""
    return CheckResult(
        "in-scope", True,
        f"all {count} changed file(s) within linked issue scope{tail}",
    )
