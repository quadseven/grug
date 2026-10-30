"""Ingest the review-findings ledger into the store (#361 slice 1): the
committed logs/review-ledger.jsonl, or a PR comment's Findings Ledger table.

Runs as a one-shot (CLI or a k8s Job): read the committed corpus, skip
malformed lines, and upsert each finding as a first-class grug_kv row via
pg_install_store.put_ledger_row. Idempotent - re-running heals rows and
adds any new ones. The `seq` disambiguates findings that share
(class, pr, reviewer) within a repo.

    python ingest_ledger.py [path-to-jsonl]   # default: logs/review-ledger.jsonl
"""

from __future__ import annotations

import json
import logging
import sys

from ledger import parse_comment_table, parse_row

log = logging.getLogger("grug.ingest_ledger")

_DEFAULT_PATH = "logs/review-ledger.jsonl"


def ingest_text(
    text: str, put=None, put_practices=None, put_exemplars=None
) -> dict[str, object]:
    """Parse + persist every valid ledger line. `put`/`put_practices`/
    `put_exemplars` default to the store adapter but are injectable for
    tests. Returns {ingested, skipped, repos_refreshed,
    exemplars_refreshed}."""
    rows: list[dict] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            skipped += 1
    return _ingest_rows(rows, skipped, put, put_practices, put_exemplars)


def ingest_comment_table(
    body: str, repo: str, pr: int | None,
    put=None, put_practices=None, put_exemplars=None,
) -> dict[str, object]:
    """Persist the Findings Ledger table of one PR comment body (#361's
    second source, beside the JSONL file). Same store, same result shape as
    `ingest_text`; a body with no ledger table ingests nothing."""
    rows = parse_comment_table(body, repo, pr)
    return _ingest_rows(rows, 0, put, put_practices, put_exemplars)


def _ingest_rows(
    rows: list[dict], skipped: int, put, put_practices, put_exemplars
) -> dict[str, object]:
    """Validate, persist and refresh the per-repo derived caches for raw
    row dicts. `skipped` carries the caller's pre-parse drops."""
    if put is None:
        from adapters.pg_install_store import put_ledger_row  # type: ignore
        put = put_ledger_row
    ingested = 0
    parsed_by_repo: dict[str, list] = {}
    for row in rows:
        lr = parse_row(row)
        if lr is None:  # reuse the same validity gate
            skipped += 1
            continue
        put(row)  # key is content-derived in the store; ingest order irrelevant
        parsed_by_repo.setdefault(lr.repo, []).append(lr)
        ingested += 1
    refreshed = _refresh_practices(parsed_by_repo, put_practices)
    exemplars_refreshed = _refresh_exemplars(parsed_by_repo, put_exemplars)
    return {
        "ingested": ingested,
        "skipped": skipped,
        "repos_refreshed": refreshed,
        "exemplars_refreshed": exemplars_refreshed,
    }


def _refresh_exemplars(parsed_by_repo: dict, put_exemplars=None) -> int:
    """After ingest, recompute + cache each repo's few-shot exemplars
    (#538) beside the practices refresh. Best-effort per repo."""
    from few_shot import exemplars_from_rows, exemplars_to_dicts
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
            put_exemplars(
                repo,
                exemplars_to_dicts(
                    exemplars_from_rows(accepted_findings_by_class(rows))
                ),
            )
            n += 1
        except Exception as e:  # noqa: BLE001 - must not abort ingest, but SAY so
            log.warning(
                "exemplar_refresh_failed repo=%s kind=%s", repo, type(e).__name__
            )
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
        except Exception as e:  # noqa: BLE001 - must not abort ingest, but SAY so
            log.warning(
                "practices_refresh_failed repo=%s kind=%s", repo, type(e).__name__
            )
            continue
    return n


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else _DEFAULT_PATH
    with open(path, encoding="utf-8") as f:
        result = ingest_text(f.read())
    print(
        f"ledger ingest: {result['ingested']} ingested, "
        f"{result['skipped']} skipped, "
        f"{result['repos_refreshed']} practices + "
        f"{result['exemplars_refreshed']} exemplar repo-caches refreshed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
