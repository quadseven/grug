# WEBHOOK-ONLY (NOT mirrored): the Elder cave-fallback producer + (later)
# result handler. Like async_dispatch.py / poller_handler.py, the api service
# never runs Elder, so there is no api sibling.
"""Elder cave-fallback (ADR-0005, spec 0018).

When BOTH cloud LLM backends fail (`review_diff` → `all_failed`), enqueue a
review job to "the Cave" — the operator's self-hosted LLM — over an SQS
airlock (`grug-cave-jobs`). The connector (#316, separate repo) answers on
`grug-cave-results`; the webhook consumes that and heals the verdict. Grug and
the Cave never connect — the queues are the only contact surface.

PART A scope (#310): the AWS-side producer + the cross-repo message contract,
for small diffs (carried INLINE). The connector (#316) and S3 spillover for
large diffs (#311) are separate slices. The result handler lands in a
follow-up commit on this branch.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import boto3

from activity_log import record_check_verdict
from github_app_auth import with_install_token_retry
from github_checks_client import CheckRunResult, post_check_run
from llm_client import Hunk

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.cave_fallback")

_sqs = boto3.client("sqs")

# Queue URL injected by Pulumi as a Lambda env var. Empty in local/dev/tests →
# enqueue is a no-op, so the producer can't crash a review just because the
# queue isn't wired yet (best-effort, same discipline as the #272 offload).
_JOBS_QUEUE_URL = os.getenv("GRUG_CAVE_JOBS_QUEUE_URL", "")

# Bump when the wire shape changes so the connector (a separate repo) can
# reject/handle an unknown version instead of silently mis-parsing.
SCHEMA_VERSION = 1

# The one persona that falls back today. Carried on the message so a future
# multi-persona airlock routes without a schema change.
_PERSONA = "elder"


@dataclass(frozen=True, slots=True)
class FallbackJob:
    """A review job handed to the Cave connector over `grug-cave-jobs`.

    Carries the PR coords + the diff INLINE (small-diff scope; #311 adds S3
    spillover) and NO GitHub credential — the connector re-reads nothing from
    GitHub. `hunks` is (path, body) pairs so the connector reconstructs the
    same review units `review_diff` would have seen.
    """

    schema_version: int
    install_id: int
    repo: str  # "owner/name"
    pr_number: int
    head_sha: str
    persona: str
    hunks: tuple[tuple[str, str], ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "install_id": self.install_id,
                "repo": self.repo,
                "pr_number": self.pr_number,
                "head_sha": self.head_sha,
                "persona": self.persona,
                "hunks": [{"path": p, "body": b} for p, b in self.hunks],
            }
        )


@dataclass(frozen=True, slots=True)
class FallbackResult:
    """The connector's answer on `grug-cave-results`: findings (or a degraded
    marker) for one (install, repo, pr, head). Consumed by the webhook to
    publish the check-run + heal the verdict.

    `findings` are kept as primitive dicts (the connector's wire shape) — the
    result handler validates them into persona `Finding`s, mirroring how
    `_coerce_finding` defends the live LLM path.
    """

    schema_version: int
    install_id: int
    repo: str
    pr_number: int
    head_sha: str
    persona: str
    findings: tuple[dict[str, Any], ...]
    degraded: bool = False
    degraded_reason: str = ""
    model: str = ""

    @classmethod
    def from_json(cls, raw: str) -> "FallbackResult":
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("FallbackResult body is not a JSON object")
        findings = d.get("findings", [])
        if not isinstance(findings, list):
            findings = []
        return cls(
            schema_version=int(d["schema_version"]),
            install_id=int(d["install_id"]),
            repo=str(d["repo"]),
            pr_number=int(d["pr_number"]),
            head_sha=str(d["head_sha"]),
            persona=str(d.get("persona", _PERSONA)),
            findings=tuple(f for f in findings if isinstance(f, dict)),
            degraded=bool(d.get("degraded", False)),
            degraded_reason=str(d.get("degraded_reason", "")),
            model=str(d.get("model", "")),
        )


def _dedup_id(install_id: int, repo: str, pr_number: int, head_sha: str) -> str:
    """FIFO content-dedup key. Includes `head_sha` deliberately: a NEW push (new
    head) is a DIFFERENT review that must enqueue, so it must not dedup against
    the prior commit's job; but a double-fire on the SAME head within the 5-min
    FIFO window is dropped for free (a redelivery / re-trigger guard)."""
    return f"{install_id}:{repo}:{pr_number}:{_PERSONA}:{head_sha}"


def enqueue_fallback(
    hunks: list[Hunk],
    *,
    installation_id: int,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> bool:
    """Enqueue a `FallbackJob` to `grug-cave-jobs` — the Elder owned-LLM fallback.

    Call ONLY when `review_diff` returned `all_failed`. No-op (returns `False`)
    when the SSM flag is off, the queue URL is unset, or there are no hunks.
    BEST-EFFORT: never raises — a SendMessage failure logs
    `elder_fallback_enqueue_failed` and returns `False` so the already-published
    `errored` verdict stands and the fallback re-triggers on the next push.

    Returns `True` iff a job was actually enqueued.
    """
    # Lazy import keeps the SSM read off this module's import path (and lets
    # tests patch `secrets_loader.get_fallback_enabled` at the lookup site).
    from secrets_loader import get_fallback_enabled

    if not get_fallback_enabled():
        return False
    if not _JOBS_QUEUE_URL:
        log.warning(
            "elder_fallback_no_queue_url",
            extra={"repo": repo, "pr": pr_number},
        )
        return False
    if not hunks:
        return False

    job = FallbackJob(
        schema_version=SCHEMA_VERSION,
        install_id=installation_id,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        persona=_PERSONA,
        hunks=tuple((h.path, h.body) for h in hunks),
    )
    try:
        _sqs.send_message(
            QueueUrl=_JOBS_QUEUE_URL,
            MessageBody=job.to_json(),
            MessageGroupId=str(installation_id),
            MessageDeduplicationId=_dedup_id(
                installation_id, repo, pr_number, head_sha
            ),
        )
    except Exception as e:  # noqa: BLE001 — best-effort: a lost fallback re-triggers next push
        log.warning(
            "elder_fallback_enqueue_failed",
            extra={"repo": repo, "pr": pr_number, "kind": type(e).__name__},
        )
        return False
    log.info(
        "elder_fallback_enqueued",
        extra={
            "repo": repo,
            "pr": pr_number,
            "head_sha": head_sha[:8],
            "hunks": len(hunks),
        },
    )
    return True


# ---------------------------------------------------------------------------
# Consumer (#310): heal the verdict from the connector's FallbackResult.
# `lambda_handler.handler` routes the aws:sqs event (`grug-cave-results`) here.
# The Elder check is advisory-by-default, so a healed fallback publishes a
# NEUTRAL check-run carrying the Cave's findings (blocking-aware fallback is a
# follow-up — Part A keeps it advisory). NEVER raises back to the Lambda: a
# raise would retry-storm the event-source mapping; a bad record is logged and
# dropped (the verdict stays `errored`, re-triggering on the next push).
# ---------------------------------------------------------------------------

# MUST match the persona's check name so the fallback heals the SAME check-run
# (post_check_run is idempotent on (name, head_sha)) rather than posting a
# duplicate. Kept in sync with personas/code_reviewer/dispatch.py:_CHECK_NAME.
_CHECK_NAME = "Grug — Code Review"

# Legacy persona code key; record_check_verdict maps it to the caveman name
# "elder" (ADR-0002) at the write boundary.
_PERSONA_KEY = "code_reviewer"


def _summarize(findings: tuple[dict[str, Any], ...]) -> tuple[str, str]:
    """(title, markdown summary) for the healed check-run. Caveman voice, same
    register as the persona — computed here from primitive finding dicts so the
    result handler stays decoupled from the persona module. Tolerant of either
    the persona shape (`file`/`rule_name`) or the wire shape (`path`/`rule`)."""
    if not findings:
        return (
            "✅ Grug see no bad omens (from the Cave)",
            "Grug read your markings from his own Cave. No omens this time.",
        )
    n = len(findings)
    high = sum(
        1 for f in findings if str(f.get("severity")) in ("high", "critical")
    )
    lines = []
    for f in findings:
        sev = str(f.get("severity", "?"))
        rule = str(f.get("rule_name") or f.get("rule") or "?")
        loc = f"{f.get('file') or f.get('path') or '?'}:{f.get('line', '?')}"
        msg = str(f.get("message", ""))
        lines.append(f"- **[{sev}]** `{rule}` @ {loc} — {msg}")
    title = f"🪨 Grug found {n} omen{'s' if n != 1 else ''} (from the Cave)"
    body = (
        "Grug read your markings from his own Cave (the cloud spirits slept).\n\n"
        f"{high} loud omen(s) of {n}:\n\n" + "\n".join(lines)
    )
    return title, body


def _heal_one(body: str) -> None:
    """Publish + heal for ONE `FallbackResult`. Raises on a malformed body or a
    publish error — `handle_fallback_result` catches per-record."""
    res = FallbackResult.from_json(body)
    if res.degraded:
        # The Cave ALSO failed (or the connector couldn't review). Don't fake a
        # review — leave the verdict `errored` and log so the double-outage is
        # visible (this is what the re-scoped degraded monitor catches).
        log.warning(
            "elder_fallback_result_degraded",
            extra={
                "repo": res.repo,
                "pr": res.pr_number,
                "reason": res.degraded_reason,
            },
        )
        return
    owner, _, repo_name = res.repo.partition("/")
    title, summary = _summarize(res.findings)
    check = CheckRunResult(
        name=_CHECK_NAME,
        head_sha=res.head_sha,
        status="completed",
        # Elder is advisory-by-default; a healed fallback is advisory. Blocking-
        # aware fallback (read RepoConfig, fail on high/critical) is a follow-up.
        conclusion="neutral",
        title=title,
        summary=summary,
    )
    with_install_token_retry(
        res.install_id,
        lambda token: post_check_run(
            token,
            owner,
            repo_name,
            check,
            external_id=f"grug-cr:{res.repo}#{res.pr_number}:{res.head_sha}",
        ),
    )
    # Heal the Activity verdict: errored → reviewed (warn/pass). Idempotent per
    # (persona, head_sha) at the store layer; never raises.
    record_check_verdict(
        install_id=res.install_id,
        persona_key=_PERSONA_KEY,
        repo=res.repo,
        pr_number=res.pr_number,
        head_sha=res.head_sha,
        conclusion="neutral",
        summary=title,
        findings_count=len(res.findings),
        blocking=False,
        degraded_reason=None,
    )
    log.info(
        "elder_fallback_healed",
        extra={
            "repo": res.repo,
            "pr": res.pr_number,
            "findings": len(res.findings),
        },
    )


def handle_fallback_result(event: dict[str, Any]) -> dict[str, int]:
    """Consume `grug-cave-results` SQS records (event-source mapping): publish
    the Cave's review as a check-run and heal the `errored` verdict.

    NEVER raises back to the Lambda — a raise would retry-storm the ESM. A
    malformed record or a publish failure is logged and dropped (the verdict
    stays `errored` and re-triggers on the next push). Returns a small summary
    dict (also the structured-log payload)."""
    records = event.get("Records", []) if isinstance(event, dict) else []
    healed = 0
    failed = 0
    for rec in records:
        body = rec.get("body", "") if isinstance(rec, dict) else ""
        try:
            _heal_one(body)
            healed += 1
        except Exception as e:  # noqa: BLE001 — never retry-storm the ESM
            log.warning(
                "elder_fallback_result_unhandled",
                extra={"kind": type(e).__name__},
            )
            failed += 1
    return {"records": len(records), "healed": healed, "failed": failed}
