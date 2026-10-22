"""Shared authenticated GitHub REST client.

Single home for the request primitives (Authorization / Accept /
X-GitHub-Api-Version headers, timeout, raise_for_status, rate-limit
logging) plus a paginate-all helper, so the hand-rolled copies across
services/ stop drifting.

Retry / 401 handling is deliberately NOT here: `with_install_token_retry`
at the call site owns cache invalidation + retry. This module never
swallows an error - rate-limit responses are logged and re-raised.

The `paginate_all` helper takes one explicit knob, `max_pages` (default
10 = 1000 repos at per_page=100, the existing v1 ceiling). `max_pages=None`
scans every page with no cap. The ceiling value itself is not changed -
only made a single explicit, documented parameter.

Follow-up (deliberately NOT converted in this slice): the remaining
hand-rolled GET/POST/PATCH sites under services/webhook/ and
services/_shared/personas/*. Each uses a different token scheme
(Bearer vs `token`) and response shape, so they are left as-is with a
follow-up issue rather than risking a silent behavior change. Route them
through github_request / paginate_all one at a time with a transport
shape audit.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("grug.github_rest")

_GH_API = "https://api.github.com"
_DEFAULT_TIMEOUT = 10.0
_DEFAULT_PER_PAGE = 100
# 10 pages x 100 = 1000 repos: the existing v1 ceiling, kept as the default
# so callers that don't opt out keep today's behaviour.
_DEFAULT_MAX_PAGES = 10

_RATE_LIMIT_STATUS = frozenset({403, 429})


class GitHubMalformedPayload(Exception):
    """A GitHub list page came back without the expected item key."""


def github_headers(token: str, *, bearer: bool = False) -> dict[str, str]:
    """Standard GitHub REST headers for an installation token.

    `bearer=True` emits `Authorization: Bearer <token>` (checks/reviews/
    rulesets clients). The default emits `Authorization: token <token>`
    (installation/repositories and the legacy installs endpoints).
    """
    scheme = "Bearer" if bearer else "token"
    return {
        "Authorization": f"{scheme} {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_request(
    method: str,
    url: str,
    token: str,
    *,
    bearer: bool = False,
    params: dict[str, Any] | None = None,
    json: Any = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> httpx.Response:
    """One authenticated GitHub REST call.

    Standard headers + timeout + raise_for_status in one place. 401s and
    rate-limit responses propagate to the caller
    (`with_install_token_retry` owns cache invalidation + retry).
    """
    resp = httpx.request(
        method,
        url,
        headers=github_headers(token, bearer=bearer),
        params=params,
        json=json,
        timeout=timeout,
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        _log_rate_limit(e)
        raise
    return resp


def _log_rate_limit(exc: httpx.HTTPStatusError) -> None:
    """Log a warning for a rate-limited response, then let it propagate."""
    resp = exc.response
    if resp is None or resp.status_code not in _RATE_LIMIT_STATUS:
        return
    if resp.headers.get("x-ratelimit-remaining") != "0" and resp.status_code != 429:
        return
    retry_after = resp.headers.get("retry-after") or resp.headers.get(
        "x-ratelimit-reset"
    )
    log.warning(
        "github_rest_rate_limited",
        extra={"status_code": resp.status_code, "retry_after": retry_after},
    )


def paginate_all(
    token: str,
    url: str,
    *,
    item_key: str | None,
    params: dict[str, Any] | None = None,
    per_page: int = _DEFAULT_PER_PAGE,
    max_pages: int | None = _DEFAULT_MAX_PAGES,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Fetch every page of a GitHub list endpoint.

    `item_key` is the response body key holding the page's items
    (e.g. `"repositories"`). Pass `item_key=None` when the body itself is
    a list (e.g. the PR reviews / review-comments endpoints).

    Stops on a short page, or when `max_pages` is reached - in which case
    a `github_rest_pagination_cap` warning is logged and the items
    collected so far are returned. `max_pages=None` scans every page with
    no cap.

    Raises `GitHubMalformedPayload` when a page's shape doesn't match
    `item_key` (never silently returns an empty list for an upstream
    breakage).
    """
    items: list[dict[str, Any]] = []
    page = 1
    with httpx.Client(timeout=timeout) as client:
        while True:
            req_params = {**params} if params else {}
            req_params["per_page"] = per_page
            req_params["page"] = page
            resp = client.get(
                url,
                headers=github_headers(token),
                params=req_params,
            )
            resp.raise_for_status()
            body = resp.json() or {}
            if item_key is None:
                if not isinstance(body, list):
                    raise GitHubMalformedPayload(
                        f"expected a list body at {url} page {page}, "
                        f"got {type(body).__name__}"
                    )
                batch: list[Any] = body
            else:
                if not isinstance(body, dict) or item_key not in body:
                    raise GitHubMalformedPayload(
                        f"missing {item_key!r} key in {url} page {page} body"
                    )
                batch = body[item_key] or []
            items.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
            if max_pages is not None and page > max_pages:
                log.warning(
                    "github_rest_pagination_cap",
                    extra={"url": url, "max_pages": max_pages,
                           "items_collected": len(items)},
                )
                break
    return items
