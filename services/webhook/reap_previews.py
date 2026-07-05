"""Preview TTL janitor entrypoint (#500, ADR-0018).

Runs in the webhook image on an hourly CronJob (k8s/preview-janitor.yaml).
Lists grug-pr-<n> namespaces via the in-cluster k8s API, reaps namespaces past the TTL (the preview workflow already reaps on
PR close/unlabel + drops the schema; this is the TTL backstop). The
Postgres schema is left to the workflow teardown's idempotent DROP
SCHEMA IF EXISTS on close - it is empty of prod data and cheap; a
TTL-reaped-but-still-open preview redeploys with a fresh schema.

TTL-only by design: `reap_targets` is fed open_pr_numbers = every preview
currently present, so the "PR closed" branch never fires here (that path
is the workflow's) - only age > GRUG_PREVIEW_TTL_HOURS reaps. Safety: the
pure `pr_of_namespace` guard means a namespace whose name is not
^grug-pr-<digits>$ is never a candidate, so a fat-fingered selector
cannot delete prod/system namespaces.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timezone

from preview_names import DEFAULT_TTL_HOURS, pr_of_namespace, reap_targets

_SA = "/var/run/secrets/kubernetes.io/serviceaccount"


def _k8s(path: str, method: str = "GET") -> dict:
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    with open(f"{_SA}/token", encoding="utf-8") as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=f"{_SA}/ca.crt")
    req = urllib.request.Request(
        f"https://{host}:{port}{path}", method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else {}


def _preview_namespaces() -> list[dict]:
    """[{'namespace', 'age_hours'}] for every grug-pr-<n> namespace."""
    now = datetime.now(timezone.utc)
    out = []
    for item in _k8s("/api/v1/namespaces").get("items", []):
        name = item["metadata"]["name"]
        if pr_of_namespace(name) is None:
            continue
        ts = item["metadata"].get("creationTimestamp")
        try:
            created = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            # One unparseable timestamp must not abort the whole sweep
            # (Qodo review on #531); skip it - next run retries.
            print(f"::warning:: unparseable creationTimestamp on {name}: {ts!r}", file=sys.stderr)
            continue
        out.append({"namespace": name, "age_hours": (now - created).total_seconds() / 3600})
    return out


def main() -> int:
    ttl = int(os.environ.get("GRUG_PREVIEW_TTL_HOURS", str(DEFAULT_TTL_HOURS)))
    previews = _preview_namespaces()
    present_prs = {pr_of_namespace(p["namespace"]) for p in previews}
    present_prs.discard(None)
    # TTL-only: treat every present preview as "open" so only age reaps.
    targets = reap_targets(previews, open_pr_numbers=present_prs, ttl_hours=ttl)
    print(f"preview-janitor: {len(previews)} previews, {len(targets)} past {ttl}h TTL")
    for ns in targets:
        try:
            _k8s(f"/api/v1/namespaces/{ns}", method="DELETE")
            print(f"deleted namespace {ns}")
        except Exception as e:  # noqa: BLE001 - one reap must not abort the sweep
            print(f"::warning:: failed to delete {ns}: {type(e).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
