"""shepherd.py

This module implements the Shepherd functionality to verify that a merged PR
actually deployed, and to escalate a broken chain once, naming the stage that
broke.

It is intentionally minimal for the current exercise but should be
expanded to interact with the registry and the k8s deployments.

The CLI reports a digest mismatch as merged-but-not-deployed and missing
reference evidence as unverified.

Escalation (T832): a chain is a sequence of named stages, each checked by a
stage check returning a StageVerdict. The first broken stage is reported
exactly once per break episode: the shepherd notifies, naming the stage and
the evidence that says so, then stays quiet on later runs while the same
break persists. When the chain recovers (every stage ok after a break), a
recovery notification is sent so a stale alarm cannot linger. Only success
counts as success; a cancelled, failed, or missing-evidence stage is broken.

PR State Model (T829):
A single honest state model for every open PR across CI and all four bot comment
surfaces. Distinguishes an empty rollup (no checks reported yet) from all-checks-passed.
Reads all four bot comment surfaces:
1. unresolved review threads
2. unanswered review bodies
3. non-passing bot check-runs
4. issue-level bot comments
A finding on any surface marks the PR not-ready. Reports time-since-ready (duration)
and outputs a queue ordered by ready longest. Takes no action on any PR (reports only).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_STATE_PATH = os.environ.get("GRUG_SHEPHERD_STATE", ".shepherd-state.json")


@dataclass(frozen=True)
class StageVerdict:
    """The outcome of checking one link in a chain.

    ``ok`` is True only when the link succeeded. A failed build, a reference
    that was never bumped, a promised observable that never appeared, or
    silently cancelled scheduled work all count as broken: only success is
    success.
    """

    name: str
    ok: bool
    evidence: str


StageCheck = Callable[[], StageVerdict]


@dataclass(frozen=True)
class ChainState:
    """Persistent memory of whether a break has already been reported."""

    broken_stage: str | None
    notified: bool


def evaluate_chain(checks: list[tuple[str, StageCheck]]) -> StageVerdict | None:
    """Return the first broken stage verdict, or None when every link holds.

    Stages run in order and the first failure wins, so a broken chain names
    exactly one stage with the evidence that says so.
    """
    for _, check in checks:
        verdict = check()
        if not verdict.ok:
            return verdict
    return None


def load_chain_state(path: str = DEFAULT_STATE_PATH) -> ChainState:
    """Load the last-known chain state, treating missing state as fresh.

    Corrupt state is never allowed to hide a break: it resets to
    not-notified so the next broken run is reported. Genuine read errors
    (permissions, and the like) still propagate.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return ChainState(broken_stage=None, notified=False)
    except json.JSONDecodeError:
        return ChainState(broken_stage=None, notified=False)
    if not isinstance(data, dict):
        return ChainState(broken_stage=None, notified=False)
    return ChainState(
        broken_stage=data.get("broken_stage"),
        notified=bool(data.get("notified")),
    )


def save_chain_state(path: str, state: ChainState) -> None:
    """Persist the chain state so the next run knows what was already said."""
    data = {}
    try:
        with open(path) as fh:
            existing = json.load(fh)
            if isinstance(existing, dict):
                data = existing
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    data["broken_stage"] = state.broken_stage
    data["notified"] = state.notified
    with open(path, "w") as fh:
        json.dump(data, fh)


