"""Reaction-poll engine for the Elder persona (#245).

The second of the two #190 quality-feedback loops: a developer's 👍/👎
on a Grug inline review comment is the HUMAN ground-truth label that
calibrates the LLM judge's `is_real_bug` guess. GitHub does not webhook
comment reactions, so a scheduled poller (#245b) reads them via the
reactions REST API and pipes the signal into DD LLM Obs as a
`human_verdict` annotation attached to every producer span. New records
trust only reactors with repository write permission; confirmed/false-positive labels also
update the repository ledger and refresh its bounded practices/examples.

This module is the engine: classify reactions → verdict, poll a
comment's reactions, and `poll_and_annotate` a batch with dedup. The
scheduled trigger (Lambda + EventBridge) + the persist-on-publish
wiring live in #245b - this layer owns GH, DD, and ledger I/O and is fully
unit-testable.

Dedup: each comment record carries `last_verdict` (the verdict last
submitted and learned). We only act when the current classification
differs — so a 👎 that's been sitting for days doesn't re-submit every
poll cycle, but a developer flipping 👎→👍 (changed their mind) does.
Dedup is at-least-once, NOT exactly-once: if the DD submit succeeds but
the baseline write then fails, the next cycle re-submits. DD LLM Obs
evaluations are time-series events (each `submit_evaluation` carries a
generation timestamp), so a re-submit APPENDS a second `human_verdict`
event on the span — it does NOT overwrite. We accept that: a rare
duplicate (only on a submit-ok / baseline-write-fail partial failure,
which needs a DDB fault) is strictly better than advancing the baseline
before a confirmed submit and losing a human signal forever. The
calibration consumer MUST dedup by (span, label) taking the latest
timestamp — do not assume one eval per comment.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional
from urllib.parse import quote

import httpx

from adapters.install_store import CommentRecord, update_comment_record_reaction
from llm_client import ReactionVerdict, submit_reaction_annotation

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.reactions")

_GH_API = "https://api.github.com"
_REACTIONS_TIMEOUT = 10


def _annotation_targets(
    record: CommentRecord,
) -> list[tuple[dict, dict[str, str]]]:
    """Build per-producer annotation targets with legacy fallback."""
    base_tags = record.get("finding_tags", {})
    targets: list[tuple[dict, dict[str, str]]] = []
    origins = record.get("finding_origins", [])
    for origin in origins:
        span_context = origin.get("review_span_context")
        if not isinstance(span_context, dict):
            continue
        tags = dict(base_tags)
        backend = origin.get("backend")
        model = origin.get("model")
        if isinstance(backend, str):
            tags["source_backend"] = backend
        if isinstance(model, str):
            tags["source_model"] = model
        targets.append((span_context, tags))
    if targets:
        return targets
    if origins:
        # The finding has producer provenance, but none of those producers has
        # an exported span. Do not attach feedback to the response-level span,
        # which may belong to a different backend.
        return []
    span_context = record.get("review_span_context")
    if isinstance(span_context, dict):
        return [(span_context, dict(base_tags))]
    return []


def _classify_reactions(reactions: list[dict]) -> Optional[ReactionVerdict]:
    """Map a comment's reactions to a single verdict, or None.

    👎 (`-1`) → false_positive; 👍 (`+1`) → confirmed. When BOTH are
    present, 👎 wins: a developer flagging a false positive is the
    higher-value correction (it tells us the reviewer was wrong, which
    is what prompt-optimization needs most). Non-thumbs reactions
    (heart/rocket/eyes) carry no verdict signal → None.

    Caller filters new records to write-authorized collaborators; old records
    without the capture marker retain their historical DD-only behavior.
    """
    contents = {r.get("content") for r in reactions}
    if "-1" in contents:
        return "false_positive"
    if "+1" in contents:
        return "confirmed"
    return None


def _has_write_permission(
    install_token: str, owner: str, repo: str, login: str,
) -> bool:
    """Check whether a reactor can maintain repository code."""
    resp = httpx.get(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/"
        f"collaborators/{quote(login, safe='')}/permission",
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_REACTIONS_TIMEOUT,
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    body = resp.json()
    return isinstance(body, dict) and body.get("permission") in {"admin", "write"}


def _trusted_reactions(
    record: CommentRecord,
    reactions: list[dict],
    *,
    install_token: str,
    owner: str,
    repo: str,
    permission_cache: dict[tuple[str, str], bool],
) -> list[dict]:
    """Use only write-authorized reactors for newly captured records."""
    if not record.get("trust_reactors", False):
        # Old records can still annotate DD but lack the fields required for
        # automatic prompt learning.
        return reactions
    trusted: list[dict] = []
    for reaction in reactions:
        login = str((reaction.get("user") or {}).get("login") or "")
        if not login:
            continue
        cache_key = (f"{owner}/{repo}".casefold(), login.casefold())
        if cache_key not in permission_cache:
            try:
                permission_cache[cache_key] = _has_write_permission(
                    install_token, owner, repo, login,
                )
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                log.warning(
                    "reaction_permission_check_failed",
                    extra={
                        "repo": f"{owner}/{repo}",
                        "login": login,
                        "kind": type(e).__name__,
                    },
                )
                permission_cache[cache_key] = False
        if permission_cache[cache_key]:
            trusted.append(reaction)
    return trusted


def _record_reaction_learning(
    record: CommentRecord, verdict: ReactionVerdict,
) -> bool:
    """Persist trusted feedback and refresh the repo's bounded prompt cache."""
    if not record.get("trust_reactors", False):
        return False
    finding_text = record.get("finding_text", "")
    tags = record.get("finding_tags", {})
    rule_name = tags.get("rule_name", "")
    severity = tags.get("severity", "")
    if not all((finding_text, rule_name, severity)):
        return False

    from adapters.install_store import (  # type: ignore
        list_ledger_rows,
        put_ledger_row,
        put_repo_exemplars,
        put_repo_practices,
    )
    from best_practices import derive_practices, practices_to_dicts
    from few_shot import exemplars_from_rows, exemplars_to_dicts
    from ledger import accepted_findings_by_class, parse_row

    row = {
        "repo": record["repo"],
        "pr": record["pr_number"],
        "reviewer": "grug-elder",
        "severity": severity,
        "class": rule_name,
        "finding": finding_text,
        "verdict": "declined" if verdict == "confirmed" else "false-positive",
        # Stable evidence makes a reaction flip overwrite the same ledger row.
        "evidence": f"github-review-comment:{record['comment_id']}",
        "ts": "",
        "commit": record.get("head_sha") or None,
    }
    put_ledger_row(row)
    parsed_rows = [
        parsed
        for raw in list_ledger_rows(record["repo"])
        if (parsed := parse_row(raw)) is not None
    ]
    put_repo_practices(
        record["repo"], practices_to_dicts(derive_practices(parsed_rows)),
    )
    put_repo_exemplars(
        record["repo"],
        exemplars_to_dicts(
            exemplars_from_rows(accepted_findings_by_class(parsed_rows))
        ),
    )
    return True


