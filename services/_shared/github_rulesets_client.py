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

GRUG_RULESET_PREFIX = "Grug - "
GRUG_RULESET_PREFIXES = (GRUG_RULESET_PREFIX, "Grug " + "\u2014" + " ")

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
# Budget note: these GETs run inside a user-facing grug-api request, so the
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
    respected but capped at _GET_RETRY_MAX_DELAY to stay inside the request
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


def update_ruleset(
    install_token: str,
    owner: str,
    repo: str,
    ruleset_id: int,
    status_check_contexts: list[str],
) -> dict:
    """Replace an existing ruleset's required_status_checks contexts.

    Used to heal a Grug-managed ruleset that still names a stale check
    title (e.g. a pre-rename em-dash alias) after the canonical check
    name changes - without this, an already-enrolled repo's required
    check is silently pinned to a title Grug no longer posts as primary.
    """
    body = {
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [
                        {"context": ctx}
                        for ctx in status_check_contexts
                    ],
                },
            },
        ],
    }
    resp = httpx.put(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/rulesets/{ruleset_id}",
        json=body,
        headers=_auth_headers(install_token),
        timeout=10,
    )
    if resp.status_code >= 400:
        log.warning(
            "update_ruleset_rejected",
            extra={
                "owner": owner, "repo": repo, "ruleset_id": ruleset_id,
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


# Rulesets are per-repo config, not data: even ruleset-heavy repos carry a
# handful. 5 pages x 100 is far past plausible; the cap exists so a pagination
# bug can never spin (same posture as _INSTALL_REPOS_MAX_PAGES).
_RULESETS_MAX_PAGES = 5


def list_rulesets(
    install_token: str,
    owner: str,
    repo: str,
) -> list[dict]:
    """List all rulesets for a repository. Resilient to GitHub's secondary
    rate limit (the dashboard fan-out burst) via bounded jittered retries.

    Paginated (grug#570): the endpoint defaults to 30 per page, so a repo
    with more rulesets than one page would silently hide the rest from
    enforcement detection. Same log-and-truncate cap posture as
    ``list_installation_repos`` - never spin.

    GitHub's LIST endpoint returns SUMMARIES ONLY - id, name, target,
    enforcement, source, timestamps - it does NOT include ``rules``
    (verified live, grug#567). Callers that need to inspect a ruleset's
    actual rules (e.g. required_status_checks) must fetch full detail
    per candidate via ``get_ruleset()``.
    """
    out: list[dict] = []
    for page in range(1, _RULESETS_MAX_PAGES + 1):
        resp = _get_with_retry(
            f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/rulesets?per_page=100&page={page}",
            install_token=install_token,
            op="list_rulesets",
        )
        resp.raise_for_status()
        batch = resp.json() or []
        out.extend(batch)
        if len(batch) < 100:
            break
    else:
        log.warning(
            "rulesets_pagination_cap",
            extra={"owner": owner, "repo": repo, "count": len(out)},
        )
    return out


def get_ruleset(
    install_token: str,
    owner: str,
    repo: str,
    ruleset_id: int,
) -> dict:
    """Fetch a single ruleset's FULL detail, including ``rules`` (the
    LIST endpoint omits this - grug#567). Resilient to GitHub's secondary
    rate limit via the same bounded jittered retries as list_rulesets."""
    resp = _get_with_retry(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/rulesets/{ruleset_id}",
        install_token=install_token,
        op="get_ruleset",
    )
    resp.raise_for_status()
    return resp.json()


def _acceptable_names(check_name: str) -> tuple[str, ...]:
    """Primary check title plus cutover aliases (tribe nomenclature)."""
    try:
        from personas.tribe import acceptable_check_names
        return acceptable_check_names(check_name)
    except Exception:  # noqa: BLE001 - rulesets client must not depend on tribe import
        return (check_name,)


def _check_name_in_ruleset(ruleset: dict, check_name: str) -> bool:
    """Return True if any required_status_checks rule matches primary or alias."""
    names = set(_acceptable_names(check_name))
    for rule in ruleset.get("rules", []):
        if rule.get("type") != "required_status_checks":
            continue
        for check in rule.get("parameters", {}).get("required_status_checks", []):
            if check.get("context") in names:
                return True
    return False


def _check_name_in_legacy(legacy_data: dict, check_name: str) -> bool:
    """Check both legacy ``contexts`` and newer ``checks`` array formats."""
    names = set(_acceptable_names(check_name))
    for ctx in legacy_data.get("contexts", []):
        if ctx in names:
            return True
    for check in legacy_data.get("checks", []):
        if isinstance(check, dict) and check.get("context") in names:
            return True
    return False


_INSTALL_REPOS_MAX_PAGES = 10  # 1000 repos - same v1 cap as the api's list_repos


def list_installation_repos(install_token: str) -> list[dict]:
    """Enumerate the repos this installation grants access to
    (``GET /installation/repositories``, paginated).

    Returns ``[{"id", "full_name", "default_branch"}]``. This is the
    GROUND-TRUTH denominator for "repos grug is expected to act on" - the
    #460 re-emission pass discovered live that the store's REPO# rows are
    written only on explicit config changes, so a defaults-only install
    has ZERO rows and any store-driven enumeration is empty. GitHub owns
    the install-repo relationship; the store overlays per-repo opt-outs.
    Page cap mirrors the api's list_repos (log + truncate, never spin).
    """
    out: list[dict] = []
    for page in range(1, _INSTALL_REPOS_MAX_PAGES + 1):
        resp = _get_with_retry(
            f"{_GH_API}/installation/repositories?per_page=100&page={page}",
            install_token=install_token,
            op="installation_repositories",
        )
        resp.raise_for_status()
        repos = (resp.json() or {}).get("repositories", [])
        if not repos:
            break
        for r in repos:
            out.append({
                "id": r.get("id"),
                "full_name": r.get("full_name", ""),
                "default_branch": r.get("default_branch") or "main",
            })
        if len(repos) < 100:
            break
    else:
        log.warning("installation_repos_pagination_cap", extra={"count": len(out)})
    return out


def detect_enforcement(
    install_token: str,
    owner: str,
    repo: str,
    branch: str,
    check_name: str,
    stored_ruleset_id: int | None = None,
) -> EnforcementState:
    """Determine whether check_name is enforced and by whom.

    Checks the Rulesets API first, then falls back to legacy branch
    protection. Returns ``"grug_managed"`` if a ruleset enforces the
    check AND either matches ``stored_ruleset_id`` (the ID Grug itself
    created, tracked in the install store) or matches the ``Grug -`` /
    ``Grug —`` name-prefix heuristic. ``"external"`` if enforced by a
    non-Grug mechanism, or ``"none"`` if not enforced at all.

    The prefix heuristic is consulted for any check-enforcing ruleset
    the stored ID did not already claim - NOT only when no ID is on
    file. This is deliberate and load-bearing: the branch is reached
    only after ``_check_name_in_ruleset`` confirms the ruleset actually
    enforces ``check_name``, so a Grug-named ruleset that enforces the
    check IS grug-managed even when the stored ID is stale (deleted or
    pointing at a different ruleset). Gating the heuristic on
    ``stored_ruleset_id is None`` would misclassify that live ruleset as
    ``external`` and make ``ensure_enforcement`` try to create a second
    one - a guaranteed 422 "Name must be unique" collision.

    The ID check is load-bearing, not cosmetic: a rename, a manual
    rename of the ruleset itself, or any other drift between the
    ruleset's live name and GRUG_RULESET_PREFIX would otherwise
    misclassify an actually-enforcing Grug ruleset as external/none
    (grug#565 - found via a live repo rename,
    ruleset id 15934208 named "Grug TPM gate" doesn't match the prefix).

    list_rulesets() only returns SUMMARIES (grug#567 - verified live,
    no ``rules`` key at all in the LIST response), so each candidate's
    full detail is fetched individually via get_ruleset() before its
    rules are inspected. Bounded to active, branch-target rulesets
    (the only kind that can enforce a branch status check) to keep the
    extra per-ruleset GETs to the handful that could plausibly match -
    most repos have 1-3 rulesets total. Short-circuits on the first
    grug_match, since that already wins the final classification below.
    """
    rulesets = list_rulesets(install_token, owner, repo)

    grug_match = False
    external_match = False
    for rs in rulesets:
        if rs.get("enforcement") != "active" or rs.get("target") != "branch":
            continue
        full = get_ruleset(install_token, owner, repo, rs["id"])
        if not _check_name_in_ruleset(full, check_name):
            continue
        if stored_ruleset_id is not None and rs.get("id") == stored_ruleset_id:
            grug_match = True
            break
        if any(rs.get("name", "").startswith(p) for p in GRUG_RULESET_PREFIXES):
            grug_match = True
            break
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