def load_ready_tracker(path: str = DEFAULT_STATE_PATH) -> dict[int, float]:
    """Load PR ready timestamps from state so ready duration persists across runs."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("pr_ready_since", {})
    if not isinstance(raw, dict):
        return {}
    tracker = {}
    for k, v in raw.items():
        try:
            tracker[int(k)] = float(v)
        except (ValueError, TypeError):
            continue
    return tracker


def save_ready_tracker(path: str, tracker: dict[int, float]) -> None:
    """Persist PR ready timestamps so time-since-ready is tracked monotonically."""
    data = {}
    try:
        with open(path) as fh:
            existing = json.load(fh)
            if isinstance(existing, dict):
                data = existing
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    data["pr_ready_since"] = {str(k): float(v) for k, v in tracker.items()}
    with open(path, "w") as fh:
        json.dump(data, fh)


Notify = Callable[[str, str | None, str], None]


def escalate_once(
    checks: list[tuple[str, StageCheck]],
    *,
    state_path: str = DEFAULT_STATE_PATH,
    notify: Notify,
) -> str:
    """Evaluate the chain and report at most once per break.

    Returns ``"healthy"``, ``"broken"``, or ``"recovered"``.

    A broken chain produces exactly one notification naming the stage that
    broke and the evidence. Later runs while the same break persists stay
    quiet. When the chain recovers, a recovery notification is sent. Only
    success counts as success; a cancelled or otherwise non-ok stage is
    broken.
    """
    state = load_chain_state(state_path)
    broken = evaluate_chain(checks)

    if broken is None:
        if state.broken_stage is not None:
            notify(
                "chain recovered",
                None,
                f"previous break at {state.broken_stage!r}",
            )
            save_chain_state(state_path, ChainState(broken_stage=None, notified=False))
            return "recovered"
        save_chain_state(state_path, ChainState(broken_stage=None, notified=False))
        return "healthy"

    if state.notified and state.broken_stage == broken.name:
        return "broken"

    notify(f"chain broken at {broken.name!r}", broken.name, broken.evidence)
    save_chain_state(state_path, ChainState(broken_stage=broken.name, notified=True))
    return "broken"


def deployment_stage_verdicts(service: str) -> list[StageVerdict]:
    """Stage verdicts for the deployed-versus-published image chain.

    The chain has one load-bearing link: the deployed image must match the
    published image. Missing reference evidence, a missing deployment, or a
    digest mismatch are each broken, and the verdict names the link and the
    evidence.
    """
    _, deployed_digest = get_latest_digest(service)
    _, pushed_digest = get_latest_push_digest(service)

    if not pushed_digest:
        return [
            StageVerdict(
                "reference",
                False,
                "no published image digest; reference never bumped",
            )
        ]
    if not deployed_digest:
        return [
            StageVerdict(
                "deploy",
                False,
                "published image present but nothing deployed; "
                "scheduled work silently cancelled",
            )
        ]
    if deployed_digest != pushed_digest:
        return [
            StageVerdict(
                "deploy",
                False,
                f"deployed digest {deployed_digest} differs from published "
                f"{pushed_digest}; merged-but-not-deployed",
            )
        ]
    return [
        StageVerdict(
            "deploy",
            True,
            f"deployed digest {deployed_digest} matches published {pushed_digest}",
        )
    ]


def get_latest_digest(service: str) -> tuple[str, str]:
    """Return the image tag and digest of the deployed service.

    In a real implementation this would query the Kubernetes API to
    fetch the image used by the deployment.

    Returns a tuple of (tag, digest). The default stub returns empty
    values.
    """
    return "", ""


def get_latest_push_digest(service: str) -> tuple[str, str]:
    """Return the image tag and digest that was pushed for the PR.

    This would normally interrogate the container registry.
    The stub returns empty values.
    """
    return "", ""


def check_deployment(service: str) -> bool:
    """Return True if the deployed image matches the pushed image.

    The function compares the digests of the latest deployed
    instance and the most recent tag pushed for the service.
    """
    verdicts = deployment_stage_verdicts(service)
    return bool(verdicts) and all(v.ok for v in verdicts)


def deployment_chain_verdict(service: str) -> StageVerdict:
    """Collapse the stage verdicts into one: the first failure, else the last.

    An empty verdict list is itself broken, so callers never index into it.
    """
    verdicts = deployment_stage_verdicts(service)
    if not verdicts:
        return StageVerdict("deploy", False, "no stage verdicts produced")
    return next((v for v in verdicts if not v.ok), verdicts[-1])


def deployment_status(service: str) -> str:
    """Distinguish a stale deployment from missing reference evidence."""
    _, deployed_digest = get_latest_digest(service)
    _, pushed_digest = get_latest_push_digest(service)
    if not deployed_digest or not pushed_digest:
        return "unverified"
    if deployed_digest != pushed_digest:
        return "merged-but-not-deployed"
    return "deployed"


def main(service: str = "webhook", *args: str) -> int:
    """Shepherd entrypoint.

    When called with a service name ('webhook', etc.), checks deployment status.
    When called with 'pr-queue' (or flags), reports the PR queue across CI and
    bot surfaces. Takes no action on any PR -- reports only.
    """
    if service == "pr-queue" or service.startswith("--"):
        import argparse

        parser = argparse.ArgumentParser(
            description="Shepherd PR state model and queue report"
        )
        parser.add_argument(
            "--repo", required=True, help="GitHub repository OWNER/NAME"
        )
        parser.add_argument(
            "--state-path",
            default=DEFAULT_STATE_PATH,
            help="Path to persistent state file",
        )
        cli_args = [service] + list(args) if service != "pr-queue" else list(args)
        parsed_args = parser.parse_args(cli_args)
        report = report_shepherd_pr_queue(
            repo=parsed_args.repo,
            state_path=parsed_args.state_path,
        )
        print(report)
        return 0

    if service == "escalate-deployment":
        target = args[0] if args else "webhook"

        def _notify(message, stage, evidence):
            print(f"{message}: {evidence}", file=sys.stderr)

        checks = [("deploy", lambda: deployment_chain_verdict(target))]
        result = escalate_once(checks, notify=_notify)
        return 0 if result != "broken" else 1

    status = deployment_status(service)
    if status == "deployed":
        print(f"{service} is deployed correctly")
        return 0
    if status == "merged-but-not-deployed":
        print(
            f"MERGED-BUT-NOT-DEPLOYED: {service} differs from the published image",
            file=sys.stderr,
        )
    else:
        print(
            f"UNVERIFIED: {service} is missing a published or deployed digest",
            file=sys.stderr,
        )
    return 1


# ==============================================================================
# PR State Model across CI and all four bot comment surfaces (T829)
# ==============================================================================

Runner = Callable[[list[str]], str]


def _subprocess_runner(argv: list[str]) -> str:
    """Execute a command and return stdout. Raises RuntimeError on failure."""
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{argv[0]} exited {proc.returncode}")
    return proc.stdout


def _normalize_check_status(status: str) -> str:
    """Normalize raw check status/conclusion into passed, failed, or pending."""
    s = (status or "").lower()
    if s in ("success", "pass", "passed"):
        return "passed"
    if s in ("failure", "failed", "error", "cancelled", "timed_out", "action_required"):
        return "failed"
    if s in ("pending", "in_progress", "queued", "waiting", "requested"):
        return "pending"
    return "pending" if not s else s


def _parse_iso_epoch(ts: str | None) -> float | None:
    """Parse an ISO 8601 timestamp string into epoch seconds."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class CICheck:
    """A CI check reporting for a PR commit.

    Tracks status (passed, failed, pending) and duration in seconds (run time
    or how long it has been pending).
    """

    name: str
    status: str
    duration: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _normalize_check_status(self.status))


