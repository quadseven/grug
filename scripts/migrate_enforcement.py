#!/usr/bin/env python3
"""One-time migration: backfill enforcement rulesets on all TPM-enabled repos.

For each installation with TPM enabled, detects current enforcement state
and creates a Grug-managed ruleset where none exists. Skips externally-
enforced repos. Special-cases the grug repo (legacy branch protection →
ruleset migration).

Usage:
    python scripts/migrate_enforcement.py --dry-run
    python scripts/migrate_enforcement.py

Requires:
    AWS creds with DDB read/write + SSM read (for GitHub App secrets).
    GRUG_DDB_TABLE env var (default: grug-main).
    GITHUB_APP_ID_SSM env var (SSM param name for App ID).
    GITHUB_APP_PRIVATE_KEY_SSM env var (SSM param name for App private key).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import httpx
import jwt

# ── Constants ───────────────────────────────────────────────────────

_GH_API = "https://api.github.com"
GRUG_RULESET_PREFIX = "Grug — "
GRUG_TPM_RULESET_NAME = "Grug — Chief Enforcement"
GRUG_DOR_CHECK_NAME = "Grug — Chief"
# Legacy titles still accepted when scanning existing rulesets.
LEGACY_TPM_RULESET_NAME = "Grug — TPM Enforcement"
LEGACY_DOR_CHECK_NAME = "Grug — Definition of Ready"

_HEADERS_TEMPLATE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

log = logging.getLogger("migrate_enforcement")

# ── GitHub App auth (standalone — no service-module dependency) ──────


class _TokenManager:
    def __init__(self) -> None:
        self._ssm = boto3.client("ssm")
        self._app_id: str | None = None
        self._app_key: str | None = None
        self._jwt: str | None = None
        self._jwt_exp: float = 0
        self._install_tokens: dict[int, tuple[str, float]] = {}

    def _get_ssm(self, name: str) -> str:
        resp = self._ssm.get_parameter(Name=name, WithDecryption=True)
        return resp["Parameter"]["Value"]

    def _ensure_secrets(self) -> None:
        if self._app_id is None:
            self._app_id = self._get_ssm(os.environ["GITHUB_APP_ID_SSM"])
        if self._app_key is None:
            self._app_key = self._get_ssm(os.environ["GITHUB_APP_PRIVATE_KEY_SSM"])

    def get_app_jwt(self) -> str:
        now = time.time()
        if self._jwt and now < self._jwt_exp:
            return self._jwt
        self._ensure_secrets()
        payload = {
            "iat": int(now - 60),
            "exp": int(now + 9 * 60),
            "iss": self._app_id,
        }
        self._jwt = jwt.encode(payload, self._app_key, algorithm="RS256")
        self._jwt_exp = now + 8 * 60
        return self._jwt

    def get_install_token(self, install_id: int) -> str:
        now = time.time()
        if install_id in self._install_tokens:
            tok, exp = self._install_tokens[install_id]
            if now < exp:
                return tok
        resp = httpx.post(
            f"{_GH_API}/app/installations/{install_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self.get_app_jwt()}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        tok = resp.json()["token"]
        self._install_tokens[install_id] = (tok, now + 55 * 60)
        return tok


# ── GitHub API helpers ──────────────────────────────────────────────


def _auth_headers(token: str) -> dict[str, str]:
    return {**_HEADERS_TEMPLATE, "Authorization": f"Bearer {token}"}


def _list_rulesets(token: str, owner: str, repo: str) -> list[dict]:
    resp = httpx.get(
        f"{_GH_API}/repos/{owner}/{repo}/rulesets",
        headers=_auth_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _detect_enforcement(token: str, owner: str, repo: str, branch: str) -> str:
    rulesets = _list_rulesets(token, owner, repo)
    grug_match = False
    external_match = False
    for rs in rulesets:
        has_check = False
        for rule in rs.get("rules", []):
            if rule.get("type") != "required_status_checks":
                continue
            for check in rule.get("parameters", {}).get("required_status_checks", []):
                if check.get("context") == GRUG_DOR_CHECK_NAME:
                    has_check = True
                    break
        if not has_check:
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
        from urllib.parse import quote
        legacy_resp = httpx.get(
            f"{_GH_API}/repos/{owner}/{repo}/branches/{quote(branch, safe='')}/protection/required_status_checks",
            headers=_auth_headers(token),
            timeout=10,
        )
        legacy_resp.raise_for_status()
        data = legacy_resp.json()
        if GRUG_DOR_CHECK_NAME in data.get("contexts", []):
            return "external"
        for check in data.get("checks", []):
            if isinstance(check, dict) and check.get("context") == GRUG_DOR_CHECK_NAME:
                return "external"
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in (404, 403):
            raise

    return "none"


def _create_ruleset(token: str, owner: str, repo: str) -> dict:
    body = {
        "name": GRUG_TPM_RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
        },
        "rules": [{
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [
                    {"context": GRUG_DOR_CHECK_NAME, "integration_id": None},
                ],
            },
        }],
    }
    resp = httpx.post(
        f"{_GH_API}/repos/{owner}/{repo}/rulesets",
        json=body,
        headers=_auth_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _remove_check_from_legacy_bp(
    token: str, owner: str, repo: str, branch: str, check_name: str,
) -> bool:
    """Remove a single check from legacy branch protection. Returns True if removed."""
    from urllib.parse import quote
    url = f"{_GH_API}/repos/{owner}/{repo}/branches/{quote(branch, safe='')}/protection/required_status_checks"
    try:
        resp = httpx.get(url, headers=_auth_headers(token), timeout=10)
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        return False

    data = resp.json()
    contexts = data.get("contexts", [])
    checks = data.get("checks", [])

    new_contexts = [c for c in contexts if c != check_name]
    new_checks = [c for c in checks if not (isinstance(c, dict) and c.get("context") == check_name)]

    if len(new_contexts) == len(contexts) and len(new_checks) == len(checks):
        return False

    patch_body: dict[str, Any] = {"strict": data.get("strict", False)}
    if new_checks:
        patch_body["checks"] = new_checks
    else:
        patch_body["contexts"] = new_contexts

    patch_resp = httpx.patch(
        url, json=patch_body, headers=_auth_headers(token), timeout=10,
    )
    patch_resp.raise_for_status()
    return True


# ── DDB scan ────────────────────────────────────────────────────────


def _scan_tpm_repos(table_name: str) -> list[dict]:
    """Return all REPO# rows with tpm_enabled across all installations."""
    ddb = boto3.resource("dynamodb")
    table = ddb.Table(table_name)

    items: list[dict] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "begins_with(SK, :repo_prefix)",
        "ExpressionAttributeValues": {":repo_prefix": "REPO#"},
    }
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    installs: list[dict] = []
    scan_kwargs = {
        "FilterExpression": "SK = :meta AND attribute_exists(account_login)",
        "ExpressionAttributeValues": {":meta": "META"},
    }
    while True:
        resp = table.scan(**scan_kwargs)
        installs.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    install_map = {}
    for inst in installs:
        pk = inst.get("PK", "")
        if pk.startswith("INST#"):
            install_map[pk] = inst

    results = []
    for item in items:
        pk = item.get("PK", "")
        sk = item.get("SK", "")
        tpm_enabled = bool(item.get("tpm_enabled", True))
        if not tpm_enabled:
            continue
        inst = install_map.get(pk)
        if not inst:
            continue
        install_id = int(pk.split("#")[1])
        repo_id = int(sk.split("#")[1])
        results.append({
            "install_id": install_id,
            "repo_id": repo_id,
            "repo_full_name": item.get("repo_full_name", ""),
            "account_login": inst.get("account_login", ""),
            "enforcement_ruleset_id": item.get("enforcement_ruleset_id"),
        })

    return results


