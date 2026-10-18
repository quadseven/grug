"""Possibly-related prior PRs for the Teller walkthrough (#675).

Finds up to 3 merged PRs in the same repo that touched the same files or
shipped related intent, so the walkthrough can show the PR as a thread of
work instead of an island.

Bounded by design (the issue's latency AC): one bounded list call for
recent closed PRs, then file fetches for at most 5 title-similar
candidates. Never a list-all scan. All of those calls share one
aggregate budget (_BUDGET): each call's timeout is capped at what is
left of it, and once it is spent the remaining candidates are scored on
title alone with no further calls. Best-effort: any failure yields [],
and the walkthrough renders without the section.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

_API = "https://api.github.com"
# Same logger as the walkthrough dispatcher, so a failed lookup shows up
# next to the walkthrough it belongs to.
log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.walkthrough")
_TIMEOUT = 10.0
# One wall-clock budget across the whole retrieval (list + file fetches),
# so six calls cannot each take a full _TIMEOUT (#675 AC3). Each call's
# timeout is capped at what is left of it.
_BUDGET = 12.0
# Bounds for the whole retrieval: one list call + at most this many /files.
_LIST_PER_PAGE = 20
_MAX_FILE_FETCHES = 5
_MAX_RELATED = 3
# A candidate must clear this combined score to be shown (honest empty -
# no filler when nothing is really related).
_MIN_SCORE = 2.0


@dataclass(frozen=True)
class RelatedPR:
    number: int
    title: str
    reason: str


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        t
        for t in "".join(c.lower() if c.isalnum() else " " for c in text).split()
        if len(t) > 2
    )


def _title_score(current_title: str, cand_title: str) -> float:
    a, b = _tokens(current_title), _tokens(cand_title)
    if not a or not b:
        return 0.0
    return len(a & b)


def _file_score(current_files: list[str], cand_files: list[str]) -> float:
    cur = set(current_files)
    overlap = [f for f in cand_files if f in cur]
    if not overlap:
        return 0.0
    # Exact path overlap counts most; same-directory overlap counts less.
    dirs = {f.rsplit("/", 1)[0] for f in cur if "/" in f}
    near = sum(1 for f in cand_files if f.rsplit("/", 1)[0] in dirs) - len(overlap)
    return 2.0 * len(overlap) + 0.5 * max(0, near)


def _one_line_reason(
    overlap_files: list[str],
    title_hit: bool,
    cand_title: str,
) -> str:
    if overlap_files:
        shown = ", ".join(f"`{f}`" for f in overlap_files[:3])
        extra = f" (+{len(overlap_files) - 3} more)" if len(overlap_files) > 3 else ""
        return f"touched the same files ({shown}{extra})"
    if title_hit:
        return f"related intent: {cand_title[:80]}"
    return "touched nearby files"


def _fetch_files(
    get: Callable[..., Any],
    token: str,
    owner: str,
    repo: str,
    number: int,
    timeout: float,
) -> list[str]:
    try:
        fresp = get(
            f"{_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/pulls/{number}/files",
            params={"per_page": 30},
            headers=_headers(token),
            timeout=timeout,
        )
        fresp.raise_for_status()
        return [str(f.get("filename", "")) for f in fresp.json()]
    except Exception as e:  # noqa: BLE001 - one bad candidate must not kill the section
        log.info(
            "walkthrough_related_files_failed",
            extra={
                "owner": owner,
                "repo": repo,
                "candidate": number,
                "kind": type(e).__name__,
            },
        )
        return []


def find_related_prs(
    token: str,
    owner: str,
    repo: str,
    pull_number: int,
    current_files: list[str],
    current_title: str,
    *,
    http_get: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> list[RelatedPR]:
    """Up to 3 related merged PRs, best-effort, within one _BUDGET.

    `http_get` and `clock` are injectable for tests.
    """
    get = http_get or httpx.get
    now = clock or time.monotonic
    deadline = now() + _BUDGET
    try:
        resp = get(
            f"{_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls",
            params={
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": _LIST_PER_PAGE,
            },
            headers=_headers(token),
            timeout=min(_TIMEOUT, _BUDGET),
        )
        resp.raise_for_status()
        listed = resp.json()
    except Exception as e:  # noqa: BLE001 - related hunts are best-effort enrichment
        # Best-effort, but never silent: an auth outage and an honest empty
        # would otherwise look the same.
        log.info(
            "walkthrough_related_list_failed",
            extra={"owner": owner, "repo": repo, "kind": type(e).__name__},
        )
        return []
    candidates = [
        pr
        for pr in listed
        if pr.get("merged_at") and int(pr.get("number", -1)) != pull_number
    ]
    if not candidates:
        return []

    # Cheap pass: title similarity needs no extra calls. Keep the best few
    # for the file-overlap pass (bounded).
    ranked = sorted(
        candidates,
        key=lambda pr: _title_score(current_title, str(pr.get("title", ""))),
        reverse=True,
    )[:_MAX_FILE_FETCHES]

    scored: list[tuple[float, dict[str, Any], list[str]]] = []
    budget_logged = False
    for pr in ranked:
        left = deadline - now()
        if left <= 0:
            # Budget spent: score the rest on title alone, no more calls.
            if not budget_logged:
                log.info(
                    "walkthrough_related_budget_exhausted",
                    extra={
                        "owner": owner,
                        "repo": repo,
                        "first_skipped": int(pr["number"]),
                    },
                )
                budget_logged = True
            cand_files: list[str] = []
        else:
            cand_files = _fetch_files(
                get, token, owner, repo, int(pr["number"]), min(_TIMEOUT, left)
            )
        ts = _title_score(current_title, str(pr.get("title", "")))
        fs = _file_score(current_files, cand_files)
        total = ts + fs
        if total >= _MIN_SCORE:
            overlap = [f for f in cand_files if f in set(current_files)]
            scored.append((total, pr, overlap))

    scored.sort(key=lambda t: t[0], reverse=True)
    out: list[RelatedPR] = []
    for _total, pr, overlap in scored[:_MAX_RELATED]:
        title = str(pr.get("title", ""))
        out.append(
            RelatedPR(
                number=int(pr["number"]),
                title=title,
                reason=_one_line_reason(
                    overlap,
                    _title_score(current_title, title) >= 1.0,
                    title,
                ),
            )
        )
    return out