@dataclass(frozen=True)
class BotFinding:
    """A finding raised on one of the four bot comment surfaces."""

    surface: str  # review_thread, review_body, bot_check_run, issue_comment
    identifier: str
    bot_name: str
    message: str = ""
    created_at: float | None = None


@dataclass(frozen=True)
class BotSurfaces:
    """Findings across all four bot comment surfaces.

    The four surfaces are:
    1. unresolved review threads
    2. unanswered review bodies
    3. non-passing bot check-runs
    4. issue-level bot comments
    """

    unresolved_review_threads: tuple[BotFinding, ...] = ()
    unanswered_review_bodies: tuple[BotFinding, ...] = ()
    non_passing_bot_check_runs: tuple[BotFinding, ...] = ()
    issue_comments: tuple[BotFinding, ...] = ()

    @property
    def is_clean(self) -> bool:
        """True only when every bot surface has zero findings."""
        return (
            len(self.unresolved_review_threads) == 0
            and len(self.unanswered_review_bodies) == 0
            and len(self.non_passing_bot_check_runs) == 0
            and len(self.issue_comments) == 0
        )

    @property
    def total_findings(self) -> int:
        """Total findings across all four surfaces."""
        return (
            len(self.unresolved_review_threads)
            + len(self.unanswered_review_bodies)
            + len(self.non_passing_bot_check_runs)
            + len(self.issue_comments)
        )

    @property
    def all_findings(self) -> tuple[BotFinding, ...]:
        return (
            self.unresolved_review_threads
            + self.unanswered_review_bodies
            + self.non_passing_bot_check_runs
            + self.issue_comments
        )


