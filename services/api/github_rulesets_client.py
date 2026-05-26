# MIRRORED — sibling at services/webhook/github_rulesets_client.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""GitHub Repository Rulesets API client — create/list/delete + enforcement detection.

Wraps the Rulesets endpoints Grug needs for automatic DoR enforcement.
Also queries legacy branch protection for repos that haven't migrated.
Tokens fetched per-installation via github_app_auth.
"""

from __future__ import annotations

import logging
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


def _auth_headers(install_token: str) -> dict[str, str]:
    return {**_HEADERS_TEMPLATE, "Authorization": f"Bearer {install_token}"}


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
                    "required_status_checks": [
                        {"context": ctx, "integration_id": None}
                        for ctx in status_check_contexts
                    ],
                },
            },
        ],
    }
    resp = httpx.post(
        f"{_GH_API}/repos/{owner}/{repo}/rulesets",
        json=body,
        headers=_auth_headers(install_token),
        timeout=10,
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
        f"{_GH_API}/repos/{owner}/{repo}/rulesets/{ruleset_id}",
        headers=_auth_headers(install_token),
        timeout=10,
    )
    resp.raise_for_status()


def list_rulesets(
    install_token: str,
    owner: str,
    repo: str,
) -> list[dict]:
    """List all rulesets for a repository."""
    resp = httpx.get(
        f"{_GH_API}/repos/{owner}/{repo}/rulesets",
        headers=_auth_headers(install_token),
        timeout=10,
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
        legacy_resp = httpx.get(
            f"{_GH_API}/repos/{owner}/{repo}/branches/{quote(branch, safe='')}/protection/required_status_checks",
            headers=_auth_headers(install_token),
            timeout=10,
        )
        legacy_resp.raise_for_status()
        if _check_name_in_legacy(legacy_resp.json(), check_name):
            return "external"
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        log.debug(
            "legacy_branch_protection_not_configured",
            extra={"owner": owner, "repo": repo, "branch": branch},
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
