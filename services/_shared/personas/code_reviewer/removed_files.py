"""Deletion-only review: a removed file that the rest of the repo still names (#1401).

`parse_diff` skips `+++ /dev/null` blocks, since a deleted file has no
new-side line to anchor a finding on. A pull request that only deletes
files therefore had no hunks, and Elder answered "eyes clouded (no_diff)"
on every pass. That neutral never satisfies a blocking gate, so every pure
revert stayed blocked. Observed on an infrastructure-repo revert that
deleted a workflow, its script and the script's tests: 255 lines removed,
none added.

A deletion-only pull request now gets a verdict from one deterministic
check: does any file at head still name a removed path? A workflow step
that runs a deleted script, or a config that loads a deleted file, breaks
on merge, and a deleted check that something still calls is the safety
layer the ticket asks Elder to catch. Each referrer is one finding on the
removed file: high when the referrer is code or config, medium when it is
only documentation. The file is gone, so findings carry
`outside_diff_reason` and appear in the check summary, never inline.

Discovery uses code search (the default-branch index, where the removed
file still exists), and every candidate is confirmed against its content
at the pull request's head, so a reference this PR also removed does not
count. The scan is pure; the fetch degrades to "no referrers" on any error.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping

import httpx

from personas.code_reviewer.diff_parser import file_changes
from personas.code_reviewer.persona import Finding

log = logging.getLogger(__name__)

RULE = "removed-but-referenced"
# Searches per review. Code search is GitHub's tightest rate bucket.
MAX_SEARCHED = 5
# Candidate referrer files fetched per review.
MAX_REFERRERS = 10
_SEARCH_TIMEOUT = 10.0
_DOC_RE = re.compile(r"\.(?:md|markdown|rst|txt|adoc)$|(?:^|/)docs?/", re.IGNORECASE)
_OUTSIDE = "the file is deleted, so no line of it is left in the diff to anchor on"


def removed_paths(unified_diff: str) -> tuple[str, ...]:
    """The paths a diff deletes, in diff order. Pure."""
    return tuple(c.path for c in file_changes(unified_diff) if c.status == "removed")


def search_term(path: str) -> str:
    """The parent directory and file name, so both a full path and a path
    relative to a nearby directory match. A top-level file is its name."""
    return "/".join(path.split("/")[-2:])


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
    caller confirms each against head content. Any error stops searching
    and returns what was found so far, logged."""
    paths: list[str] = []
    for path in removed[:MAX_SEARCHED]:
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
            items = (resp.json() or {}).get("items", [])
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError, AttributeError) as e:
            log.info(
                "code_review_removed_reference_search_degraded",
                extra={"repo": f"{owner}/{repo}", "kind": type(e).__name__},
            )
            break
        for item in items if isinstance(items, list) else ():
            found = item.get("path") if isinstance(item, dict) else None
            if isinstance(found, str) and found not in removed and found not in paths:
                paths.append(found)
    return tuple(paths[:MAX_REFERRERS])


def removed_reference_findings(
    removed: tuple[str, ...],
    search: Callable[[tuple[str, ...]], tuple[str, ...]],
    fetch_at_head: Callable[[tuple[str, ...]], Mapping[str, str]],
) -> tuple[Finding, ...]:
    """Search for referrers, read them at head, and scan. A failed search
    or fetch yields fewer candidates, never an exception."""
    if not removed:
        return ()
    candidates = search(removed)
    if not candidates:
        return ()
    return scan_removed_references(removed, fetch_at_head(candidates) or {})
