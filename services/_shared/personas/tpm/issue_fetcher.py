"""Chief's linked-issue fetcher - the ONE impure builder behind
`check_linked_issue_completeness` (#782).

`evaluate_pull_request` stays pure (spec 0002): the fetcher is a parameter
it receives, never a call it makes. Every caller that evaluates a PR for
real must pass a fetcher built HERE - the `pull_request` webhook dispatch
and the `/grug recheck` comment path both do. Pre-#782 only the webhook
path built one (inline, in `webhook_dispatch.py`); the recheck path called
`evaluate_pull_request(pr_body)` bare, so its `linked-issue-completeness`
check structurally hit the no-fetcher fail-open branch and a stale red
row could be turned green by a comment without the check ever running.
One builder, imported by both, is what keeps the two paths from
diverging again.

Imports stay inside the builder: the ACK path pays import cost only when
a persona actually dispatches, and the `github_app_auth` patch target
the tests intercept keeps working.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime

from personas.tpm.dor_checks import IssueFacts, IssueFactsFetcher, IssueFetcher
from personas.tpm.sibling_evidence import SiblingFetcher, SiblingPr

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.tpm.issue_fetcher")

# The timeline carries label churn too; three pages reach the references of
# any ticket seen so far, and the ones past them only cost evidence.
_MAX_TIMELINE_PAGES = 3
_MAX_SIBLINGS = 20


def build_issue_fetcher(
    *, installation_id: int, owner: str, repo: str,
) -> IssueFetcher:
    """Return a `number -> issue body` callable reading via the install
    token. Raises on any fetch failure (HTTP status, transport, auth) -
    `check_linked_issue_completeness` catches that and fails OPEN with the
    check marked `skipped`, so a GitHub blip never blocks merges and never
    masquerades as a pass either."""

    def _fetch_issue_body(number: int) -> str:
        from urllib.parse import quote
        import httpx  # type: ignore

        def _get(token: str) -> str:
            r = httpx.get(
                f"https://api.github.com/repos/{quote(owner, safe='')}/"
                f"{quote(repo, safe='')}/issues/{int(number)}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
            )
            r.raise_for_status()
            return (r.json() or {}).get("body") or ""

        from github_app_auth import with_install_token_retry  # type: ignore
        return with_install_token_retry(installation_id, _get)

    return _fetch_issue_body


def build_changed_files_fetcher(
    *, installation_id: int, owner: str, repo: str, pr_number: int,
) -> Callable[[], list[str]]:
    """Return a `() -> changed file paths` callable for the PR's diff.

    The in-scope check compares the PR's changed files against the linked
    issue's body (the scope oracle). Built as a no-arg callable capturing
    the PR coordinates, so `evaluate_pull_request` stays pure (the fetcher
    is a parameter, not a call inside it). Raises on fetch failure; the
    check fails OPEN with the check marked `skipped`."""

    def _fetch_changed_files() -> list[str]:
        from urllib.parse import quote
        import httpx  # type: ignore

        def _get(token: str) -> list[str]:
            r = httpx.get(
                f"https://api.github.com/repos/{quote(owner, safe='')}/"
                f"{quote(repo, safe='')}/pulls/{int(pr_number)}/files",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
            )
            r.raise_for_status()
            return [f["filename"] for f in (r.json() or [])]

        from github_app_auth import with_install_token_retry  # type: ignore
        return with_install_token_retry(installation_id, _get)

    return _fetch_changed_files


def build_issue_details_fetcher(
    *, installation_id: int, owner: str, repo: str,
) -> Callable[[int], dict[str, str]]:
    """Return a `number -> {title, body}` callable for review enrichment."""

    def _fetch_issue_details(number: int) -> dict[str, str]:
        from urllib.parse import quote
        import httpx  # type: ignore

        def _get(token: str) -> dict[str, str]:
            response = httpx.get(
                f"https://api.github.com/repos/{quote(owner, safe='')}/"
                f"{quote(repo, safe='')}/issues/{int(number)}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
            )
            response.raise_for_status()
            issue = response.json() or {}
            return {"title": issue.get("title") or "", "body": issue.get("body") or ""}

        from github_app_auth import with_install_token_retry  # type: ignore
        return with_install_token_retry(installation_id, _get)

    return _fetch_issue_details


def _instant(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def merged_siblings(
    token: str, owner: str, repo: str, number: int, *, pr_number: int = 0,
) -> tuple[SiblingPr, ...]:
    """The merged pull requests of this repository that link issue `number`,
    other than `pr_number`, whose body is unedited since the merge (grug#1530).
    A cross-reference alone is a mention, so that body must link the issue the
    way a review reads a link. One GitHub read per timeline page, one more for
    the bodies, and no model call. Raises on a failed read."""
    from urllib.parse import quote
    import httpx  # type: ignore

    from personas.code_reviewer.linked_issue import linked_issue_numbers

    full_name = f"{owner}/{repo}".casefold()
    url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}/issues/{int(number)}/timeline"
    )
    candidates: set[int] = set()
    for page in range(1, _MAX_TIMELINE_PAGES + 1):
        r = httpx.get(
            url,
            params={"per_page": 100, "page": page},
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        r.raise_for_status()
        events = r.json() or []
        for event in events:
            if event.get("event") != "cross-referenced":
                continue
            source = (event.get("source") or {}).get("issue") or {}
            home = str(source.get("repository_url") or "").casefold()
            if (
                (source.get("pull_request") or {}).get("merged_at")
                and source.get("number") != pr_number
                and home.endswith(f"/repos/{full_name}")
            ):
                candidates.add(int(source["number"]))
        if len(events) < 100:
            break
    numbers = sorted(candidates)[-_MAX_SIBLINGS:]
    if not numbers:
        return ()
    return tuple(
        pr for pr in _unedited_merged_prs(token, owner, repo, numbers)
        if int(number) in linked_issue_numbers(pr.body, owner=owner, repo=repo, limit=None)
    )


def _unedited_merged_prs(
    token: str, owner: str, repo: str, numbers: list[int],
) -> tuple[SiblingPr, ...]:
    """The PRs whose body has not been edited since they merged, each with
    that body. Anyone who can edit a merged PR could otherwise write a gate's
    evidence into it, so the merged body is the only one that counts. The body
    comes from the same read as the edit time, and a title is never evidence
    because `lastEditedAt` does not cover it. One GraphQL read."""
    import httpx  # type: ignore

    fields = "".join(
        f"pr{int(n)}: pullRequest(number: {int(n)}) {{ number mergedAt lastEditedAt body }} "
        for n in numbers
    )
    r = httpx.post(
        "https://api.github.com/graphql",
        json={
            "query": (
                "query($owner: String!, $name: String!) "
                f"{{ repository(owner: $owner, name: $name) {{ {fields}}} }}"
            ),
            "variables": {"owner": owner, "name": repo},
        },
        headers={"Authorization": f"bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    payload = r.json() or {}
    if payload.get("errors"):
        raise RuntimeError("sibling pull request read returned errors")
    found: list[SiblingPr] = []
    for pr in ((payload.get("data") or {}).get("repository") or {}).values():
        merged, edited = (pr or {}).get("mergedAt"), (pr or {}).get("lastEditedAt")
        if not merged:
            continue
        if edited and _instant(edited) > _instant(merged):
            log.info("sibling_body_edited_after_merge", extra={"pr": pr.get("number")})
            continue
        found.append(SiblingPr(int(pr["number"]), pr.get("body") or ""))
    return tuple(sorted(found, key=lambda p: p.number))


def build_merged_siblings_fetcher(
    *, installation_id: int, owner: str, repo: str, pr_number: int,
) -> SiblingFetcher:
    """Return an `issue number -> merged sibling PRs` callable. Failures
    raise; a caller then has no sibling evidence, which is the plain check."""

    def _fetch(number: int) -> tuple[SiblingPr, ...]:
        from github_app_auth import with_install_token_retry  # type: ignore
        return with_install_token_retry(
            installation_id,
            lambda token: merged_siblings(token, owner, repo, number, pr_number=pr_number),
        )

    return _fetch


def issue_facts(token: str, owner: str, repo: str, number: int) -> IssueFacts:
    """One issue's epic facts, read with a token already in hand (grug#1034,
    #1035). Two reads: the issue, and its native parent, where a 404 means "no
    parent" rather than a failure. Anything else raises; every caller treats
    that as fail-open."""
    from urllib.parse import quote
    import httpx  # type: ignore

    base = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}/issues/{int(number)}"
    )
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    r = httpx.get(base, headers=headers, timeout=10)
    r.raise_for_status()
    issue = r.json() or {}
    p = httpx.get(f"{base}/parent", headers=headers, timeout=10)
    if p.status_code == 404:
        parent = None
    else:
        p.raise_for_status()
        parent = (p.json() or {}).get("number")
    return IssueFacts(
        number=int(number),
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        labels=tuple(
            (label.get("name") or "") if isinstance(label, dict) else str(label)
            for label in issue.get("labels") or []
        ),
        sub_issue_count=int((issue.get("sub_issues_summary") or {}).get("total") or 0),
        parent_number=parent,
        is_pull_request=bool(issue.get("pull_request")),
    )


def build_issue_facts_fetcher(
    *, installation_id: int, owner: str, repo: str,
) -> IssueFactsFetcher:
    """Return a `number -> IssueFacts` callable for the `which epic` check
    (grug#1034), acquiring the install token per call. Failures raise, and
    `check_linked_issue_in_epic` fails OPEN with the check marked `skipped`.
    """

    def _fetch(number: int) -> IssueFacts:
        from github_app_auth import with_install_token_retry  # type: ignore
        return with_install_token_retry(
            installation_id, lambda token: issue_facts(token, owner, repo, number),
        )

    return _fetch
