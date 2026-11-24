"""Measure review outcomes for PRs opened in a UTC date window.

Run make lane-review-metrics REPO=owner/name SINCE=2026-10-01 UNTIL=2026-11-01
with an authenticated GitHub CLI. SINCE is inclusive and UNTIL is exclusive.
The cohort is all PRs whose body has a Loom lane footer and whose opening
date falls in the window. Outcomes reflect the evidence available at report
time, including activity after UNTIL.

The report counts one finding per Grug inline review thread. A living-review
dismissal takes precedence, then a later PR commit changing an anchored line,
then a non-bot reply. All remaining findings are unanswered. A resolved or
outdated flag alone does not prove a fix. A fix requires a GitHub diff that
removes or replaces a right-side anchored line. Comparisons to descendant PR
commits preserve the original coordinates through shifts and catch edits
later reverted. File-level and left-side anchors lack this line evidence.
Category comes from the inline header. The per-rule breakdown shows
individual rules such as high-complexity. Shares with no denominator are null.

Readiness estimates the final head's ready time as the latest of opening,
the draft exit, the findings' handling times, and the current passing head
checks and statuses. It requires no unanswered findings, passing checks
including grug python tests, Chief and Elder, and a merged or currently clean
open PR. GitHub exposes current mergeability, so this estimate uses the final
head's evidence. The median and p90 use linear interpolation. An empty
sample reports null for both.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from itertools import count
from statistics import median, quantiles

_RULE = re.compile(r"<!-- grug-rule:([\w-]+) -->")
_HEADER = re.compile(
    r"\b(critical|high|medium|low|info)\s*\|\s*_([^|]+?)_\s*\|\s*`([^`]+)`",
)
_OUTCOMES = ("fixed", "answered", "dismissed", "unanswered")
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
_LANE_FOOTER = re.compile(r"^Loom ticket \S+, lane \S+", re.MULTILINE)
_REQUIRED_CHECKS = {"Grug - Chief", "Grug - Elder", "grug python tests"}
_COMMITS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      commits(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { commit { oid committedDate } }
      }
    }
  }
}
"""

Runner = Callable[[list[str]], str]


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    rule: str
    outcome: str
    handled_at: str | None


def _non_bot_reply_times(comments: list[dict]) -> dict[int, list[str]]:
    replies: dict[int, list[str]] = defaultdict(list)
    for comment in comments:
        user = comment.get("user") or {}
        login = user.get("login") or ""
        parent = comment.get("in_reply_to_id")
        if (
            parent
            and login
            and user.get("type") != "Bot"
            and not login.lower().endswith("[bot]")
        ):
            replies[parent].append(comment["created_at"])
    return replies


def classify_findings(
    comments: list[dict],
    changed_at: Callable[[dict], str | None],
) -> list[Finding]:
    """Classify the threaded comments from GitHub's flat review-comment list."""
    replies = _non_bot_reply_times(comments)
    findings = []
    for root in comments:
        user = root.get("user") or {}
        if root.get("in_reply_to_id") or user.get("login") != "grug-tribe[bot]":
            continue
        body = root["body"]
        original = body.split("\n<!-- grug-original -->\n", 1)[-1]
        header = _HEADER.search(original.partition("\n")[0])
        rules = _RULE.findall(original)
        if not (rules or header):
            continue
        severity, category, rule = (
            header.groups() if header else ("unknown", "unknown", "unknown")
        )
        reply_at = min(replies[root["id"]], default=None)
        if body.startswith("<!-- grug-state:dismissed -->\n"):
            outcome, handled_at = "dismissed", root["updated_at"]
        elif fixed_at := changed_at(root):
            outcome, handled_at = "fixed", max(fixed_at, root["created_at"])
        elif reply_at:
            outcome, handled_at = "answered", reply_at
        else:
            outcome, handled_at = "unanswered", None
        if handled_at and reply_at:
            handled_at = min(handled_at, reply_at)
        findings.append(
            Finding(
                severity, category, rules[-1] if rules else rule, outcome, handled_at
            )
        )
    return findings


