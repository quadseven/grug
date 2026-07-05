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
from collections import Counter

from ledger import parse_row

_DEFAULT_PATH = "logs/review-ledger.jsonl"


def ingest_text(text: str, put=None) -> dict[str, int]:
    """Parse + persist every valid ledger line. `put` defaults to the store
    adapter but is injectable for tests. Returns {ingested, skipped}."""
    if put is None:
        from adapters.pg_install_store import put_ledger_row  # type: ignore
        put = put_ledger_row
    seq: Counter = Counter()
    ingested = skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if parse_row(row) is None:  # reuse the same validity gate
            skipped += 1
            continue
        key = (row["repo"], row["class"], row["pr"], row["reviewer"])
        put(row, seq[key])
        seq[key] += 1
        ingested += 1
    return {"ingested": ingested, "skipped": skipped}


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else _DEFAULT_PATH
    with open(path, encoding="utf-8") as f:
        result = ingest_text(f.read())
    print(f"ledger ingest: {result['ingested']} ingested, {result['skipped']} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
