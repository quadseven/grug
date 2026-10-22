"""The `grug:reviewing` label - the visible sign that Elder is re-reviewing a PR.

grug#1270. Before this, nothing on a PR said "Elder is already looking at
this", so people and agents fired `/grug improve` again while a review was
already in flight. On 2026-10-18 a burst of 14 degraded PRs each got one
(or two) `/grug improve` within about two minutes, which spent the
installation's whole hourly GitHub budget: the commands that followed were
dropped with no reply and no retry, so they LOOKED like they worked and did
nothing.

The three rules this module keeps:

  - The Elder CHECK RUN is the source of truth for "a review is running".
    The label is only the visible sign of it.
  - Every write has a removal. The consumer removes the label once the
    Elder check on that head reached a terminal conclusion, and the sweep
    removes labels whose head has no in-progress Elder check (a crashed
    run, a moved head). One missed removal can therefore never block a
    future run - the label is a sign, never a lock.
  - Nothing in here raises. A label write that fails is a missing sign, not
    a failed review.

Webhook-only (NOT mirrored): the api service never starts an Elder re-run
of its own accord, so the label's lifecycle lives with the consumer that
runs the review.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx

from github_app_auth import with_install_token_retry
from github_checks_client import list_check_runs_for_ref
from personas.tribe import CHECK_ELDER, acceptable_check_names

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.review_label")

# The label every operator (and every agent reading the PR page) can see.
REVIEWING_LABEL = "grug:reviewing"

# GitHub's own palette: yellow reads as "happening now".
_LABEL_COLOR = "fbca04"
_LABEL_DESCRIPTION = "Grug's Elder is reviewing this PR right now."

_GH_API = "https://api.github.com"
_TIMEOUT = 10.0
# A repo can carry more than 100 labeled PRs only if a hundred of them were
# labeled at once; the cap is a runaway backstop, not a real ceiling (the
# same convention as the pagination caps elsewhere).
_LABEL_MAX_PAGES = 10

# Every check name that counts as "the Elder check" - the current title plus
# its legacy aliases, so a run posted before the rename still answers the
# question.
_ELDER_CHECK_NAMES = frozenset(acceptable_check_names(CHECK_ELDER))


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo_path(owner: str, repo: str) -> str:
    return f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


# --- the label itself ------------------------------------------------------


def ensure_repo_label(token: str, owner: str, repo: str) -> bool:
    """Create the `grug:reviewing` label in this repo if it is missing.

    GitHub rejects an unknown label name on an issue (422), so a repo that
    has never been reviewed by grug would fail every label write. Creating
    it on demand means a stranger's repo needs no manual setup. A 422 here
    means somebody else created it first - a race, not a failure."""
    name = quote(REVIEWING_LABEL, safe="")
    resp = httpx.get(
        f"{_repo_path(owner, repo)}/labels/{name}",
        headers=_headers(token),
        timeout=_TIMEOUT,
    )
    if resp.status_code == 200:
        return True
    if resp.status_code != 404:
        return False
    created = httpx.post(
        f"{_repo_path(owner, repo)}/labels",
        json={
            "name": REVIEWING_LABEL,
            "color": _LABEL_COLOR,
            "description": _LABEL_DESCRIPTION,
        },
        headers=_headers(token),
        timeout=_TIMEOUT,
    )
    return created.status_code in (201, 422)


def add_reviewing_label(*, token: str, owner: str, repo: str, pr_number: int) -> bool:
    """Put the `grug:reviewing` label on the PR.

    True when the PR carries the label afterwards. Never raises: the caller
    treats a missing sign as a logging event, not a failed command."""
    try:
        if _attach_label(token, owner, repo, pr_number):
            return True
        # 422 = the repo has no such label yet. Create it, then attach it.
        if not ensure_repo_label(token, owner, repo):
            return False
        return _attach_label(token, owner, repo, pr_number)
    except Exception as error:  # noqa: BLE001 - a sign is never worth an exception
        log.warning(
            "reviewing_label_add_failed",
            extra={
                "repo": f"{owner}/{repo}",
                "pr": pr_number,
                "kind": type(error).__name__,
            },
        )
        return False


def _attach_label(token: str, owner: str, repo: str, pr_number: int) -> bool:
    """One attach call. False means "not attached" - the caller decides
    whether that is because the label does not exist yet (422) or because
    the write was refused."""
    resp = httpx.post(
        f"{_repo_path(owner, repo)}/issues/{int(pr_number)}/labels",
        json={"labels": [REVIEWING_LABEL]},
        headers=_headers(token),
        timeout=_TIMEOUT,
    )
    return resp.status_code in (200, 201)


def remove_reviewing_label(*, token: str, owner: str, repo: str, pr_number: int) -> bool:
    """Take the `grug:reviewing` label off the PR.

    A label that is not there counts as removed (404): the goal is "the PR
    does not carry it", not "the DELETE call succeeded". Never raises."""
    try:
        resp = httpx.delete(
            f"{_repo_path(owner, repo)}/issues/{int(pr_number)}"
            f"/labels/{quote(REVIEWING_LABEL, safe='')}",
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        return resp.status_code in (200, 204, 404)
    except Exception as error:  # noqa: BLE001 - a sign is never worth an exception
        log.warning(
            "reviewing_label_remove_failed",
            extra={
                "repo": f"{owner}/{repo}",
                "pr": pr_number,
                "kind": type(error).__name__,
            },
        )
        return False


# --- the Elder check on a head: the source of truth ------------------------


def elder_check_state(*, token: str, owner: str, repo: str, head_sha: str) -> str:
    """The state of the Elder check-run on this head.

    - `running`   - a queued or in-progress Elder run exists: a review is
                    in flight right now.
    - `concluded` - a completed Elder run exists: the review finished (pass,
                    fail, or a degraded neutral - all terminal).
    - `absent`    - no Elder run on this head at all.
    - `unknown`   - GitHub could not be read. Never guessed away: an
                    unreadable state must not be read as "no review is
                    running", or the sweep would strip a live label.

    A run that is in progress wins over a completed one: both names (the
    current title and a legacy alias) can carry a run at the same time, and
    "something is still running" is the answer that keeps the sign."""
    if not head_sha:
        return "absent"
    try:
        runs = list_check_runs_for_ref(token, owner, repo, head_sha)
    except Exception as error:  # noqa: BLE001 - unreadable is its own answer
        log.warning(
            "elder_check_state_unreadable",
            extra={
                "repo": f"{owner}/{repo}",
                "head_sha": head_sha[:8],
                "kind": type(error).__name__,
            },
        )
        return "unknown"
    return _elder_state_from_runs(runs)


def _elder_state_from_runs(runs: list[dict]) -> str:
    """`elder_check_state`'s pure core: the same answer from a listing."""
    concluded = False
    for run in runs:
        if str(run.get("name") or "") not in _ELDER_CHECK_NAMES:
            continue
        status = str(run.get("status") or "")
        if status in {"queued", "in_progress"}:
            return "running"
        if status == "completed":
            concluded = True
    return "concluded" if concluded else "absent"


def elder_check_conclusion(*, token: str, owner: str, repo: str, head_sha: str) -> str:
    """The conclusion of the Elder check-run on this head ("success",
    "failure", "neutral", ...), or "" while it is still running, absent, or
    unreadable. The conclusion is what the label's removal log line
    reports - success, failure and a degraded neutral all end the review."""
    if not head_sha:
        return ""
    try:
        runs = list_check_runs_for_ref(token, owner, repo, head_sha)
    except Exception as error:  # noqa: BLE001 - unreadable is its own answer
        log.warning(
            "elder_check_conclusion_unreadable",
            extra={
                "repo": f"{owner}/{repo}",
                "head_sha": head_sha[:8],
                "kind": type(error).__name__,
            },
        )
        return ""
    for run in runs:
        if str(run.get("name") or "") not in _ELDER_CHECK_NAMES:
            continue
        if str(run.get("status") or "") == "completed":
            return str(run.get("conclusion") or "")
    return ""


# --- the sweep: no label may outlive its review ----------------------------


def _labeled_prs_for_repo(token: str, owner: str, repo: str) -> list[int]:
    """The numbers of the PRs in this repo that carry the label.

    The per-repo `labels` filter takes the label NAME - not a search
    qualifier - so a label whose name contains a colon needs no escaping
    trick and no query-parser bet. `state=all` so a PR that was merged or
    closed with the label still on it gets cleaned."""
    numbers: list[int] = []
    for page in range(1, _LABEL_MAX_PAGES + 1):
        resp = httpx.get(
            f"{_repo_path(owner, repo)}/issues",
            params={
                "labels": REVIEWING_LABEL,
                "state": "all",
                "per_page": 100,
                "page": page,
            },
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        for item in batch:
            number = item.get("number")
            if number:
                numbers.append(int(number))
        if len(batch) < 100:
            break
    return numbers


def _pr_head_sha(token: str, owner: str, repo: str, pr_number: int) -> str:
    """The PR's current head SHA ("" when it cannot be read)."""
    resp = httpx.get(
        f"{_repo_path(owner, repo)}/pulls/{int(pr_number)}",
        headers=_headers(token),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return str(((resp.json() or {}).get("head") or {}).get("sha") or "")


def sweep_repo_labels(
    *, token: str, owner: str, repo: str, pr_numbers: list[int],
) -> int:
    """Remove the label from every one of these PRs whose head has no
    in-progress Elder check. Returns how many labels were removed."""
    removed = 0
    for pr_number in pr_numbers:
        try:
            head_sha = _pr_head_sha(token, owner, repo, pr_number)
            state = elder_check_state(
                token=token, owner=owner, repo=repo, head_sha=head_sha,
            )
            # `running` keeps the sign (a review IS in flight); `unknown`
            # keeps it too (GitHub would not say, so we do not guess).
            if state not in {"concluded", "absent"}:
                continue
            if remove_reviewing_label(
                token=token, owner=owner, repo=repo, pr_number=pr_number,
            ):
                removed += 1
                log.info(
                    "reviewing_label_swept",
                    extra={
                        "repo": f"{owner}/{repo}",
                        "pr": pr_number,
                        "head_sha": head_sha[:8],
                        "state": state,
                    },
                )
        except Exception as error:  # noqa: BLE001 - one PR must not abort the sweep
            log.warning(
                "reviewing_label_sweep_pr_failed",
                extra={
                    "repo": f"{owner}/{repo}",
                    "pr": pr_number,
                    "kind": type(error).__name__,
                },
            )
    return removed


def sweep_stale_labels(*, install_id: int) -> int:
    """Remove every `grug:reviewing` label whose head has no in-progress
    Elder check.

    The backstop for the removal paths that can be missed: a review that
    crashed mid-flight, a head that moved, a label written by a version
    that died before it could remove it. Without it one stale label would
    tell every later reader that Elder is busy, and the command path would
    refuse to re-run a review that is long finished.

    The installation's repos are the ground truth for "repos grug acts on"
    (`list_installation_repos`), scanned one call per repo - the same
    per-repo shape the enforcement pass already uses. Best-effort per repo
    and per PR; never raises."""
    from github_rulesets_client import list_installation_repos

    def _do(token: str) -> int:
        removed = 0
        for repo in list_installation_repos(token):
            full = str(repo.get("full_name") or "")
            owner, _, name = full.partition("/")
            if not owner or not name:
                continue
            removed += sweep_repo_labels(
                token=token, owner=owner, repo=name,
                pr_numbers=_labeled_prs_for_repo(token, owner, name),
            )
        return removed

    try:
        return int(with_install_token_retry(install_id, _do) or 0)
    except Exception as error:  # noqa: BLE001 - one installation must not abort the pass
        log.warning(
            "reviewing_label_sweep_failed",
            extra={"install_id": install_id, "kind": type(error).__name__},
        )
        return 0
