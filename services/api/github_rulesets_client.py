# MIRRORED — sibling at services/webhook/github_rulesets_client.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""GitHub Repository Rulesets API client — create/list/delete + enforcement detection.

Wraps the Rulesets endpoints Grug needs for automatic DoR enforcement.
Also queries legacy branch protection for repos that haven't migrated.
Tokens fetched per-installation via github_app_auth.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Literal
from urllib.parse import quote

import httpx

_GH_API = "https://api.github.com"

GRUG_RULESET_PREFIX = "Grug — "

EnforcementState = Literal["grug_managed", "external", "none"]

log = logging.getLogger("grug.rulesets")

_HEADERS_TEMPLATE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# --- Resilient GET (#272 follow-up: dashboard 429 storm) -------------------
# The dashboard fans out one /enforcement request per repo in parallel, each
# of which calls GitHub's rulesets API; the burst trips GitHub's secondary
# rate limit (429, sometimes 403 + Retry-After). A bare raise_for_status made
# the whole enforcement check fail → the UI showed a false "not enforced".
#
# Budget note: these GETs run inside the grug-api Lambda (15s timeout), so the
# retry budget is deliberately SMALL — a few jittered, short backoffs to ride
# out a transient burst. A SUSTAINED rate limit is NOT waited out here; it
# exhausts fast and the caller (get_enforcement) falls back to the last-known
# stored state. Resilience = small retries + jitter + graceful fallback, not
# long blocking waits on a user-facing request.
_GET_RETRY_ATTEMPTS = 3          # 1 initial + 2 retries
_GET_RETRY_BASE_DELAY = 0.3      # seconds; grows 0.3 → 0.6 → ...
_GET_RETRY_MAX_DELAY = 2.0       # cap per-attempt sleep (+ caps honored Retry-After)
_GET_RETRYABLE_STATUSES = frozenset((429, 502, 503, 504))


def _auth_headers(install_token: str) -> dict[str, str]:
    return {**_HEADERS_TEMPLATE, "Authorization": f"Bearer {install_token}"}


def _RETRY_SLEEP(seconds: float) -> None:  # noqa: N802 — mockable seam (matches llm_client)
    """Indirection so tests can stub the wait without real time passing."""
    time.sleep(seconds)


def _is_rate_limited(resp: httpx.Response) -> bool:
    """Retryable rate-limit / transient signal. 429 + 5xx always; a 403 only
    when it carries a rate-limit signal (Retry-After, or exhausted primary
    quota via X-RateLimit-Remaining: 0) — NOT a plain permission 403."""
    if resp.status_code in _GET_RETRYABLE_STATUSES:
        return True
    if resp.status_code == 403:
        return (
            "retry-after" in resp.headers
            or resp.headers.get("x-ratelimit-remaining") == "0"
        )
    return False


def _retry_delay(attempt: int, resp: httpx.Response | None) -> float:
    """Equal-jitter exponential backoff, honoring (capped) Retry-After.

    Equal jitter (`base/2 + rand(0, base/2)`) de-syncs the dashboard's
    parallel retries so they don't re-collide into a fresh burst, while
    still guaranteeing a meaningful minimum wait. Retry-After (seconds) is
    respected but capped at _GET_RETRY_MAX_DELAY to stay inside the Lambda
    budget — a longer limit is handled by the caller's fallback, not a wait.
    """
    base = min(_GET_RETRY_BASE_DELAY * (2 ** attempt), _GET_RETRY_MAX_DELAY)
    if resp is not None:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                base = min(max(base, float(int(retry_after))), _GET_RETRY_MAX_DELAY)
            except (TypeError, ValueError):
                pass  # HTTP-date form (rare for GH) — fall back to backoff
    return base / 2 + random.uniform(0, base / 2)


def _get_with_retry(
    url: str, *, install_token: str, timeout: float = 10.0, op: str,
) -> httpx.Response:
    """GET with bounded jittered retries on rate-limit / transient errors.

    Returns the final response (caller decides on `raise_for_status`). Retries
    a rate-limited (429 / rate-limit-403) or 5xx response, and transport
    errors, up to `_GET_RETRY_ATTEMPTS`; logs each retry + an exhausted line so
    the storm is visible in DD. Does NOT swallow — a non-retryable status is
    returned as-is, and a transport error on the final attempt re-raises.
    """
    headers = _auth_headers(install_token)
    last_exc: httpx.RequestError | None = None
    resp: httpx.Response | None = None
    for attempt in range(_GET_RETRY_ATTEMPTS):
        last = attempt == _GET_RETRY_ATTEMPTS - 1
        try:
            resp = httpx.get(url, headers=headers, timeout=timeout)
        except httpx.RequestError as e:
            last_exc = e
            if last:
                log.warning(
                    "rulesets_get_retry_exhausted",
                    extra={"op": op, "attempts": _GET_RETRY_ATTEMPTS,
                           "kind": type(e).__name__},
                )
                raise
            delay = _retry_delay(attempt, None)
            log.warning(
                "rulesets_get_retry",
                extra={"op": op, "attempt": attempt + 1, "kind": type(e).__name__,
                       "delay_s": round(delay, 3)},
            )
            _RETRY_SLEEP(delay)
            continue
        if not last and _is_rate_limited(resp):
            delay = _retry_delay(attempt, resp)
            log.warning(
                "rulesets_get_retry",
                extra={"op": op, "attempt": attempt + 1,
                       "status": resp.status_code,
                       "retry_after": resp.headers.get("retry-after"),
                       "delay_s": round(delay, 3)},
            )
            _RETRY_SLEEP(delay)
            continue
        if _is_rate_limited(resp):
            # Out of attempts on a rate-limited response — surface it (the
            # caller's fallback handles sustained limits). Make it visible.
            log.warning(
                "rulesets_get_retry_exhausted",
                extra={"op": op, "attempts": _GET_RETRY_ATTEMPTS,
                       "status": resp.status_code},
            )
        return resp
    # Unreachable (loop always returns/raises), but satisfies type checkers.
    if resp is not None:
        return resp
    assert last_exc is not None
    raise last_exc


