"""Ingest logs/review-ledger.jsonl into the store (#361 slice 1).

Runs as a one-shot (CLI or a k8s Job): read the committed corpus, skip
malformed lines, and upsert each finding as a first-class grug_kv row via
pg_install_store.put_ledger_row. Idempotent - re-running heals rows and
adds any new ones. The `seq` disambiguates findings that share
(class, pr, reviewer) within a repo.

    python ingest_ledger.py [path-to-jsonl]   # default: logs/review-ledger.jsonl
"""

from __future__ import annotations

import json
import sys

from ledger import parse_row

_DEFAULT_PATH = "logs/review-ledger.jsonl"


def ingest_text(
    text: str, put=None, put_practices=None, put_exemplars=None
) -> dict[str, object]:
    """Parse + persist every valid ledger line. `put` defaults to the store
    adapter but is injectable for tests. Returns {ingested, skipped}."""
    if put is None:
        from adapters.pg_install_store import put_ledger_row  # type: ignore
        put = put_ledger_row
    ingested = skipped = 0
    parsed_by_repo: dict[str, list] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        lr = parse_row(row)
        if lr is None:  # reuse the same validity gate
            skipped += 1
            continue
        put(row)  # key is content-derived in the store; ingest order irrelevant
        parsed_by_repo.setdefault(lr.repo, []).append(lr)
        ingested += 1
    refreshed = _refresh_practices(parsed_by_repo, put_practices)
    _refresh_exemplars(parsed_by_repo, put_exemplars)
    return {"ingested": ingested, "skipped": skipped, "repos_refreshed": refreshed}


def _refresh_exemplars(parsed_by_repo: dict, put_exemplars=None) -> int:
    """After ingest, recompute + cache each repo's few-shot exemplars
    (#538) beside the practices refresh. Best-effort per repo."""
    from few_shot import exemplars_to_dicts
    from ledger import accepted_findings_by_class
    if put_exemplars is None:
        try:
            from adapters.pg_install_store import put_repo_exemplars  # type: ignore
            put_exemplars = put_repo_exemplars
        except Exception:  # noqa: BLE001
            return 0
    n = 0
    for repo, rows in parsed_by_repo.items():
        try:
            put_exemplars(repo, exemplars_to_dicts(accepted_findings_by_class(rows)))
            n += 1
        except Exception:  # noqa: BLE001 - an exemplar refresh must not abort ingest
            continue
    return n


def _refresh_practices(parsed_by_repo: dict, put_practices=None) -> int:
    """After ingest, recompute + cache each repo's best-practices (#527) so
    the derived block tracks the freshest corpus. Best-effort per repo."""
    from best_practices import derive_practices, practices_to_dicts
    if put_practices is None:
        try:
            from adapters.pg_install_store import put_repo_practices  # type: ignore
            put_practices = put_repo_practices
        except Exception:  # noqa: BLE001
            return 0
    n = 0
    for repo, rows in parsed_by_repo.items():
        try:
            put_practices(repo, practices_to_dicts(derive_practices(rows)))
            n += 1
        except Exception:  # noqa: BLE001 - a practices refresh must not abort ingest
            continue
    return n


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else _DEFAULT_PATH
    with open(path, encoding="utf-8") as f:
        result = ingest_text(f.read())
    print(f"ledger ingest: {result['ingested']} ingested, {result['skipped']} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
