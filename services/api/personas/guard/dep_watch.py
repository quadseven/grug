# MIRRORED — sibling at services/webhook/personas/guard/dep_watch.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Guard dependency watch (#491) - the owned dependabot-class pass.

Guard's diff-time SCA (#434) only sees pins a PR touches; a vulnerable
pin already merged sits silent until someone edits the manifest. This
scheduled pass (grug-poller cadence, store-driven targeting like Pulse)
audits the DEFAULT BRANCH's pinned deps against OSV and files ONE
caveman-voiced quarantine report issue per repo per week when known
vulns exist - Guard's landing-page promise ("quarantines evil
dependencies before they reach main") made real.

Default OFF per repo (`dep_watch_enabled`). Best-effort per repo;
OSV/GitHub failures log and continue. Reuses sca.py's pin regex + OSV
batch audit so the advisory data and parse rules can never drift from
the diff-time scanner.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

from adapters.install_store import claim_dep_watch_report, get_repo_config
from personas.code_reviewer.sca import ChangedDep, _audit, _PINNED_DEP_RE

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.guard.dep_watch")

_MANIFESTS = ("requirements.txt", "pyproject.toml")
_FETCH_TIMEOUT = 10
_MAX_PINS = 100
_MAX_REPORT_ROWS = 30
_REPORT_MARKER = "<!-- grug-guard-dep-watch -->"
_REPORT_TITLE = "[grug-guard] Dependency quarantine report"


def parse_manifest_pins(path: str, text: str) -> tuple[ChangedDep, ...]:
    """Pinned `name==version` deps from a manifest's CONTENT (vs sca's
    diff-line scope). Pure; unpinned/complex specifiers are skipped and
    counted by the caller via len() difference. Line numbers are real so
    the report can cite them."""
    out: list[ChangedDep] = []
    seen: set[tuple[str, str]] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PINNED_DEP_RE.match(line)
        if not m:
            continue
        key = (m.group("name").lower(), m.group("version"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ChangedDep(
            file=path, line=lineno, name=m.group("name"), version=m.group("version"),
        ))
    return tuple(out[:_MAX_PINS])


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_manifest(token: str, owner: str, repo: str, path: str) -> str | None:
    """Manifest content at the default branch, None when absent."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/{quote(path, safe='/')}",
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def _report_body(rows: list[dict[str, Any]]) -> str:
    lines = [
        "Grug Guard walk the stores at night and smell sick meat. These",
        "dependencies on the MAIN trail carry known evil. Quarantine or",
        "bump them before they bite the tribe.",
        "",
        "| Manifest | Line | Dependency | Pinned | Advisories |",
        "|---|---|---|---|---|",
    ]
    for r in rows[:_MAX_REPORT_ROWS]:
        lines.append(
            f"| `{r['file']}` | {r['line']} | `{r['name']}` | `{r['version']}` "
            f"| {', '.join(r['ids'][:4])} |"
        )
    if len(rows) > _MAX_REPORT_ROWS:
        lines.append(f"\n...and {len(rows) - _MAX_REPORT_ROWS} more (capped).")
    lines += [
        "",
        "Advisory IDs are OSV (https://osv.dev/<id>). Re-checked weekly while",
        "dep_watch is enabled; this report refreshes rather than duplicates.",
        "",
        _REPORT_MARKER,
    ]
    return "\n".join(lines)


def _existing_report(token: str, owner: str, repo: str) -> int | None:
    """Open quarantine-report issue number, or None. Marker-based - the
    same write-verification discipline as Pulse."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues",
        params={"state": "open", "per_page": 50},
        headers={**_headers(token), "Accept": "application/vnd.github+json"},
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    for issue in resp.json() or []:
        if issue.get("pull_request"):
            continue
        if _REPORT_TITLE in (issue.get("title") or ""):
            return int(issue["number"])
    return None


def run_dep_watch_for_install(
    token: str, install_id: int, repos: list[dict[str, Any]],
) -> int:
    """One dep-watch pass for one install's ENABLED repos (store-driven
    list, the Pulse pattern). Returns reports filed/refreshed. Never
    raises past a repo."""
    filed = 0
    for repo in repos:
        repo_id = repo.get("id")
        full = repo.get("full_name", "")
        owner, _, name = full.partition("/")
        if not (repo_id and owner and name):
            continue
        try:
            if not get_repo_config(install_id, int(repo_id)).get("dep_watch_enabled", False):
                continue
            pins: list[ChangedDep] = []
            for manifest in _MANIFESTS:
                text = _fetch_manifest(token, owner, name, manifest)
                if text:
                    pins.extend(parse_manifest_pins(manifest, text))
            if not pins:
                continue
            vulns = _audit(tuple(pins))
            if not vulns:
                log.info("dep_watch_clean", extra={"repo": full, "pins": len(pins)})
                continue
            rows = [
                {"file": d.file, "line": d.line, "name": d.name,
                 "version": d.version, "ids": vulns[(d.name.lower(), d.version)]}
                for d in pins if (d.name.lower(), d.version) in vulns
            ]
            # Weekly cadence claim BEFORE any write (the Pulse ordering:
            # a lost claim = this window already reported).
            if not claim_dep_watch_report(install_id, full):
                continue
            existing = _existing_report(token, owner, name)
            api_headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if existing:
                resp = httpx.patch(
                    f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues/{existing}",
                    json={"body": _report_body(rows)},
                    headers=api_headers, timeout=_FETCH_TIMEOUT,
                )
            else:
                resp = httpx.post(
                    f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues",
                    json={
                        "title": f"{_REPORT_TITLE} ({len(rows)} dependency(ies))",
                        "body": _report_body(rows),
                    },
                    headers=api_headers, timeout=_FETCH_TIMEOUT,
                )
            resp.raise_for_status()
            filed += 1
            log.info(
                "dep_watch_reported",
                extra={"install_id": install_id, "repo": full,
                       "vulnerable": len(rows), "refreshed": bool(existing)},
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.warning(
                "dep_watch_repo_failed",
                extra={"install_id": install_id, "repo": full, "kind": type(e).__name__},
            )
    return filed