@dataclass(frozen=True)
class PRState:
    """State model for an open PR across CI and bot surfaces."""

    pr_number: int
    title: str = ""
    checks: tuple[CICheck, ...] = ()
    bot_surfaces: BotSurfaces = BotSurfaces()
    ready_since: float | None = None

    @property
    def ci_status(self) -> str:
        """Rollup status of CI checks.

        Distinguishes 'empty' (no checks reported yet) from 'passed'
        (all checks passed). Returns 'failed' if any check failed,
        and 'pending' if any check is still pending.
        """
        if not self.checks:
            return "empty"
        if any(c.status == "failed" for c in self.checks):
            return "failed"
        if any(c.status == "pending" for c in self.checks):
            return "pending"
        return "passed"

    @property
    def is_ci_green(self) -> bool:
        """True if and only if checks have reported and all passed."""
        return self.ci_status == "passed"

    @property
    def is_bot_clean(self) -> bool:
        """True if all four bot comment surfaces have zero findings."""
        return self.bot_surfaces.is_clean

    @property
    def is_ready(self) -> bool:
        """True only when simultaneously CI green and bot clean."""
        return self.is_ci_green and self.is_bot_clean

    def time_since_ready(self, now: float) -> float | None:
        """Duration in seconds since the PR became ready, or None if not ready."""
        if not self.is_ready or self.ready_since is None:
            return None
        return max(0.0, now - self.ready_since)


@dataclass(frozen=True)
class PRQueueItem:
    """An item in the Shepherd PR queue."""

    pr_number: int
    title: str
    is_ready: bool
    time_since_ready: float | None
    ci_status: str
    findings_count: int
    summary: str


def format_duration(seconds: float | None) -> str:
    """Format duration in seconds to a concise string (e.g. 2h 15m, 45s)."""
    if seconds is None:
        return "not ready"
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    rem_secs = secs % 60
    if mins < 60:
        if rem_secs > 0:
            return f"{mins}m {rem_secs}s"
        return f"{mins}m"
    hours = mins // 60
    rem_mins = mins % 60
    if hours < 24:
        if rem_mins > 0:
            return f"{hours}h {rem_mins}m"
        return f"{hours}h"
    days = hours // 24
    rem_hours = hours % 24
    if rem_hours > 0:
        return f"{days}d {rem_hours}h"
    return f"{days}d"


def _summarize_pr_state(pr: PRState, duration: float | None) -> str:
    """Construct a brief summary line of PR readiness status."""
    if pr.is_ready:
        dur = format_duration(duration)
        check_count = len(pr.checks)
        return (
            f"ready for {dur} "
            f"(CI: {check_count}/{check_count} passed, bot surfaces clean)"
        )

    reasons = []
    if pr.ci_status == "empty":
        reasons.append("CI empty rollup (no checks reported yet)")
    elif pr.ci_status == "failed":
        failed = [c.name for c in pr.checks if c.status == "failed"]
        reasons.append(f"CI failed: {', '.join(failed)}")
    elif pr.ci_status == "pending":
        pending_desc = [
            f"{c.name} ({format_duration(c.duration)})"
            for c in pr.checks
            if c.status == "pending"
        ]
        reasons.append(f"CI pending: {', '.join(pending_desc)}")

    if not pr.bot_surfaces.is_clean:
        findings_parts = []
        if pr.bot_surfaces.unresolved_review_threads:
            findings_parts.append(
                f"{len(pr.bot_surfaces.unresolved_review_threads)} review thread(s)"
            )
        if pr.bot_surfaces.unanswered_review_bodies:
            findings_parts.append(
                f"{len(pr.bot_surfaces.unanswered_review_bodies)} review body(ies)"
            )
        if pr.bot_surfaces.non_passing_bot_check_runs:
            findings_parts.append(
                f"{len(pr.bot_surfaces.non_passing_bot_check_runs)} non-passing bot check(s)"
            )
        if pr.bot_surfaces.issue_comments:
            findings_parts.append(
                f"{len(pr.bot_surfaces.issue_comments)} issue comment(s)"
            )
        reasons.append(f"bot findings: {', '.join(findings_parts)}")

    return "; ".join(reasons)