def _can_learn(record: CommentRecord) -> bool:
    tags = record.get("finding_tags", {})
    return bool(
        record.get("finding_text")
        and record.get("trust_reactors")
        and tags.get("rule_name")
        and tags.get("severity")
    )


def poll_comment_reactions(
    install_token: str, owner: str, repo: str, comment_id: int,
) -> list[dict]:
    """GET the reactions on one PR review comment. Raises on transport /
    HTTP error — the batch caller catches per-record so one failure
    doesn't abort the cycle.

    `per_page=100` (the max) — the endpoint defaults to 30/page and
    returns reactions in created_at order. Without this, a 👎 sitting
    past position 30 on a heavily-reacted comment would never be seen
    and the comment would misclassify as confirmed/None — defeating the
    "👎 wins" calibration premise. 100 makes a miss effectively
    impossible for a single review comment (full Link-header pagination
    would be over-engineering at that ceiling)."""
    resp = httpx.get(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/"
        f"pulls/comments/{comment_id}/reactions",
        params={"per_page": 100},
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_REACTIONS_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, list) else []


def poll_and_annotate(
    records: list[CommentRecord],
    *,
    install_id: int,
    fetch_token: Callable[[], str],
) -> int:
    """Poll reactions for each comment record and submit changed
    verdicts to DD. Returns the count submitted.

    Best-effort per-record: a single comment's poll failing (GH 5xx,
    deleted comment) logs + continues — one bad comment must not abort
    the whole poll cycle. `fetch_token` is a thunk so the caller owns
    install-token acquisition (and we don't re-fetch per record beyond
    what the thunk caches).
    """
    submitted = 0
    permission_cache: dict[tuple[str, str], bool] = {}
    for rec in records:
        comment_id = rec["comment_id"]
        annotation_targets = _annotation_targets(rec)
        if not annotation_targets and not _can_learn(rec):
            # Old record with neither a producer span nor trusted-learning
            # fields has no usable feedback destination.
            continue
        # `partition` (not `split` + splat): a malformed persisted repo
        # without a "/" would make `*split("/",1)` a 1-element splat →
        # uncaught TypeError → aborts the whole batch, breaking the
        # per-record best-effort contract. Guard + skip instead.
        owner, sep, name = rec["repo"].partition("/")
        if not sep:
            log.warning(
                "reaction_record_malformed_repo",
                extra={"install_id": install_id, "comment_id": comment_id,
                       "repo": rec["repo"]},
            )
            continue
        try:
            install_token = fetch_token()
            reactions = poll_comment_reactions(
                install_token, owner, name, comment_id,
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.warning(
                "reaction_poll_failed",
                extra={
                    "install_id": install_id, "comment_id": comment_id,
                    "kind": type(e).__name__,
                },
            )
            continue
        reactions = _trusted_reactions(
            rec,
            reactions,
            install_token=install_token,
            owner=owner,
            repo=name,
            permission_cache=permission_cache,
        )
        verdict = _classify_reactions(reactions)
        if verdict is None:
            continue
        if verdict == rec.get("last_verdict"):
            # Dedup: already submitted this verdict for this comment.
            continue
        # Mixed-signal observability: a comment with BOTH 👍 and 👎 is a
        # developer disagreement — the highest-information calibration
        # case, force-classified to false_positive. Log it (with the
        # verdict) so the calibration set can DOWN-WEIGHT contested rows
        # rather than have them silently flatten into the verdict.
        contents = {r.get("content") for r in reactions}
        if "+1" in contents and "-1" in contents:
            log.info(
                "reaction_mixed_signal",
                extra={"install_id": install_id, "comment_id": comment_id,
                       "verdict": verdict},
            )
        # Attempt every producer even if one DD submit fails. Advance the
        # per-comment baseline only when every annotation and the store update
        # succeed; otherwise the next poll retries the whole fan-out. A retry
        # can append a duplicate eval for a producer that succeeded before a
        # sibling failed, which the calibration consumer already deduplicates
        # by (span, label).
        annotation_failed = False
        for span_context, tags in annotation_targets:
            try:
                submit_reaction_annotation(
                    verdict=verdict,
                    review_span_context=span_context,
                    tags=tags,
                )
            except Exception as e:  # noqa: BLE001 - best-effort per producer span
                annotation_failed = True
                log.warning(
                    "reaction_submit_or_persist_failed",
                    extra={
                        "install_id": install_id,
                        "comment_id": comment_id,
                        "kind": type(e).__name__,
                        "source_backend": tags.get("source_backend"),
                    },
                )
        if annotation_failed:
            continue
        try:
            _record_reaction_learning(rec, verdict)
            update_comment_record_reaction(
                install_id=install_id, comment_id=comment_id, verdict=verdict,
            )
            submitted += 1
        except Exception as e:  # noqa: BLE001 — best-effort per-record
            log.warning(
                "reaction_submit_or_persist_failed",
                extra={"install_id": install_id, "comment_id": comment_id,
                       "kind": type(e).__name__},
            )
            continue
    log.info(
        "reaction_poll_cycle",
        extra={
            "install_id": install_id,
            "records": len(records),
            "submitted": submitted,
        },
    )
    return submitted