def _scan_all_installs(table_name: str) -> list[dict]:
    """Return INST# META rows for installations that have no REPO# rows yet."""
    ddb = boto3.resource("dynamodb")
    table = ddb.Table(table_name)

    installs: list[dict] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "SK = :meta AND begins_with(PK, :inst) AND attribute_exists(account_login)",
        "ExpressionAttributeValues": {":meta": "META", ":inst": "INST#"},
    }
    while True:
        resp = table.scan(**scan_kwargs)
        installs.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    return installs


def _set_enforcement_id(
    table_name: str, install_id: int, repo_id: int, ruleset_id: int | None,
) -> None:
    ddb = boto3.resource("dynamodb")
    table = ddb.Table(table_name)
    if ruleset_id is not None:
        table.update_item(
            Key={"PK": f"INST#{install_id}", "SK": f"REPO#{repo_id}"},
            UpdateExpression="SET enforcement_ruleset_id = :rid",
            ExpressionAttributeValues={":rid": ruleset_id},
        )
    else:
        table.update_item(
            Key={"PK": f"INST#{install_id}", "SK": f"REPO#{repo_id}"},
            UpdateExpression="REMOVE enforcement_ruleset_id",
        )


# ── Migration logic ────────────────────────────────────────────────


def _list_install_repos(token: str, owner: str) -> list[dict]:
    """List repos accessible by the installation token."""
    repos: list[dict] = []
    url = f"{_GH_API}/installation/repositories?per_page=100"
    while url:
        resp = httpx.get(url, headers=_auth_headers(token), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        repos.extend(data.get("repositories", []))
        link = resp.headers.get("link", "")
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split("<")[1].split(">")[0]
    return repos


def _emit(entry: dict) -> None:
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(entry, default=str), flush=True)


