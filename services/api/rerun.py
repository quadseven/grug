# API-ONLY (NOT mirrored): the enqueue side of operator-triggered re-runs
# (#305, ADR-0004). The webhook has its OWN consumer (services/webhook/rerun.py)
# — the api never runs the persona dispatch, so the two `rerun.py` files share a
# name but not content (allowlisted to diverge; not in check-mirrored-files.sh).
"""Re-run enqueuer (#305, ADR-0004) — the api side.

`POST /installations/{id}/repos/{repo_id}/rerun` validates + calls
`enqueue_rerun`, which sends a `RerunJob` to `grug-rerun-jobs` (SQS FIFO). The
api Lambda is 15s and can't run the review itself, so it hands off to the
    durable queue; the webhook consumer runs the persona with renewable leases.
"""
from __future__ import annotations

import logging
import os

import boto3
from rerun_queue import encode_rerun, rerun_group_id  # noqa: F401 - re-export for tests

log = logging.getLogger("grug.api.rerun")

_sqs = boto3.client("sqs")

# Queue URL injected by Pulumi. Unset in local/dev/tests; the endpoint surfaces
# a 503 (a real misconfig) rather than silently dropping the re-run.
_RERUN_QUEUE_URL = os.getenv("GRUG_RERUN_QUEUE_URL", "")


def enqueue_rerun(
    *, install_id: int, repo: str, pr_number: int, persona: str
) -> None:
    """Send a `RerunJob` to `grug-rerun-jobs`. Raises `RuntimeError` when the
    queue isn't configured (the endpoint maps it to a 503).

    Keyed by `repo` ("owner/name") — what the Activity row carries. FIFO
    content-dedup on `(install, repo, pr, persona)` — a double-click within the
    5-min window is dropped for free (ADR-0004). `head_sha` is deliberately NOT
    in the key: a re-run always targets the PR's CURRENT head, so two clicks on
    the same errored row ARE the same job. FIFO ordering is scoped to the same
    PR/persona so one long review cannot block unrelated PRs or questions."""
    if not _RERUN_QUEUE_URL:
        raise RuntimeError("GRUG_RERUN_QUEUE_URL not configured")
    _sqs.send_message(
        QueueUrl=_RERUN_QUEUE_URL,
        **encode_rerun(
            install_id=install_id, repo=repo, pr_number=pr_number,
            persona=persona,
        ).message_kwargs(),
    )
    log.info(
        "rerun_enqueued",
        extra={"install_id": install_id, "repo": repo, "pr": pr_number, "persona": persona},
    )
