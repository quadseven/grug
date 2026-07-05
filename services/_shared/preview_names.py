"""Preview-environment naming + reaping logic (#500, ADR-0018).

Pure functions, no I/O: the preview workflow and the TTL janitor gather
facts (PR number, live namespaces + ages, open-PR set) and these decide
the derived names and what to reap. Keeping it here gives the janitor's
reap decision a unit-test seam the CronJob shell cannot have.

Isolation model: one namespace `grug-pr-<n>` + one Postgres SCHEMA
`grug_pr_<n>` per preview (the app bootstraps grug_kv into whatever
search_path the connection URL sets, so a schema is a full data
partition). Both are torn down when the PR closes/merges/unlabels OR the
TTL lapses.
"""

from __future__ import annotations

import re

_NS_PREFIX = "grug-pr-"
_SCHEMA_PREFIX = "grug_pr_"
DEFAULT_TTL_HOURS = 48
_NS_RE = re.compile(rf"^{_NS_PREFIX}(\d+)$")


def _require_pr(pr: int) -> int:
    """A preview id must be a positive int - it is interpolated into a
    namespace name AND a SQL schema identifier, so anything else is an
    injection vector, not a typo to tolerate."""
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        raise ValueError(f"preview PR number must be a positive int, got {pr!r}")
    return pr


def preview_namespace(pr: int) -> str:
    return f"{_NS_PREFIX}{_require_pr(pr)}"


def preview_schema(pr: int) -> str:
    return f"{_SCHEMA_PREFIX}{_require_pr(pr)}"


def pr_of_namespace(ns: str) -> int | None:
    """Extract the PR number from a preview namespace name, or None if the
    name is not a preview namespace (so the janitor never touches a
    non-preview namespace even if its selector is fooled)."""
    m = _NS_RE.match(ns)
    return int(m.group(1)) if m else None


def reap_targets(
    previews: list[dict],
    open_pr_numbers: set[int],
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> list[str]:
    """Namespaces to delete. `previews` = [{"namespace", "age_hours"}].
    A preview is reaped when its PR is no longer open (closed/merged/
    unlabeled) OR it has outlived the TTL. A namespace whose name is not
    a valid preview is NEVER reaped (defense against a bad selector)."""
    out = []
    for p in previews:
        ns = p.get("namespace", "")
        pr = pr_of_namespace(ns)
        if pr is None:
            continue  # not a preview namespace - never touch
        age = p.get("age_hours", 0)
        if pr not in open_pr_numbers or age > ttl_hours:
            out.append(ns)
    return out