def migrate(dry_run: bool = True) -> None:
    table_name = os.environ.get("GRUG_DDB_TABLE", "grug-main")
    tokens = _TokenManager()

    log.info("scanning DDB for installations...")
    installs = _scan_all_installs(table_name)
    repo_rows = _scan_tpm_repos(table_name)

    log.info("found %d installations, %d TPM-enabled repo rows", len(installs), len(repo_rows))

    known_repos = {(r["install_id"], r["repo_id"]) for r in repo_rows}
    stats = {"created": 0, "external": 0, "grug_managed": 0, "skipped": 0, "legacy_migrated": 0, "errors": 0}

    for inst in installs:
        pk = inst.get("PK", "")
        install_id = int(pk.split("#")[1])
        account_login = inst.get("account_login", "")

        try:
            token = tokens.get_install_token(install_id)
        except Exception as e:
            _emit({"action": "token_failed", "install_id": install_id, "error": str(e)})
            stats["errors"] += 1
            continue

        try:
            repos = _list_install_repos(token, account_login)
        except Exception as e:
            _emit({"action": "list_repos_failed", "install_id": install_id, "error": str(e)})
            stats["errors"] += 1
            continue

        for repo in repos:
            repo_id = repo["id"]
            full_name = repo["full_name"]
            owner = repo["owner"]["login"]
            repo_name = repo["name"]
            default_branch = repo.get("default_branch", "main")

            try:
                state = _detect_enforcement(token, owner, repo_name, default_branch)
            except Exception as e:
                _emit({
                    "action": "detect_failed",
                    "install_id": install_id, "repo": full_name,
                    "error": str(e),
                })
                stats["errors"] += 1
                continue

            if state == "grug_managed":
                _emit({"action": "skip", "reason": "grug_managed", "repo": full_name})
                stats["grug_managed"] += 1
                continue

            if state == "external":
                _emit({"action": "skip", "reason": "external", "repo": full_name})
                stats["external"] += 1

                is_grug_repo = full_name.lower() in ("githumps/grug",)
                if is_grug_repo:
                    _emit({"action": "legacy_bp_migration_candidate", "repo": full_name})
                    if not dry_run:
                        try:
                            result = _create_ruleset(token, owner, repo_name)
                            new_id = result["id"]
                            _set_enforcement_id(table_name, install_id, repo_id, new_id)
                            _emit({"action": "ruleset_created", "repo": full_name, "ruleset_id": new_id})

                            removed = _remove_check_from_legacy_bp(
                                token, owner, repo_name, default_branch, GRUG_DOR_CHECK_NAME,
                            )
                            if removed:
                                _emit({"action": "legacy_bp_check_removed", "repo": full_name})
                            stats["legacy_migrated"] += 1
                        except Exception as e:
                            _emit({"action": "legacy_migration_failed", "repo": full_name, "error": str(e)})
                            stats["errors"] += 1
                    else:
                        _emit({"action": "dry_run_would_migrate_legacy", "repo": full_name})
                        stats["legacy_migrated"] += 1
                continue

            # state == "none" — create ruleset
            if dry_run:
                _emit({"action": "dry_run_would_create", "repo": full_name, "install_id": install_id})
                stats["created"] += 1
            else:
                try:
                    result = _create_ruleset(token, owner, repo_name)
                    new_id = result["id"]
                    _set_enforcement_id(table_name, install_id, repo_id, new_id)
                    _emit({"action": "ruleset_created", "repo": full_name, "ruleset_id": new_id})
                    stats["created"] += 1
                except Exception as e:
                    _emit({"action": "create_failed", "repo": full_name, "error": str(e)})
                    stats["errors"] += 1

    _emit({"action": "migration_complete", "dry_run": dry_run, "stats": stats})


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill enforcement rulesets on TPM-enabled repos")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
