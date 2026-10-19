"""Elder auto-pause under rapid pushes (grug#1188).

Each push to an open PR already gets a delta review: Elder reviews only
what changed since the last head it reviewed (Living Hunt, #557). During
active development that is still one review per push. After
`GRUG_ELDER_AUTOPAUSE_PUSHES` pushes inside
`GRUG_ELDER_AUTOPAUSE_WINDOW_MINUTES`, Elder pauses: it posts one note on
the PR and stops enqueueing reviews for pushes. Comment `/grug resume` to
lift the pause and review everything since the last reviewed head, or
`/grug review` for one review while the pause stays.

Only real pushes count and only real pushes are held: a `synchronize`
payload carries `after`, and the synthetic `synchronize` the dispatcher
builds for GitHub's Re-run button does not, so an operator's Re-run still
reviews. Every other action (opened, edited, ...) reviews as before.

Each push is claimed once per (PR, head SHA), so a redelivered webhook
neither counts twice nor posts a second note. The gate fails open: a store
outage reviews the push, and a pause whose note did not post is lifted
again, because a pause nobody was told about is a silent stop.

The store calls are imported at call time so `adapters.install_store.*`
stays the one patch target, as for the Living Hunt marker in dispatch.py;
GitHub auth is imported at call time because it is only used webhook-side.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

import httpx

from activity_log import record_check_verdict
from personas.registry import PullRequestContext

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.autopause")

# "review": enqueue as usual. "paused": already paused, hold this push.
# "pause_engaged": this push crossed the threshold; the note posted.
PauseDecision = Literal["review", "paused", "pause_engaged"]

_DEFAULT_PUSHES = 5
_DEFAULT_WINDOW_MINUTES = 10
_PAUSED_REASON = "auto_paused"
NOTE_MARKER = "<!-- grug:elder-autopause -->"
_API = "https://api.github.com"
_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class AutoPausePolicy:
    """`pushes` pushes inside `window_minutes` pause Elder. Zero or a
    negative value in either field turns auto-pause off."""

    pushes: int
    window_minutes: int

    @property
    def enabled(self) -> bool:
        return self.pushes > 0 and self.window_minutes > 0


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("elder_autopause_env_invalid", extra={"var": name, "default": default})
        return default


def policy_from_env() -> AutoPausePolicy:
    return AutoPausePolicy(
        pushes=_env_int("GRUG_ELDER_AUTOPAUSE_PUSHES", _DEFAULT_PUSHES),
        window_minutes=_env_int(
            "GRUG_ELDER_AUTOPAUSE_WINDOW_MINUTES", _DEFAULT_WINDOW_MINUTES,
        ),
    )


def _is_real_push(payload: dict) -> bool:
    return payload.get("action") == "synchronize" and bool(payload.get("after"))


def _coords(ctx: PullRequestContext) -> dict:
    return {
        "install_id": ctx.installation_id,
        "repo": f"{ctx.owner}/{ctx.repo_name}",
        "pr_number": ctx.pr_number,
    }


def _decide(ctx: PullRequestContext, policy: AutoPausePolicy) -> PauseDecision:
    """Count this push once and say whether it pauses Elder. Store errors
    propagate to `gate_push`, which fails open."""
    from adapters.install_store import (
        claim_delivery, is_elder_paused, pause_elder, record_elder_push,
    )

    coords = _coords(ctx)
    first_delivery = claim_delivery(
        f"elder-push:{ctx.installation_id}:{coords['repo']}"
        f"#{ctx.pr_number}@{ctx.head_sha}"
    )
    if is_elder_paused(**coords):
        return "paused"
    if not first_delivery:
        return "review"
    recent = record_elder_push(**coords, window_seconds=policy.window_minutes * 60)
    if recent < policy.pushes:
        return "review"
    return "pause_engaged" if pause_elder(**coords, head_sha=ctx.head_sha) else "paused"


def _note_body(policy: AutoPausePolicy) -> str:
    return (
        f"{NOTE_MARKER}\n"
        f"Grug see {policy.pushes} pushes in {policy.window_minutes} minutes. "
        "Tribe still hunting, so Elder rest and not review each push.\n\n"
        "- Comment `/grug resume` when ready: Elder wake and review all "
        "that changed since Elder last looked.\n"
        "- Comment `/grug review` for one look now. Elder keep resting after."
    )


def _post_note(ctx: PullRequestContext, body: str) -> None:
    from github_app_auth import with_install_token_retry  # lazy: webhook-side auth

    url = (
        f"{_API}/repos/{quote(ctx.owner, safe='')}/{quote(ctx.repo_name, safe='')}"
        f"/issues/{int(ctx.pr_number)}/comments"
    )

    def _post(token: str) -> None:
        httpx.post(
            url,
            json={"body": body},
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=_TIMEOUT,
        ).raise_for_status()

    with_install_token_retry(ctx.installation_id, _post)


def _announce(ctx: PullRequestContext, policy: AutoPausePolicy) -> bool:
    """Post the pause note. On failure lift the pause again and say so."""
    from adapters.install_store import resume_elder

    try:
        _post_note(ctx, _note_body(policy))
        return True
    except Exception as e:  # noqa: BLE001 - any failure means nobody was told
        log.warning(
            "elder_autopause_note_failed",
            extra={**_coords(ctx), "kind": type(e).__name__},
        )
    try:
        resume_elder(**_coords(ctx))
    except Exception as e:  # noqa: BLE001 - logged; the push reviews either way
        log.error(
            "elder_autopause_unpause_failed",
            extra={**_coords(ctx), "kind": type(e).__name__},
        )
    return False


def _record_held_push(ctx: PullRequestContext, policy: AutoPausePolicy) -> None:
    """Activity row for a push Elder did not review (`errored` in the feed:
    nothing was concluded on this head, so it must not read as a pass)."""
    record_check_verdict(
        install_id=ctx.installation_id,
        persona_key="code_reviewer",
        repo=f"{ctx.owner}/{ctx.repo_name}",
        pr_number=ctx.pr_number,
        head_sha=ctx.head_sha,
        conclusion="skipped",
        summary=(
            f"Elder paused after {policy.pushes} pushes in "
            f"{policy.window_minutes} min - comment /grug resume to review"
        ),
        findings_count=0,
        blocking=ctx.blocking,
        degraded_reason=_PAUSED_REASON,
    )


def gate_push(ctx: PullRequestContext) -> PauseDecision:
    """Decide whether this pull_request event enqueues an Elder review."""
    policy = policy_from_env()
    if not policy.enabled or not _is_real_push(ctx.payload):
        return "review"
    try:
        decision = _decide(ctx, policy)
    except Exception as e:  # noqa: BLE001 - fail open: review the push
        log.warning(
            "elder_autopause_gate_failed",
            extra={**_coords(ctx), "kind": type(e).__name__},
        )
        return "review"
    if decision == "pause_engaged" and not _announce(ctx, policy):
        return "review"
    if decision != "review":
        _record_held_push(ctx, policy)
        log.info(
            "elder_autopause_held_push",
            extra={**_coords(ctx), "decision": decision, "head_sha": ctx.head_sha[:8]},
        )
    return decision