def _removed_lines(file: dict) -> set[int]:
    removed = set()
    old_line = 0
    for line in (file.get("patch") or "").split("\n"):
        if header := _HUNK.match(line):
            old_line = int(header[1])
        elif line.startswith("-"):
            removed.add(old_line)
            old_line += 1
        elif line.startswith(" "):
            old_line += 1
    if len(removed) != file["deletions"]:
        raise ValueError("GitHub omitted part of a finding's file patch")
    return removed


def find_line_change(
    root: dict,
    commits: list[tuple[str, str]],
    compare: Callable[[str, str], dict],
) -> str | None:
    """Find the first descendant PR commit that edits the original right-side range."""
    original, end = root.get("original_commit_id"), root.get("original_line")
    if not original or not end or "LEFT" in (root.get("side"), root.get("start_side")):
        return None
    start = root.get("original_start_line") or end
    for sha, committed_at in commits:
        if sha == original:
            continue
        comparison = compare(original, sha)
        if comparison["status"] != "ahead":
            continue
        files = comparison["files"]
        for file in files:
            if (file.get("previous_filename") or file["filename"]) != root["path"]:
                continue
            if file["status"] == "removed" or any(
                start <= line <= end for line in _removed_lines(file)
            ):
                return committed_at
            break
        else:
            if len(files) >= 300:
                raise ValueError(
                    "GitHub reached its comparison file limit for a finding"
                )
    return None


def _counts(findings: list[Finding]) -> dict:
    counts = Counter(finding.outcome for finding in findings)
    total = len(findings)
    return {
        "findings_total": total,
        **{outcome: counts[outcome] for outcome in _OUTCOMES},
        "fixed_share": counts["fixed"] / total if total else None,
    }


def _breakdown(findings: list[Finding], attribute: str) -> dict:
    groups = defaultdict(list)
    for finding in findings:
        groups[getattr(finding, attribute)].append(finding)
    return {name: _counts(group) for name, group in sorted(groups.items())}


def summarize(findings_by_pr: list[list[Finding]], durations: list[float]) -> dict:
    """Turn fixture or API observations into a single report."""
    findings = [finding for pr in findings_by_pr for finding in pr]
    counts = _counts(findings)
    p90 = (
        quantiles(durations, n=10, method="inclusive")[8]
        if len(durations) > 1
        else next(iter(durations), None)
    )
    return {
        **counts,
        "lane_prs": len(findings_by_pr),
        "unanswered_per_pr": counts["unanswered"] / len(findings_by_pr)
        if findings_by_pr
        else None,
        "ready_prs": len(durations),
        "median_minutes_to_ready": median(durations) if durations else None,
        "p90_minutes_to_ready": p90,
        "by_severity": _breakdown(findings, "severity"),
        "by_category": _breakdown(findings, "category"),
        "by_rule": _breakdown(findings, "rule"),
    }


def _run(argv: list[str]) -> str:
    return subprocess.run(
        argv, check=True, text=True, capture_output=True, timeout=60
    ).stdout


def _api(run: Runner, *args: str):
    return json.loads(run(["gh", "api", *args]))


def _pages(run: Runner, *args: str) -> list:
    pages = _api(run, "--paginate", "--slurp", *args)
    if not pages:
        raise ValueError("GitHub returned no response pages")
    return pages


def _pr_commits(run: Runner, repo: str, number: int) -> list[tuple[str, str]]:
    owner, name = repo.split("/")
    commits = []
    for page in _pages(
        run,
        "graphql",
        "-F",
        f"pr={number}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-f",
        f"query={_COMMITS_QUERY}",
    ):
        if page.get("errors"):
            raise ValueError("GitHub could not list PR commits")
        for node in page["data"]["repository"]["pullRequest"]["commits"]["nodes"]:
            commit = node["commit"]
            commits.append((commit["oid"], commit["committedDate"]))
    return commits


