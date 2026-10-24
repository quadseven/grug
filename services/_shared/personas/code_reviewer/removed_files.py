"""Deletion-only review: what a pull request that only deletes files takes away (#1401).

`parse_diff` skips `+++ /dev/null` blocks, since a deleted file has no
new-side line to anchor a finding on. A pull request that only deletes
files therefore had no hunks, and Elder answered "eyes clouded (no_diff)"
on every pass. That neutral never satisfies a blocking gate, so every pure
revert stayed blocked. Observed on an infrastructure-repo revert that
deleted a workflow, its script and the script's tests: 255 lines removed,
none added.

A deletion-only pull request now gets a verdict from two deterministic
checks. Neither one can pass a deletion it could not finish checking.

- `removed-guarded-path` (high, always): the deletion removes a guard by
  its path alone: a CI workflow, CODEOWNERS, branch protection or ruleset
  config, anything under a security, policy or leak-scan directory, or a
  test file whose covered code stays. A test counts as covered only when
  the same PR deletes a code file of the same module name in the test's
  directory or the one above it, never a doc or a root-level file. There
  is no ticket downgrade: a linked issue can be written by the PR's own
  author, so a person or session answers the finding to merge. Same
  layer list as the weakened-verification check (#1367, #1381).
- `removed-but-referenced`: a file at head still names a removed path (in
  full, or relative to its own directory). High for code or config, medium
  for docs. Discovery uses code search (the default-branch index, where the
  removed file still exists); every candidate is confirmed against its
  content at the PR head, so a reference this PR also removed does not
  count. A search or fetch that fails, or a result past the caps, raises
  `RemovedReferenceCheckIncomplete`; dispatch then publishes the degraded
  reason `REMOVED_REFERENCE_CHECK_FAILED`, never a pass.

The file is gone, so findings carry `outside_diff_reason` and appear in the
check summary, never inline. The scans are pure; only the search is IO.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping

import httpx

from personas.code_reviewer.diff_parser import file_changes
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file
from personas.code_reviewer.weakened_verification import _CI_PATH_RE

log = logging.getLogger(__name__)

RULE = "removed-but-referenced"
RULE_GUARDED = "removed-guarded-path"
REMOVED_REFERENCE_CHECK_FAILED = "removed_reference_check_failed"
# Searches per review. Code search is GitHub's tightest rate bucket; a
# deletion past this many files is not fully checked, so it degrades.
MAX_SEARCHED = 5
# Candidate referrer files fetched per review; more degrades the same way.
MAX_REFERRERS = 10
_SEARCH_TIMEOUT = 10.0
_DOC_RE = re.compile(r"\.(?:md|markdown|rst|txt|adoc)$|(?:^|/)docs?/", re.IGNORECASE)
_OUTSIDE = "the file is deleted, so no line of it is left in the diff to anchor on"

_CODEOWNERS_RE = re.compile(r"(?:^|/)CODEOWNERS$")
_RULESET_RE = re.compile(
    r"(?:^|/)(?:rulesets?|branch[-_]protections?)(?:/|[-_.][^/]*$)", re.IGNORECASE,
)
_SECURITY_DIR_RE = re.compile(
    r"(?:^|/)(?:security|policy|policies|leak[-_]?scan\w*|leak[-_]?guard\w*)/",
    re.IGNORECASE,
)


class RemovedReferenceCheckIncomplete(Exception):
    """The referrer check could not finish, so it cannot vouch for the deletion."""


def removed_paths(unified_diff: str) -> tuple[str, ...]:
    """The paths a diff deletes, in diff order. Pure."""
    return tuple(c.path for c in file_changes(unified_diff) if c.status == "removed")


def search_term(path: str) -> str:
    """The parent directory and file name, so both a full path and a path
    relative to a nearby directory are discovered. A top-level file is its
    name."""
    return "/".join(path.split("/")[-2:])


def _guard_kind(path: str, removed: frozenset[str]) -> str | None:
    """What guard deleting `path` removes, or None for an ordinary file."""
    if _CI_PATH_RE.search(path):
        return "a CI workflow"
    if _CODEOWNERS_RE.search(path):
        return "the code ownership rules"
    if _RULESET_RE.search(path):
        return "branch protection or ruleset config"
    if _SECURITY_DIR_RE.search(path):
        return "a security, policy or leak-scan layer"
    if is_test_file(path) and not _covered_code_goes_too(path, removed):
        return "test coverage of code this PR keeps"
    return None


_CODE_EXT_RE = re.compile(
    r"\.(?:py|sh|bash|js|jsx|mjs|ts|tsx|go|rs|rb|java|kt|swift|c|cc|cpp|h|hpp"
    r"|cs|php|scala|ex|exs)$"
)
_TEST_AFFIX_RE = re.compile(r"^test_|_test$|[._-](?:test|spec)$")


def _module_stem(path: str) -> str:
    """The module name a file is about: `test_check.py`, `check_test.go`
    and `check.test.ts` are all `check`."""
    base = path.rsplit("/", 1)[-1]
    name = base.rsplit(".", 1)[0] if "." in base else base
    return _TEST_AFFIX_RE.sub("", name.lower())


# Never covered code: package markers and docs.
_NOT_CODE_RE = re.compile(r"(?:^|/)(?:__init__\.py|README[^/]*|[^/]*\.md)$", re.IGNORECASE)


def _covered_code_goes_too(test: str, removed: frozenset[str]) -> bool:
    """The tests go with the code they covered: the PR also deletes a code
    file (by extension; docs never count) that is not at the repository
    root, sits in the test's directory or the one above it, and has the
    test's module name. Package markers and docs never count. When in
    doubt this answers False, so the finding is raised."""
    parts = test.split("/")[:-1]
    homes = {"/".join(parts[:depth]) for depth in (len(parts) - 1, len(parts)) if depth >= 1}
    stem = _module_stem(test)
    return bool(stem) and any(
        "/" in other
        and not is_test_file(other)
        and _NOT_CODE_RE.search(other) is None
        and _CODE_EXT_RE.search(other) is not None
        and other.rsplit("/", 1)[0] in homes
        and _module_stem(other) == stem
        for other in removed
    )


def scan_guarded_deletions(removed: tuple[str, ...]) -> tuple[Finding, ...]:
    """One high finding per deleted guard path. Pure."""
    gone = frozenset(removed)
    found: list[Finding] = []
    for path in removed:
        kind = _guard_kind(path, gone)
        if kind is None:
            continue
        found.append(Finding(
            file=path,
            line=1,
            severity="high",
            rule_name=RULE_GUARDED,
            message=(
                f"This PR deletes `{path}`, which removes {kind}. A guard that "
                "is gone passes everything. Keep the file, or have a person "
                "confirm the removal is intended before this merges."
            ),
            suggestion=None,
            effort="quick-win",
            outside_diff_reason=_OUTSIDE,
        ))
    return tuple(found)


def _pattern(path: str, referrer: str) -> re.Pattern[str]:
    """The removed path as the referrer can write it: in full, or relative
    to a directory the referrer sits in. Never inside a longer name, so
    `check.sh` is not `check.sh.bak`, and a same-named file in another
    directory is not this one."""
    forms = {path}
    parts = referrer.split("/")[:-1]
    for depth in range(1, len(parts) + 1):
        base = "/".join(parts[:depth]) + "/"
        if path.startswith(base):
            forms.add(path[len(base):])
    alternatives = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    return re.compile(rf"(?<![\w.-])(?<![\w-]/)(?:{alternatives})(?![\w-]|\.\w)")


def scan_removed_references(
    removed: tuple[str, ...], contents: Mapping[str, str],
) -> tuple[Finding, ...]:
    """One finding per (removed file, referrer) where the referrer's head
    text still names the removed path. Removed files never count as
    referrers. Pure."""
    gone = set(removed)
    found: list[Finding] = []
    for path in removed:
        for referrer, text in sorted(contents.items()):
            if referrer in gone:
                continue
            pattern = _pattern(path, referrer)
            line = next(
                (n for n, row in enumerate(text.splitlines(), 1) if pattern.search(row)),
                None,
            )
            if line is None:
                continue
            doc = _DOC_RE.search(referrer) is not None
            found.append(Finding(
                file=path,
                line=1,
                severity="medium" if doc else "high",
                rule_name=RULE,
                message=(
                    f"This PR deletes `{path}`, but `{referrer}:{line}` still "
                    "names it. "
                    + (
                        "Update or drop the stale reference in the docs."
                        if doc else
                        "That caller breaks on merge, or a check it runs "
                        "silently goes away. Remove the reference in this PR "
                        "or keep the file."
                    )
                ),
                suggestion=None,
                effort="quick-win",
                outside_diff_reason=_OUTSIDE,
            ))
    return tuple(found)


def find_referrer_candidates(
    install_token: str, owner: str, repo: str, removed: tuple[str, ...],
) -> tuple[str, ...]:
    """Paths code search says mention a removed file. Discovery only; the
    caller confirms each against head content. Raises
    `RemovedReferenceCheckIncomplete` when any search fails or a cap cuts
    the answer short."""
    if len(removed) > MAX_SEARCHED:
        raise RemovedReferenceCheckIncomplete(
            f"{len(removed)} deleted files, over the {MAX_SEARCHED}-search cap"
        )
    paths: list[str] = []
    for path in removed:
        try:
            resp = httpx.get(
                "https://api.github.com/search/code",
                params={
                    "q": f'"{search_term(path)}" repo:{owner}/{repo}',
                    "per_page": MAX_REFERRERS,
                },
                headers={
                    "Authorization": f"Bearer {install_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=_SEARCH_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            raise RemovedReferenceCheckIncomplete(f"search failed: {type(e).__name__}") from e
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or body.get("incomplete_results"):
            raise RemovedReferenceCheckIncomplete("search answer incomplete")
        total = body.get("total_count")
        if isinstance(total, int) and total > len(items):
            raise RemovedReferenceCheckIncomplete(f"{total} search hits, over the page")
        for item in items:
            found = item.get("path") if isinstance(item, dict) else None
            if isinstance(found, str) and found not in removed and found not in paths:
                paths.append(found)
    if len(paths) > MAX_REFERRERS:
        raise RemovedReferenceCheckIncomplete(
            f"{len(paths)} candidate referrers, over the {MAX_REFERRERS}-file cap"
        )
    return tuple(paths)


def removed_reference_findings(
    removed: tuple[str, ...],
    search: Callable[[tuple[str, ...]], tuple[str, ...]],
    fetch_at_head: Callable[[tuple[str, ...]], Mapping[str, str]],
) -> tuple[Finding, ...]:
    """Search for referrers, read every one at head, and scan. A candidate
    that cannot be read at head raises `RemovedReferenceCheckIncomplete`,
    since its content is the evidence."""
    if not removed:
        return ()
    candidates = search(removed)
    if not candidates:
        return ()
    contents = fetch_at_head(candidates) or {}
    unread = [c for c in candidates if c not in contents]
    if unread:
        raise RemovedReferenceCheckIncomplete(f"{len(unread)} referrer(s) unreadable at head")
    return scan_removed_references(removed, contents)