def build_pr_queue(
    prs: list[PRState],
    now: float,
    *,
    ready_only: bool = False,
) -> list[PRQueueItem]:
    """Order open PRs by how long they have been simultaneously green and clean.

    PRs ready the longest come first (descending duration). Non-ready PRs follow.
    Takes no action on any PR.
    """
    ready_items: list[PRQueueItem] = []
    not_ready_items: list[PRQueueItem] = []

    for pr in prs:
        dur = pr.time_since_ready(now)
        item = PRQueueItem(
            pr_number=pr.pr_number,
            title=pr.title,
            is_ready=pr.is_ready,
            time_since_ready=dur,
            ci_status=pr.ci_status,
            findings_count=pr.bot_surfaces.total_findings,
            summary=_summarize_pr_state(pr, dur),
        )
        if pr.is_ready:
            ready_items.append(item)
        else:
            not_ready_items.append(item)

    ready_items.sort(key=lambda x: x.time_since_ready or 0.0, reverse=True)
    if ready_only:
        return ready_items
    return ready_items + not_ready_items


def format_queue_report(queue: list[PRQueueItem]) -> str:
    """Format the PR queue into an honest human-readable report.

    Takes no action on any PR -- reports only, does not merge or modify.
    """
    lines = ["Shepherd PR Queue (ordered by ready longest):"]
    if not queue:
        lines.append("  (no open PRs)")
        return "\n".join(lines)

    ready_count = sum(1 for item in queue if item.is_ready)
    lines.append(f"  {ready_count} of {len(queue)} open PR(s) ready")
    lines.append("")

    for item in queue:
        if item.is_ready:
            dur_str = format_duration(item.time_since_ready)
            lines.append(f"- PR #{item.pr_number}: READY for {dur_str} - {item.title}")
            lines.append(f"    {item.summary}")
        else:
            lines.append(f"- PR #{item.pr_number}: NOT READY - {item.title}")
            lines.append(f"    {item.summary}")

    return "\n".join(lines)


def update_ready_tracker(
    prs: list[PRState],
    tracker: dict[int, float],
    now: float,
) -> list[PRState]:
    """Update ready timestamps across PR runs.

    Preserves the initial ready timestamp while a PR stays ready. Clears
    the timestamp if the PR becomes not ready.
    """
    updated: list[PRState] = []
    for pr in prs:
        if pr.is_ready:
            if pr.pr_number in tracker:
                ready_since = tracker[pr.pr_number]
            else:
                ready_since = now
                tracker[pr.pr_number] = now
            updated.append(
                PRState(
                    pr_number=pr.pr_number,
                    title=pr.title,
                    checks=pr.checks,
                    bot_surfaces=pr.bot_surfaces,
                    ready_since=ready_since,
                )
            )
        else:
            if pr.pr_number in tracker:
                del tracker[pr.pr_number]
            updated.append(
                PRState(
                    pr_number=pr.pr_number,
                    title=pr.title,
                    checks=pr.checks,
                    bot_surfaces=pr.bot_surfaces,
                    ready_since=None,
                )
            )
    return updated