def _passing_head_times(run: Runner, endpoint: str) -> list[str] | None:
    """Collect completion times if the head's check runs and statuses pass."""
    checks = [
        check
        for page in _pages(run, f"{endpoint}/check-runs?per_page=100&filter=latest")
        for check in page["check_runs"]
    ]
    if not _REQUIRED_CHECKS.issubset(check["name"] for check in checks):
        return None
    times = []
    for check in checks:
        passing = {"success"}
        if check["name"] != "Grug - Elder":
            passing.add("neutral")
        if check["name"] not in _REQUIRED_CHECKS:
            passing.add("skipped")
        if check["status"] != "completed" or check["conclusion"] not in passing:
            return None
        times.append(check["completed_at"])

    statuses = {}
    for page in _pages(run, f"{endpoint}/statuses?per_page=100"):
        for status in page:  # Newest first for each legacy status context.
            statuses.setdefault(status["context"], status)
    for status in statuses.values():
        if status["state"] != "success":
            return None
        times.append(status["created_at"])
    return times


def _ready_minutes(
    run: Runner,
    repo_path: str,
    pr: dict,
    opened: str,
    findings: list[Finding],
) -> float | None:
    merged_at = pr.get("merged_at")
    clean_open = pr["state"] == "open" and pr.get("mergeable_state") == "clean"
    if (
        pr["draft"]
        or not (merged_at or clean_open)
        or any(f.handled_at is None for f in findings)
    ):
        return None

    endpoint = f"{repo_path}/commits/{pr['head']['sha']}"
    head_times = _passing_head_times(run, endpoint)
    if head_times is None:
        return None
    times = [opened, *(finding.handled_at for finding in findings), *head_times]
    for page in _pages(run, f"{repo_path}/issues/{pr['number']}/timeline?per_page=100"):
        times.extend(
            event["created_at"]
            for event in page
            if event["event"] == "ready_for_review"
        )
    ready_at = max(times)
    if merged_at and ready_at > merged_at:
        return None
    return (
        datetime.fromisoformat(ready_at) - datetime.fromisoformat(opened)
    ).total_seconds() / 60


def _lane_prs(run: Runner, repo_path: str, since: str, until: str):
    for page in count(1):
        prs = _api(
            run,
            f"{repo_path}/pulls?state=all&sort=created&direction=desc&per_page=100&page={page}",
        )
        for pr in prs:
            opened = pr["created_at"][:10]
            if opened < since:
                return
            if opened < until and _LANE_FOOTER.search(pr["body"] or ""):
                yield pr
        if len(prs) < 100:
            return


def _measure_pr(run: Runner, repo: str, pr: dict) -> tuple[list[Finding], float | None]:
    number = pr["number"]
    repo_path = f"repos/{repo}"
    detail = _api(run, f"{repo_path}/pulls/{number}")
    head = detail["head"]["sha"]
    pages = _pages(run, f"{repo_path}/pulls/{number}/comments?per_page=100")

    @cache
    def commits() -> list[tuple[str, str]]:
        history = _pr_commits(run, repo, number)
        if not any(sha == head for sha, _ in history):
            raise ValueError("PR head changed while listing its commits")
        return history

    @cache
    def compare(original: str, later: str) -> dict:
        return _api(run, f"{repo_path}/compare/{original}...{later}?per_page=1")

    def changed_at(root: dict) -> str | None:
        if root.get("original_commit_id") == head:
            return None
        return find_line_change(root, commits(), compare)

    findings = classify_findings(
        [comment for page in pages for comment in page], changed_at
    )
    minutes = _ready_minutes(
        run, repo_path, {**detail, "number": number}, pr["created_at"], findings
    )
    return findings, minutes


def main(argv: list[str] | None = None, *, run: Runner = _run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--repo", required=True, help="GitHub repository in owner/name form"
    )
    parser.add_argument(
        "--since",
        type=date.fromisoformat,
        required=True,
        help="First UTC date, inclusive",
    )
    parser.add_argument(
        "--until",
        type=date.fromisoformat,
        required=True,
        help="Last UTC date, exclusive",
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must use owner/name form")
    if args.since >= args.until:
        parser.error("--since must be earlier than --until")

    findings_by_pr = []
    durations = []
    try:
        for pr in _lane_prs(
            run, f"repos/{args.repo}", args.since.isoformat(), args.until.isoformat()
        ):
            findings, minutes = _measure_pr(run, args.repo, pr)
            findings_by_pr.append(findings)
            if minutes is not None:
                durations.append(minutes)
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        print(
            f"Could not measure lane review metrics ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(summarize(findings_by_pr, durations), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
