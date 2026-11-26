"""Emit findings ledger rows from a captured escaped-defect review snapshot."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from escape_classifier import ReviewRecord, ReviewSource
from escape_ledger import CorrectingPull, sweep_escape_rows
from escape_trace import FindingRef, PullRef, TraceSources


def _time(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError("snapshot timestamps require a UTC offset")
    return timestamp


def _strings(values: list[str], field: str) -> tuple[str, ...]:
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{field} must be an array of strings")
    return tuple(values)


def _plan(snapshot: dict, ledger: list[dict]) -> tuple[list[dict], list[int]]:
    repo = snapshot["repo"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo, re.ASCII):
        raise ValueError("snapshot repo must be OWNER/NAME")
    pulls, corrections = {}, []
    for entry in snapshot["pulls"]:
        number = entry["number"]
        if type(number) is not int or number <= 0 or number in pulls:
            raise ValueError("snapshot pull numbers must be unique positive integers")
        pulls[number] = PullRef(
            number=number, title=entry.get("title", ""), body=entry.get("body", ""),
            merged=True, files=_strings(entry["files"], "files"),
            commit_messages=_strings(entry.get("commit_messages", []), "commit_messages"),
        )
        corrections.append(CorrectingPull(number, _time(entry["merged_at"])))
    findings, reviews = {}, {}
    for key, evidence in snapshot["review_evidence"].items():
        number = int(key)
        if number not in pulls or number in reviews:
            raise ValueError("review evidence must name one pull in the snapshot")
        findings[number] = tuple(FindingRef(number, **item) for item in evidence["findings"])
        reviews[number] = tuple(
            ReviewRecord(
                number, ReviewSource(item["source"]),
                _strings(item["seen_paths"], "seen_paths"),
            )
            for item in evidence["reviews"]
        )
    commit_pulls = snapshot.get("commit_pulls", {})
    sources = TraceSources(pulls.get, commit_pulls.get, lambda number: findings.get(number, ()))
    return sweep_escape_rows(
        repo, corrections, sources, reviews, ledger, now=_time(snapshot["as_of"])
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="captured PR and review evidence JSON")
    parser.add_argument("--ledger", type=Path, help="existing findings ledger JSONL")
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        ledger = []
        if args.ledger is not None:
            with args.ledger.open(encoding="utf-8") as stream:
                ledger = [json.loads(line) for line in stream if line.strip()]
        if not isinstance(snapshot, dict) or any(not isinstance(row, dict) for row in ledger):
            raise ValueError("snapshot and ledger rows must be JSON objects")
        rows, missing = _plan(snapshot, ledger)
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        parser.error(f"invalid sweep input ({type(exc).__name__}): {exc}")
    for row in rows:
        print(json.dumps(row))
    if missing:
        print(f"Corrections without review evidence: {', '.join(map(str, missing))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