def parse_ci_checks(
    check_runs_data: list[dict],
    now: float | None = None,
) -> list[CICheck]:
    """Parse check-runs data into normalized CICheck list with durations."""
    checks: list[CICheck] = []
    current_time = now if now is not None else datetime.now(timezone.utc).timestamp()

    for item in check_runs_data:
        name = str(item.get("name") or "unknown")
        status_raw = str(item.get("status") or "")
        conclusion_raw = str(item.get("conclusion") or "")

        if status_raw == "completed":
            effective_status = conclusion_raw or "success"
        else:
            effective_status = "pending"

        started_at = _parse_iso_epoch(item.get("started_at"))
        completed_at = _parse_iso_epoch(item.get("completed_at"))

        if completed_at is not None and started_at is not None:
            duration = max(0.0, completed_at - started_at)
        elif started_at is not None:
            duration = max(0.0, current_time - started_at)
        else:
            duration = 0.0

        checks.append(
            CICheck(
                name=name,
                status=effective_status,
                duration=duration,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
    return checks


def _find_thread_bot_author_and_msg(comments_nodes: list[dict]) -> tuple[str, str]:
    for c in comments_nodes:
        author_login = ((c.get("author") or {}).get("login") or "").lower()
        if author_login.endswith("[bot]") or author_login.startswith("grug-"):
            return author_login, str(c.get("body") or "")
    return "bot", ""


def _parse_unresolved_threads(threads_data: list[dict]) -> list[BotFinding]:
    unresolved_threads: list[BotFinding] = []
    for t in threads_data:
        if not t.get("isResolved"):
            t_id = str(t.get("id") or "")
            comments_nodes = (t.get("comments") or {}).get("nodes") or []
            bot_name, msg = _find_thread_bot_author_and_msg(comments_nodes)
            unresolved_threads.append(BotFinding("review_thread", t_id, bot_name, msg))
    return unresolved_threads


def _is_bot_account(user: dict) -> bool:
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _parse_unanswered_reviews(reviews_data: list[dict]) -> list[BotFinding]:
    unanswered_reviews: list[BotFinding] = []
    for r in reviews_data:
        u = r.get("user") or {}
        body = (r.get("body") or "").strip()
        state = (r.get("state") or "").upper()
        if _is_bot_account(u) and body and state != "APPROVED":
            r_id = str(r.get("id") or "")
            u_login = (u.get("login") or "").lower()
            unanswered_reviews.append(BotFinding("review_body", r_id, u_login, body))
    return unanswered_reviews


def _parse_non_passing_bot_check_runs(check_runs_data: list[dict]) -> list[BotFinding]:
    non_passing_checks: list[BotFinding] = []
    for cr in check_runs_data:
        app_slug = ((cr.get("app") or {}).get("slug") or "").lower()
        name = str(cr.get("name") or "")
        is_bot = bool(app_slug or name.startswith("Grug") or "bot" in name.lower())
        status = cr.get("status")
        conclusion = cr.get("conclusion")
        if is_bot and (status != "completed" or conclusion != "success"):
            cr_id = str(cr.get("id") or name)
            non_passing_checks.append(
                BotFinding(
                    "bot_check_run",
                    cr_id,
                    app_slug or "bot",
                    f"{name}: status={status}, conclusion={conclusion}",
                )
            )
    return non_passing_checks


def _parse_issue_bot_comments(issue_comments_data: list[dict]) -> list[BotFinding]:
    issue_comments: list[BotFinding] = []
    for ic in issue_comments_data:
        u = ic.get("user") or {}
        body = (ic.get("body") or "").strip()
        if _is_bot_account(u) and body:
            ic_id = str(ic.get("id") or "")
            u_login = (u.get("login") or "").lower()
            issue_comments.append(BotFinding("issue_comment", ic_id, u_login, body))
    return issue_comments


def parse_bot_surfaces(
    threads_data: list[dict],
    reviews_data: list[dict],
    check_runs_data: list[dict],
    issue_comments_data: list[dict],
) -> BotSurfaces:
    """Parse raw GitHub data into findings on all four bot comment surfaces."""
    return BotSurfaces(
        unresolved_review_threads=tuple(_parse_unresolved_threads(threads_data)),
        unanswered_review_bodies=tuple(_parse_unanswered_reviews(reviews_data)),
        non_passing_bot_check_runs=tuple(
            _parse_non_passing_bot_check_runs(check_runs_data)
        ),
        issue_comments=tuple(_parse_issue_bot_comments(issue_comments_data)),
    )


def fetch_open_prs(
    repo: str,
    runner: Runner = _subprocess_runner,
) -> list[dict]:
    """Fetch open PRs for a repo. Takes no action on any PR (reports only)."""
    raw = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,headRefOid",
        ]
    )
    try:
        prs = json.loads(raw or "[]")
        return prs if isinstance(prs, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def fetch_pr_state(
    repo: str,
    pr_info: dict,
    runner: Runner = _subprocess_runner,
    now: float | None = None,
    ready_since: float | None = None,
) -> PRState:
    """Fetch and construct PRState for one open PR across CI and bot surfaces."""
    pr_number = int(pr_info.get("number") or 0)
    title = str(pr_info.get("title") or "")
    head_sha = str(pr_info.get("headRefOid") or "")

    check_runs_data: list[dict] = []
    if head_sha:
        try:
            raw_cr = runner(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/commits/{head_sha}/check-runs",
                    "--jq",
                    ".check_runs",
                ]
            )
            parsed = json.loads(raw_cr or "[]")
            if isinstance(parsed, list):
                check_runs_data = parsed
        except (RuntimeError, json.JSONDecodeError):
            check_runs_data = []

    checks = parse_ci_checks(check_runs_data, now=now)

    threads_data: list[dict] = []
    try:
        owner, name = repo.split("/", 1)
        raw_threads = runner(
            [
                "gh",
                "api",
                "graphql",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"pr={pr_number}",
                "-f",
                (
                    "query=query($owner:String!,$name:String!,$pr:Int!){"
                    "repository(owner:$owner,name:$name){"
                    "pullRequest(number:$pr){"
                    "reviewThreads(first:100){nodes{id,isResolved,"
                    "comments(first:10){nodes{author{login},body}}}}}}}"
                ),
                "--jq",
                ".data.repository.pullRequest.reviewThreads.nodes",
            ]
        )
        parsed_threads = json.loads(raw_threads or "[]")
        if isinstance(parsed_threads, list):
            threads_data = parsed_threads
    except (RuntimeError, json.JSONDecodeError, ValueError):
        threads_data = []

    reviews_data: list[dict] = []
    try:
        raw_revs = runner(
            [
                "gh",
                "api",
                f"repos/{repo}/pulls/{pr_number}/reviews",
            ]
        )
        parsed_revs = json.loads(raw_revs or "[]")
        if isinstance(parsed_revs, list):
            reviews_data = parsed_revs
    except (RuntimeError, json.JSONDecodeError):
        reviews_data = []

    issue_comments_data: list[dict] = []
    try:
        raw_ics = runner(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
            ]
        )
        parsed_ics = json.loads(raw_ics or "[]")
        if isinstance(parsed_ics, list):
            issue_comments_data = parsed_ics
    except (RuntimeError, json.JSONDecodeError):
        issue_comments_data = []

    bot_surfaces = parse_bot_surfaces(
        threads_data=threads_data,
        reviews_data=reviews_data,
        check_runs_data=check_runs_data,
        issue_comments_data=issue_comments_data,
    )

    return PRState(
        pr_number=pr_number,
        title=title,
        checks=tuple(checks),
        bot_surfaces=bot_surfaces,
        ready_since=ready_since,
    )


def report_shepherd_pr_queue(
    repo: str,
    runner: Runner = _subprocess_runner,
    now: float | None = None,
    state_path: str = DEFAULT_STATE_PATH,
) -> str:
    """Fetch all open PRs, evaluate state, track ready duration, and report queue.

    Takes no action on any PR -- reports only, does not merge or modify.
    """
    current_time = now if now is not None else datetime.now(timezone.utc).timestamp()
    open_prs_info = fetch_open_prs(repo, runner=runner)
    tracker = load_ready_tracker(state_path)

    pr_states = [
        fetch_pr_state(
            repo,
            pr_info,
            runner=runner,
            now=current_time,
            ready_since=tracker.get(int(pr_info.get("number") or 0)),
        )
        for pr_info in open_prs_info
    ]

    updated_prs = update_ready_tracker(pr_states, tracker, now=current_time)
    save_ready_tracker(state_path, tracker)

    queue = build_pr_queue(updated_prs, now=current_time)
    return format_queue_report(queue)


if __name__ == "__main__":
    svc = sys.argv[1] if len(sys.argv) > 1 else "webhook"
    sys.exit(main(svc))
