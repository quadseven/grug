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
from typing import Any, Optional

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

# Wire-shape version. v2 (#311) carries a `diff_ref` (inline-or-S3) instead of
# v1's raw inline `hunks` — bump so the connector (a separate repo) rejects an
# unknown version instead of silently mis-parsing.
SCHEMA_VERSION = 2

# The one persona that falls back today. Carried on the message so a future
# multi-persona airlock routes without a schema change.
_PERSONA = "elder"

# SQS hard-caps a message at 256 KB. A diff serializing larger than this is
# spilled to S3 and the message carries only a pointer (#311); under it, the
# diff rides inline. 250 KB leaves headroom for the JSON envelope.
_MAX_INLINE_DIFF_BYTES = 250 * 1024

# S3 bucket for spilled (large) diffs, injected by Pulumi. Empty in
# local/dev/tests → no spillover (a too-large diff just can't be packed and the
# enqueue no-ops, same best-effort discipline as a missing queue URL).
_DIFF_BUCKET = os.getenv("GRUG_CAVE_DIFF_BUCKET", "")

_s3 = boto3.client("s3")


@dataclass(frozen=True, slots=True)
class FallbackJob:
    """A review job handed to the Cave connector over `grug-cave-jobs`.

    Carries the PR coords + a `diff_ref` (the DiffRef codec's output: the diff
    inline for small PRs, or an S3 pointer for large ones — #311) and NO GitHub
    credential. The connector calls `unpack_diff(diff_ref)` to reconstruct the
    same review units `review_diff` would have seen — reading the diff from S3,
    never from GitHub (the creds boundary).
    """

    schema_version: int
    install_id: int
    repo: str  # "owner/name"
    pr_number: int
    head_sha: str
    persona: str
    diff_ref: dict[str, Any]  # DiffRef: {"kind": "inline"|"s3", ...}

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "install_id": self.install_id,
                "repo": self.repo,
                "pr_number": self.pr_number,
                "head_sha": self.head_sha,
                "persona": self.persona,
                "diff_ref": self.diff_ref,
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


# ---------------------------------------------------------------------------
# DiffRef codec (#311) — the deep module behind the airlock's diff delivery.
# A DiffRef is a small JSON-able dict that is EITHER the diff inline (small PR)
# OR an S3 pointer (large PR). `pack_diff` decides; `unpack_diff` reconstructs.
# Both sides of the airlock use it: the webhook packs, the connector (#316)
# unpacks — so a big diff is read from S3, never re-fetched from GitHub.
# ---------------------------------------------------------------------------


def _hunks_payload(hunks: list[Hunk]) -> list[dict[str, str]]:
    return [{"path": h.path, "body": h.body} for h in hunks]


def pack_diff(
    hunks: list[Hunk], *, install_id: int, head_sha: str
) -> Optional[dict[str, Any]]:
    """Serialize `hunks` into a DiffRef: an INLINE ref when the diff fits under
    `_MAX_INLINE_DIFF_BYTES`, else spill the diff to S3 and return an S3 pointer.

    Returns `None` when a large diff can't be spilled (no bucket configured, or
    the S3 put failed) — the caller treats that as "can't pack" and no-ops
    (best-effort, same discipline as a missing queue URL)."""
    payload = _hunks_payload(hunks)
    inline = {"kind": "inline", "hunks": payload}
    if len(json.dumps(inline).encode("utf-8")) <= _MAX_INLINE_DIFF_BYTES:
        return inline
    if not _DIFF_BUCKET:
        log.warning(
            "elder_fallback_diff_too_large_no_bucket",
            extra={"install_id": install_id, "head_sha": head_sha[:8]},
        )
        return None
    key = f"diffs/{install_id}/{head_sha}.json"
    try:
        _s3.put_object(
            Bucket=_DIFF_BUCKET,
            Key=key,
            Body=json.dumps(payload).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:  # noqa: BLE001 — best-effort: a failed spill drops the fallback
        log.warning(
            "elder_fallback_diff_spill_failed",
            extra={
                "install_id": install_id,
                "head_sha": head_sha[:8],
                "kind": type(e).__name__,
            },
        )
        return None
    return {"kind": "s3", "bucket": _DIFF_BUCKET, "key": key}


def unpack_diff(diff_ref: dict[str, Any]) -> list[Hunk]:
    """Reconstruct the hunks from a DiffRef — inline payload or an S3 fetch.

    Raises `ValueError` on an unknown/malformed ref (the connector catches it
    and degrades). Defined here, the codec's home, so the wire contract has ONE
    owner; the connector (#316) consumes this same shape."""
    kind = diff_ref.get("kind")
    if kind == "inline":
        payload = diff_ref.get("hunks", [])
    elif kind == "s3":
        bucket, key = diff_ref.get("bucket"), diff_ref.get("key")
        if not bucket or not key:
            raise ValueError("s3 DiffRef missing bucket/key")
        raw = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        payload = json.loads(raw)
    else:
        raise ValueError(f"unknown DiffRef kind: {kind!r}")
    if not isinstance(payload, list):
        raise ValueError("DiffRef hunks payload is not a list")
    return [
        Hunk(path=str(h["path"]), body=str(h["body"]))
        for h in payload
        if isinstance(h, dict)
    ]


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

    # Pack the diff into a DiffRef (#311): inline for small PRs, spilled to S3
    # for large ones. None ⇒ a large diff couldn't be spilled (no bucket / put
    # failed); pack_diff already logged it, so no-op (best-effort).
    diff_ref = pack_diff(hunks, install_id=installation_id, head_sha=head_sha)
    if diff_ref is None:
        return False

    job = FallbackJob(
        schema_version=SCHEMA_VERSION,
        install_id=installation_id,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        persona=_PERSONA,
        diff_ref=diff_ref,
    )
    body = job.to_json()
    try:
        _sqs.send_message(
            QueueUrl=_JOBS_QUEUE_URL,
            MessageBody=body,
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
            "diff_kind": diff_ref["kind"],  # inline | s3
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


def _md_safe(s: str) -> str:
    """Neutralize markdown/control chars in connector-supplied finding text
    before it lands in a check-run summary. Backticks/pipes/newlines are the
    layout-breaking + injection-relevant ones; cap length to bound a hostile
    payload. Cosmetic-grade (GitHub renders check-run summaries as markdown),
    not a security boundary on its own."""
    return (
        s.replace("`", "'")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )[:300]


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
        # peer-review (OpenRouter + Poolside + Spark, CONFIRMED 3x): connector
        # findings originate from an LLM reviewing a (semi-untrusted) PR diff,
        # so neutralize markdown/control chars before interpolating into the
        # check-run body — a crafted diff must not inject markup or break layout.
        sev = _md_safe(str(f.get("severity", "?")))
        rule = _md_safe(str(f.get("rule_name") or f.get("rule") or "?"))
        loc = _md_safe(f"{f.get('file') or f.get('path') or '?'}:{f.get('line', '?')}")
        msg = _md_safe(str(f.get("message", "")))
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
