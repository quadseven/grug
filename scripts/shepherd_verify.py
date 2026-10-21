"""Check explicit PR acceptance claims against post-deploy observations.

The query callback returns a count of matching observations from a source.
The caller supplies a known-live baseline for each source. No baseline or an
empty baseline leaves an absent target unverifiable. Each attempt is retained
in the evidence so a verdict can be challenged with the underlying counts.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_HEADING = re.compile(r"^#{1,6}\s+acceptance criteria\s*$", re.IGNORECASE)
_NEXT_HEADING = re.compile(r"^(#{1,6})\s+")
_CRITERION = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s*)?(.+)$")
_OBSERVABLE = re.compile(
    r"\b(metric|log event|endpoint|GET)\s+"
    r"(?:`([^`]+)`|\"([^\"]+)\"|(https?://[^\s]+|[A-Za-z][\w.:-]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Observable:
    source: str
    target: str
    criterion: str


@dataclass(frozen=True)
class Verdict:
    status: str
    evidence: str


def parse_observables(body: str) -> list[Observable]:
    """Extract only explicitly named signals from PR acceptance bullets."""
    heading_level = 0
    fenced = False
    found: list[Observable] = []
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if _HEADING.match(line):
            heading_level = len(line) - len(line.lstrip("#"))
            continue
        heading = _NEXT_HEADING.match(line)
        if heading and heading_level and len(heading.group(1)) <= heading_level:
            heading_level = 0
        if not heading_level:
            continue
        bullet = _CRITERION.match(line)
        if not bullet:
            continue
        criterion = bullet.group(1)
        for match in _OBSERVABLE.finditer(criterion):
            label = match.group(1).lower()
            source = (
                "endpoint"
                if label in {"get", "endpoint"}
                else label.replace(" event", "")
            )
            target = match.group(2) or match.group(3) or match.group(4).rstrip(".,;")
            found.append(Observable(source, target, criterion))
    return found


Query = Callable[[str, str], int]


def _check_one(
    claim: Observable,
    query: Query,
    baseline: str | None,
    attempts: int,
    pause: Callable[[float], None],
    retry_seconds: float,
) -> Verdict:
    if claim.source == "endpoint" and baseline is not None:
        target_url = urlsplit(claim.target)
        baseline_url = urlsplit(baseline)
        if (target_url.scheme, target_url.netloc) != (
            baseline_url.scheme,
            baseline_url.netloc,
        ):
            raise ValueError("endpoint baseline must share the target's same origin")
    records: list[str] = []
    live_attempts = 0
    for attempt in range(1, attempts + 1):
        count = query(claim.source, claim.target)
        if count < 0:
            raise ValueError("query count must not be negative")
        if count:
            records.append(f"attempt {attempt}: target {claim.target!r} count={count}")
            return Verdict("observed", "; ".join(records))
        if baseline is None:
            records.append(
                f"attempt {attempt}: target {claim.target!r} count=0; no baseline configured"
            )
        else:
            baseline_count = query(claim.source, baseline)
            if baseline_count < 0:
                raise ValueError("query count must not be negative")
            live_attempts += bool(baseline_count)
            records.append(
                f"attempt {attempt}: target {claim.target!r} count=0; "
                f"baseline {baseline!r} count={baseline_count}"
            )
        if attempt < attempts:
            pause(retry_seconds)
    status = (
        "missing" if attempts >= 2 and live_attempts == attempts else "unverifiable"
    )
    if status == "unverifiable" and baseline is not None and live_attempts < attempts:
        records.append("source freshness unproven; possible ingestion lag")
    return Verdict(status, "; ".join(records))


def verify_claims(
    body: str,
    query: Query,
    *,
    baselines: Mapping[str, str],
    attempts: int = 2,
    retry_seconds: float = 30,
    pause: Callable[[float], None] = time.sleep,
) -> Verdict:
    """Report observed, missing, or unverifiable with query evidence."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if retry_seconds < 0:
        raise ValueError("retry_seconds must not be negative")
    claims = parse_observables(body)
    if not claims:
        return Verdict("unverifiable", "PR acceptance criteria name no observable")
    outcomes = [
        (
            claim,
            _check_one(
                claim,
                query,
                baselines.get(claim.source),
                attempts,
                pause,
                retry_seconds,
            ),
        )
        for claim in claims
    ]
    evidence = "\n".join(
        f"criterion {claim.criterion!r}; {claim.source} {claim.target!r}: "
        f"{verdict.status}; {verdict.evidence}"
        for claim, verdict in outcomes
    )
    statuses = {verdict.status for _, verdict in outcomes}
    if "missing" in statuses:
        status = "missing"
    elif "unverifiable" in statuses:
        status = "unverifiable"
    else:
        status = "observed"
    return Verdict(status, evidence)


def _run(argv: list[str]) -> str:
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode:
        detail = result.stderr.strip() or f"command exited {result.returncode}"
        raise RuntimeError(detail)
    return result.stdout.strip()


def _body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text()
    raw = _run(["gh", "pr", "view", str(args.pr), "-R", args.repo, "--json", "body"])
    return json.loads(raw)["body"] or ""


def _baselines(raw: list[str]) -> dict[str, str]:
    baselines: dict[str, str] = {}
    for entry in raw:
        source, separator, name = entry.partition("=")
        if not separator or source not in {"metric", "log", "endpoint"} or not name:
            raise ValueError(f"invalid baseline {entry!r}; use source=signal")
        baselines[source] = name
    return baselines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify stated PR claims after deploy")
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--body-file", help="PR body file")
    body.add_argument("--pr", type=int, help="PR number to fetch with gh")
    parser.add_argument("--repo", help="OWNER/NAME for --pr")
    parser.add_argument(
        "--query-command",
        nargs="+",
        required=True,
        help="command prefix; called with source, signal, start, end, criterion",
    )
    parser.add_argument(
        "--baseline", action="append", default=[], metavar="SOURCE=SIGNAL"
    )
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--retry-seconds", type=float, default=30)
    parser.add_argument(
        "--since-epoch",
        type=int,
        required=True,
        help="deploy completion time as Unix seconds",
    )
    args = parser.parse_args(argv)
    if args.pr is not None and not args.repo:
        parser.error("--pr requires --repo")
    if args.since_epoch < 0 or args.since_epoch > time.time():
        parser.error("--since-epoch must be a past Unix timestamp")
    try:
        pr_body = _body(args)
        baselines = _baselines(args.baseline)
        criteria = {
            (claim.source, claim.target): claim.criterion
            for claim in parse_observables(pr_body)
        }

        def query(source: str, signal: str) -> int:
            raw = _run(
                [
                    *args.query_command,
                    source,
                    signal,
                    str(args.since_epoch),
                    str(int(time.time())),
                    criteria.get((source, signal), ""),
                ]
            )
            count = int(raw)
            if count < 0:
                raise ValueError("query count must not be negative")
            return count

        verdict = verify_claims(
            pr_body,
            query,
            baselines=baselines,
            attempts=args.attempts,
            retry_seconds=args.retry_seconds,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.TimeoutExpired,
        KeyError,
    ) as exc:
        print(f"unverifiable: lookup failed: {exc}")
        return 2
    print(f"{verdict.status}: {verdict.evidence}")
    return {"observed": 0, "missing": 1, "unverifiable": 2}[verdict.status]


if __name__ == "__main__":
    sys.exit(main())