def create_ruleset(
    install_token: str,
    owner: str,
    repo: str,
    name: str,
    status_check_contexts: list[str],
) -> dict:
    """Create a ruleset requiring status checks on the default branch."""
    body = {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {
                "include": ["~DEFAULT_BRANCH"],
                "exclude": [],
            },
        },
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    # OMIT integration_id — GitHub's ruleset schema rejects a
                    # null integration_id ("Invalid property /rules/0: data
                    # matches no possible input", 422), which broke every
                    # enforcement "fix". integration_id is optional; sending
                    # just {context} requires the check by name regardless of
                    # which app reports it.
                    "required_status_checks": [
                        {"context": ctx}
                        for ctx in status_check_contexts
                    ],
                },
            },
        ],
    }
    resp = httpx.post(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/rulesets",
        json=body,
        headers=_auth_headers(install_token),
        timeout=10,
    )
    if resp.status_code >= 400:
        # Log GitHub's validation detail before raising — a 422 here is
        # otherwise opaque (raise_for_status drops the body), and it's exactly
        # what the dashboard "fix" button hit. The body names the real cause
        # (duplicate ruleset name / invalid rule param / missing perm).
        log.warning(
            "create_ruleset_rejected",
            extra={
                "owner": owner, "repo": repo, "ruleset_name": name,
                "status": resp.status_code,
                "body": resp.text[:600],
            },
        )
    resp.raise_for_status()
    return resp.json()


def delete_ruleset(
    install_token: str,
    owner: str,
    repo: str,
    ruleset_id: int,
) -> None:
    """Delete a ruleset by ID."""
    resp = httpx.delete(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/rulesets/{ruleset_id}",
        headers=_auth_headers(install_token),
        timeout=10,
    )
    resp.raise_for_status()


def list_rulesets(
    install_token: str,
    owner: str,
    repo: str,
) -> list[dict]:
    """List all rulesets for a repository. Resilient to GitHub's secondary
    rate limit (the dashboard fan-out burst) via bounded jittered retries."""
    resp = _get_with_retry(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/rulesets",
        install_token=install_token,
        op="list_rulesets",
    )
    resp.raise_for_status()
    return resp.json()


def _check_name_in_ruleset(ruleset: dict, check_name: str) -> bool:
    """Return True if any required_status_checks rule in the ruleset matches check_name."""
    for rule in ruleset.get("rules", []):
        if rule.get("type") != "required_status_checks":
            continue
        for check in rule.get("parameters", {}).get("required_status_checks", []):
            if check.get("context") == check_name:
                return True
    return False


def _check_name_in_legacy(legacy_data: dict, check_name: str) -> bool:
    """Check both legacy ``contexts`` and newer ``checks`` array formats."""
    if check_name in legacy_data.get("contexts", []):
        return True
    for check in legacy_data.get("checks", []):
        if isinstance(check, dict) and check.get("context") == check_name:
            return True
    return False


def detect_enforcement(
    install_token: str,
    owner: str,
    repo: str,
    branch: str,
    check_name: str,
) -> EnforcementState:
    """Determine whether check_name is enforced and by whom.

    Checks the Rulesets API first, then falls back to legacy branch
    protection. Returns ``"grug_managed"`` if a ``Grug —``-prefixed
    ruleset enforces the check, ``"external"`` if enforced by a
    non-Grug mechanism, or ``"none"`` if not enforced at all.
    """
    rulesets = list_rulesets(install_token, owner, repo)

    grug_match = False
    external_match = False
    for rs in rulesets:
        if not _check_name_in_ruleset(rs, check_name):
            continue
        if rs.get("name", "").startswith(GRUG_RULESET_PREFIX):
            grug_match = True
        else:
            external_match = True

    if grug_match:
        return "grug_managed"
    if external_match:
        return "external"

    try:
        legacy_resp = _get_with_retry(
            f"{_GH_API}/repos/{owner}/{repo}/branches/{quote(branch, safe='')}/protection/required_status_checks",
            install_token=install_token,
            op="legacy_branch_protection",
        )
        legacy_resp.raise_for_status()
        if _check_name_in_legacy(legacy_resp.json(), check_name):
            return "external"
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in (404, 403):
            raise
        log.debug(
            "legacy_branch_protection_unavailable",
            extra={"owner": owner, "repo": repo, "branch": branch,
                   "status": e.response.status_code},
        )
    except httpx.RequestError as e:
        log.warning(
            "legacy_branch_protection_transport_failed",
            extra={
                "owner": owner,
                "repo": repo,
                "branch": branch,
                "kind": type(e).__name__,
            },
        )

    return "none"
