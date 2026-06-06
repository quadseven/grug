# MIRRORED — sibling at services/webhook/adapters/install_store.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Webhook-side install store + allowlist gate.

Webhook Lambda has DDB read+write perms but NO KMS perms — it never
touches OAuth tokens (those belong to api Lambda). This module exposes
ONLY the bool `allowlisted` field plus install-row CRUD.

Single-table layout:
  PK = INST#<install_id>     SK = META
    account_login, account_type, installed_at, installed_by_user_id
  PK = USER#<github_user_id> SK = META
    allowlisted (read here; oauth_*_blob untouched)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, NotRequired, Optional, TypedDict

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("grug.webhook.install_store")

_TABLE_NAME = os.environ.get("GRUG_DDB_TABLE", "grug-main")

# Lazy init — boto3 resource construction at import time would require
# AWS_DEFAULT_REGION to be set BEFORE the first import, which breaks
# tests that monkeypatch the env after collection. Codex post-review
# #52. The descriptor on `_table` lets old call sites (`_table.scan`,
# `_table.get_item`, etc.) keep working unchanged.
#
# Thread-safety via double-checked locking: a warm Lambda handling two
# concurrent invocations could race the unguarded check; both would call
# boto3.resource() and one of the two resource handles would leak. The
# lock + re-check after acquire is the canonical fix and adds zero
# LOCK cost after the first call — the outer `is None` short-circuits
# without acquiring the lock; per-attribute `__getattr__` indirection
# remains. Peer-review HIGH (openrouter).
import threading

_ddb = None
_table_real = None
_init_lock = threading.Lock()


class _LazyTable:
    def __getattr__(self, name):
        global _ddb, _table_real
        if _table_real is None:
            with _init_lock:
                if _table_real is None:
                    _ddb = boto3.resource("dynamodb")
                    _table_real = _ddb.Table(_TABLE_NAME)
        return getattr(_table_real, name)


_table = _LazyTable()


def _inst_pk(install_id: int | str) -> str:
    return f"INST#{install_id}"


def _user_pk(github_user_id: int | str) -> str:
    return f"USER#{github_user_id}"


def record_installation(
    *,
    install_id: int,
    account_login: str,
    account_type: str,
    installed_by_user_id: int,
) -> None:
    """Idempotent upsert of INST#<id> META row on `installation:created`.

    On `installation:deleted` callers should use `delete_installation`
    instead — this function only writes.

    Uses an atomic UpdateExpression with `if_not_exists(installed_at)`
    so concurrent duplicate webhook deliveries can't race-overwrite
    the original install timestamp (Codex P2 follow-up to Greptile P2
    on PR #41 — read-then-put had a race window between two concurrent
    Lambda invocations).
    """
    now = datetime.now(timezone.utc).isoformat()
    _table.update_item(
        Key={"PK": _inst_pk(install_id), "SK": "META"},
        UpdateExpression=(
            "SET account_login = :login, "
            "account_type = :atype, "
            "installed_by_user_id = :by, "
            "GSI1PK = :gsi1pk, "
            "GSI1SK = :gsi1sk, "
            # if_not_exists preserves the original timestamp when the
            # row already has one — concurrent-safe.
            "installed_at = if_not_exists(installed_at, :now)"
        ),
        ExpressionAttributeValues={
            ":login": account_login,
            ":atype": account_type,
            ":by": str(installed_by_user_id),
            ":gsi1pk": str(installed_by_user_id),
            ":gsi1sk": _inst_pk(install_id),
            ":now": now,
        },
    )


def delete_installation(install_id: int) -> None:
    _table.delete_item(Key={"PK": _inst_pk(install_id), "SK": "META"})


def get_installation(install_id: int) -> dict[str, Any] | None:
    resp = _table.get_item(
        Key={"PK": _inst_pk(install_id), "SK": "META"},
        ConsistentRead=True,
    )
    return resp.get("Item")


def is_install_allowlisted(install_id: int) -> bool:
    """Return True iff INST#<id> exists AND its installer is allowlisted.

    Two-hop lookup — INST# row holds installed_by_user_id; USER# row
    holds the actual `allowlisted` bool. Returns False on any miss
    (unknown install, unknown user, allowlisted=False/missing).

    Webhook-safe: reads `allowlisted` directly, never touches token
    blobs (no KMS Decrypt call).
    """
    inst = get_installation(install_id)
    if not inst:
        log.info("allowlist_miss_no_install", extra={"install_id": install_id})
        return False
    user_id = inst.get("installed_by_user_id")
    if not user_id:
        log.warning(
            "allowlist_install_missing_user_id",
            extra={"install_id": install_id},
        )
        return False
    user = _table.get_item(
        Key={"PK": _user_pk(user_id), "SK": "META"},
        ConsistentRead=True,
        ProjectionExpression="allowlisted",
    ).get("Item")
    if not user:
        log.info(
            "allowlist_miss_no_user",
            extra={"install_id": install_id, "user_id": user_id},
        )
        return False
    allowlisted = bool(user.get("allowlisted", False))
    if not allowlisted:
        log.info(
            "allowlist_miss_user_not_allowlisted",
            extra={"install_id": install_id, "user_id": user_id},
        )
    return allowlisted


def list_allowlisted_installs() -> list[int]:
    """Return the install_ids whose installer is allowlisted — the
    reaction poller's per-cycle batch (#247b).

    There is no single partition for installs (`PK=INST#<id>`), so this
    SCANs for `SK=META AND begins_with(PK, "INST#")` and re-checks each
    via `is_install_allowlisted`. Paginated over `LastEvaluatedKey` so a
    >1MB page can't silently truncate the batch (same guard as
    `list_comment_records`). Scale note: a SCAN + per-install two-hop
    allowlist check is fine for a homelab's handful of installs; revisit
    (a GSI on an `allowlisted` attribute) if the install count grows large.
    """
    install_ids: list[int] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": "SK = :meta AND begins_with(PK, :inst)",
        "ExpressionAttributeValues": {":meta": "META", ":inst": "INST#"},
        "ProjectionExpression": "PK",
    }
    while True:
        resp = _table.scan(**kwargs)
        for item in resp.get("Items", []):
            pk = item.get("PK", "")
            _, sep, id_str = pk.partition("#")
            if not sep:
                continue
            try:
                iid = int(id_str)
            except (TypeError, ValueError):
                # Unreachable under the write path (_inst_pk formats an int);
                # if it fires it's data corruption — log rather than silently
                # drop an install that should be polled.
                log.warning("install_pk_unparsable", extra={"pk": pk})
                continue
            if is_install_allowlisted(iid):
                install_ids.append(iid)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return install_ids


# ---------------------------------------------------------------------------
# Per-repo persona toggles
# ---------------------------------------------------------------------------

# Default for v1: TPM + Elder enabled on every repo unless an override
# row says otherwise. Newly-installed users get value out-of-the-box;
# opt-out is explicit per repo. `code_reviewer_blocking` defaults False
# (advisory mode: check-run conclusion=neutral, review event=COMMENT)
# so a noisy false-positive LLM run doesn't block PR velocity. Operator
# flips to blocking via dashboard once trust is established.
_DEFAULT_PERSONA_CONFIG = {
    "tpm_enabled": True,
    "code_reviewer_enabled": True,
    "code_reviewer_blocking": False,
}


def _repo_sk(repo_id: int | str) -> str:
    return f"REPO#{repo_id}"


def list_user_installations(github_user_id: str) -> list[dict[str, Any]]:
    """Return INST# rows installed by this user via the GSI1 index."""
    resp = _table.query(
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :pk AND begins_with(GSI1SK, :sk)",
        ExpressionAttributeValues={
            ":pk": str(github_user_id),
            ":sk": "INST#",
        },
    )
    return resp.get("Items", [])


def get_repo_config(install_id: int, repo_id: int) -> dict[str, Any]:
    """Resolved RepoConfig (persona toggles + enforcement state fields);
    defaults from `_DEFAULT_PERSONA_CONFIG` when no row exists."""
    resp = _table.get_item(
        Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
    )
    item = resp.get("Item")
    if not item:
        return {
            **_DEFAULT_PERSONA_CONFIG,
            "enforcement_ruleset_id": None,
            "force_disable_enforcement": False,
        }
    rid = item.get("enforcement_ruleset_id")
    return {
        "tpm_enabled": bool(item.get(
            "tpm_enabled", _DEFAULT_PERSONA_CONFIG["tpm_enabled"]
        )),
        "code_reviewer_enabled": bool(item.get(
            "code_reviewer_enabled",
            _DEFAULT_PERSONA_CONFIG["code_reviewer_enabled"],
        )),
        "code_reviewer_blocking": bool(item.get(
            "code_reviewer_blocking",
            _DEFAULT_PERSONA_CONFIG["code_reviewer_blocking"],
        )),
        "enforcement_ruleset_id": int(rid) if rid is not None else None,
        "force_disable_enforcement": bool(item.get("force_disable_enforcement", False)),
    }


def set_repo_config(
    *,
    install_id: int,
    repo_id: int,
    repo_full_name: str,
    tpm_enabled: bool,
    updated_by_user_id: str,
    code_reviewer_enabled: bool | None = None,
    code_reviewer_blocking: bool | None = None,
) -> dict[str, Any]:
    """Upsert per-repo override. Returns the FIELDS THAT WERE UPDATED
    (not the full resolved RepoConfig — callers needing that should
    follow up with `get_repo_config`).

    Uses update_item (not put_item) to preserve fields managed by
    other writers — e.g. enforcement_ruleset_id set by enforcement.py.

    `code_reviewer_*` kwargs are Optional so callers that only toggle
    TPM (e.g. legacy dashboard endpoints) don't have to pass them —
    None means "don't update this field." Falls back to the value
    already stored on the row, or to _DEFAULT_PERSONA_CONFIG.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Build SET expression dynamically — the kwargs are sparse.
    set_clauses = [
        "repo_full_name = :fn",
        "tpm_enabled = :te",
        "updated_at = :ua",
        "updated_by_user_id = :ub",
    ]
    values: dict[str, Any] = {
        ":fn": repo_full_name,
        ":te": bool(tpm_enabled),
        ":ua": now,
        ":ub": str(updated_by_user_id),
    }
    if code_reviewer_enabled is not None:
        set_clauses.append("code_reviewer_enabled = :cre")
        values[":cre"] = bool(code_reviewer_enabled)
    if code_reviewer_blocking is not None:
        set_clauses.append("code_reviewer_blocking = :crb")
        values[":crb"] = bool(code_reviewer_blocking)

    _table.update_item(
        Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
        UpdateExpression="SET " + ", ".join(set_clauses),
        ExpressionAttributeValues=values,
    )
    updated_fields = {"tpm_enabled": bool(tpm_enabled)}
    if code_reviewer_enabled is not None:
        updated_fields["code_reviewer_enabled"] = bool(code_reviewer_enabled)
    if code_reviewer_blocking is not None:
        updated_fields["code_reviewer_blocking"] = bool(code_reviewer_blocking)
    return updated_fields


def get_enforcement_id(install_id: int, repo_id: int) -> int | None:
    """Return the stored Grug-managed ruleset ID, or None."""
    resp = _table.get_item(
        Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
    )
    item = resp.get("Item")
    if not item:
        return None
    val = item.get("enforcement_ruleset_id")
    return int(val) if val is not None else None


def set_enforcement_id(
    install_id: int, repo_id: int, ruleset_id: int | None,
) -> None:
    """Update only the enforcement_ruleset_id field on a RepoConfig row."""
    if ruleset_id is not None:
        _table.update_item(
            Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
            UpdateExpression="SET enforcement_ruleset_id = :rid",
            ExpressionAttributeValues={":rid": ruleset_id},
        )
    else:
        _table.update_item(
            Key={"PK": _inst_pk(install_id), "SK": _repo_sk(repo_id)},
            UpdateExpression="REMOVE enforcement_ruleset_id",
        )


def is_persona_enabled(install_id: int, repo_id: int, persona: str) -> bool:
    """Webhook-style check: is `persona` enabled for this repo?

    Mirrored into services/webhook/adapters/install_store.py — the
    webhook calls this before TPM dispatch so a user can disable Grug
    on a noisy repo without uninstalling.
    """
    cfg = get_repo_config(install_id, repo_id)
    key = f"{persona}_enabled"
    return bool(cfg.get(key, _DEFAULT_PERSONA_CONFIG.get(key, True)))


# ---------------------------------------------------------------------------
# Elder reaction-poll comment records (#245)
# ---------------------------------------------------------------------------
#
# Each Grug-posted inline review comment is persisted so a scheduled
# poller (#245b) can later read its 👍/👎 reactions and attribute a
# `human_verdict` DD LLM Obs annotation to the review span that produced
# the finding. SK shape: `CRCOMMENT#<comment_id>` under the install PK.
# `last_verdict` is the dedup baseline — the poller only submits an
# annotation when the current reaction classification differs from it.


class CommentRecord(TypedDict):
    """A persisted Grug inline-comment, read by the reaction poller.

    Defined in this adapter (the lowest layer) so the persona engine
    imports the shape DOWN — same precedent as `JudgeFindingRepr` in
    llm_client. `last_verdict` is `NotRequired` (absent until the first
    reaction is polled) + a plain `Optional[str]`: the adapter persists
    the verdict opaquely and stays agnostic of the `ReactionVerdict`
    vocabulary (that lives in the LLM layer)."""
    comment_id: int
    repo: str
    pr_number: int
    review_span_context: Optional[dict]
    finding_tags: dict[str, str]
    last_verdict: NotRequired[Optional[str]]


def _comment_record_sk(comment_id: int | str) -> str:
    return f"CRCOMMENT#{comment_id}"


# Comment records auto-expire so the per-install CRCOMMENT# partition
# doesn't grow unbounded as PRs close (the poller queries it every
# cycle). 30 days comfortably outlives an open PR's active-review
# window; the table's DDB TTL (on the `ttl` attribute) is enabled in
# ddb_table.py — #272 actually turned it on (it had been asserted here
# since #247 but was never enabled; the runtime-trace audit caught the
# live table at DISABLED).
_COMMENT_RECORD_TTL_DAYS = 30


def put_comment_record(
    *,
    install_id: int,
    comment_id: int,
    repo: str,
    pr_number: int,
    review_span_context: dict,
    finding_tags: dict,
) -> None:
    """Persist a Grug inline-comment for later reaction polling. Idempotent
    upsert — re-posting the same comment_id overwrites (the span context
    + finding identity are stable per comment). `last_verdict` is left
    unset (None) so the first poll that sees a reaction always submits.
    `ttl` (epoch seconds) lets DDB auto-expire the record ~30 days out
    so the poll partition stays bounded as PRs close."""
    ttl = int(
        datetime.now(timezone.utc).timestamp()
        + _COMMENT_RECORD_TTL_DAYS * 86400
    )
    _table.put_item(Item={
        "PK": _inst_pk(install_id),
        "SK": _comment_record_sk(comment_id),
        "comment_id": int(comment_id),
        "repo": repo,
        "pr_number": int(pr_number),
        "review_span_context": review_span_context,
        "finding_tags": finding_tags,
        "ttl": ttl,
    })


class CheckVerdictRecord(TypedDict):
    """A persisted Check verdict — the atom of the Activity feed (PRD #301).

    One row per `(persona, head_sha)`: re-reviewing the same commit upserts
    (heals the row), a new commit appends. Stores the persona's RAW facts
    (ADR-0003); the `verdict` badge is denormalized here for cheap filtering
    but the canonical source is the raw facts (`review_types.verdict` re-derives
    it, so a future mapping change can fix history). `persona` is the caveman
    key (`chief`/`elder`, ADR-0002), never the legacy code key."""
    persona: str
    repo: str
    pr_number: int
    head_sha: str
    conclusion: str
    summary: str
    findings_count: int
    blocking: bool
    verdict: str
    created_at: str
    degraded_reason: NotRequired[Optional[str]]


# Activity rows auto-expire so the per-install ACT# partition stays bounded as
# PRs close. 90 days — a history feed wants longer than the 30-day comment
# window, but still finite. A GSI keyed by time is the noted scale upgrade.
_CHECK_VERDICT_TTL_DAYS = 90


def _check_verdict_sk(head_sha: str, persona: str) -> str:
    return f"ACT#{head_sha}#{persona}"


def put_check_verdict(
    *,
    install_id: int,
    persona: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    conclusion: str,
    summary: str,
    findings_count: int,
    blocking: bool,
    verdict: str,
    created_at: str,
    degraded_reason: Optional[str] = None,
) -> None:
    """Upsert a Check verdict. Idempotent per `(persona, head_sha)` via the SK
    — re-reviewing the same commit overwrites (heals the row), a new commit
    appends a fresh row. `ttl` (epoch seconds) auto-expires the row ~90 days
    out. `degraded_reason` is omitted from the item when None (kept sparse, the
    same opaque-optional discipline as `CommentRecord.last_verdict`)."""
    ttl = int(
        datetime.now(timezone.utc).timestamp()
        + _CHECK_VERDICT_TTL_DAYS * 86400
    )
    item: dict[str, Any] = {
        "PK": _inst_pk(install_id),
        "SK": _check_verdict_sk(head_sha, persona),
        "persona": persona,
        "repo": repo,
        "pr_number": int(pr_number),
        "head_sha": head_sha,
        "conclusion": conclusion,
        "summary": summary,
        "findings_count": int(findings_count),
        "blocking": bool(blocking),
        "verdict": verdict,
        "created_at": created_at,
        "ttl": ttl,
    }
    if degraded_reason is not None:
        item["degraded_reason"] = degraded_reason
    _table.put_item(Item=item)


def list_check_verdicts(
    install_id: int, limit: int = 50,
) -> list[CheckVerdictRecord]:
    """Return an install's Check verdicts, newest-first, capped at `limit`.

    Queries the `INST#` partition by `ACT#` SK prefix and sorts by `created_at`
    descending in-process: the SK encodes `head_sha` (for idempotency), not
    time, so DDB can't range-sort by time. Acceptable at current volume + the
    90-day TTL bound; a time-ordered GSI is the noted scale upgrade. Paginates
    via `LastEvaluatedKey` so a busy install isn't silently truncated at the
    1MB page cap before the sort."""
    rows: list[CheckVerdictRecord] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {":pk": _inst_pk(install_id), ":sk": "ACT#"},
    }
    while True:
        resp = _table.query(**kwargs)
        for item in resp.get("Items", []):
            rec: CheckVerdictRecord = {
                "persona": str(item["persona"]),
                "repo": str(item["repo"]),
                "pr_number": int(item["pr_number"]),
                "head_sha": str(item["head_sha"]),
                "conclusion": str(item["conclusion"]),
                "summary": str(item.get("summary", "")),
                "findings_count": int(item.get("findings_count", 0)),
                "blocking": bool(item.get("blocking", False)),
                "verdict": str(item.get("verdict", "")),
                "created_at": str(item["created_at"]),
            }
            if "degraded_reason" in item:
                rec["degraded_reason"] = item["degraded_reason"]
            rows.append(rec)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows[:limit]


# Async-Elder idempotency claim (#272). Keyed on the GitHub
# X-GitHub-Delivery UUID so a redelivery OR an AWS async-invoke retry of
# the same delivery claims-and-skips instead of double-posting a review.
# 24h comfortably outlives GitHub's redelivery window + AWS's async retry
# horizon; the `ttl` auto-expires the claim so the DELIVERY# partition
# stays bounded.
_DELIVERY_CLAIM_TTL_HOURS = 24


def _delivery_pk(delivery_id: str) -> str:
    return f"DELIVERY#{delivery_id}"


def claim_delivery(delivery_id: str) -> bool:
    """Win-once idempotency claim for an async Elder job.

    Returns ``True`` if THIS caller won the claim (first time this
    delivery is processed → proceed) and ``False`` if the delivery was
    already claimed (a GitHub redelivery or an AWS async-invoke retry →
    the caller must SKIP to avoid a duplicate review).

    Implemented as a conditional ``PutItem`` with
    ``attribute_not_exists(PK)`` — atomic at the DDB layer, so two
    concurrent invocations for the same delivery can't both win. A
    ``ConditionalCheckFailedException`` means already-claimed.

    Fails OPEN on an empty ``delivery_id`` (shouldn't happen post-HMAC,
    since GitHub always sends ``X-GitHub-Delivery``): without an id we
    can't dedup, and a possible double-review beats a silently-skipped
    one. Any non-conditional DDB error propagates — the caller
    (`run_elder_job`) catches it and degrades, but we do NOT swallow it
    here into a false "claimed" that would drop the review.
    """
    if not delivery_id:
        return True
    ttl = int(
        datetime.now(timezone.utc).timestamp()
        + _DELIVERY_CLAIM_TTL_HOURS * 3600
    )
    try:
        _table.put_item(
            Item={"PK": _delivery_pk(delivery_id), "SK": "META", "ttl": ttl},
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def list_comment_records(install_id: int) -> list[CommentRecord]:
    """Return all Grug comment records for an install (the poll batch).
    Scoped to the install PK + `CRCOMMENT#` SK prefix so another
    install's records can't leak into the batch.

    Paginates via `LastEvaluatedKey`: a single DDB query page caps at
    1MB, so a busy install with many open-PR comments would silently
    truncate the poll batch (some 👎 never seen) without this loop. The
    TTL on write bounds the partition, but a burst can still exceed one
    page between expirations."""
    out: list[CommentRecord] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {
            ":pk": _inst_pk(install_id),
            ":sk": "CRCOMMENT#",
        },
    }
    while True:
        resp = _table.query(**kwargs)
        for item in resp.get("Items", []):
            out.append({
                "comment_id": int(item["comment_id"]),
                "repo": item["repo"],
                "pr_number": int(item["pr_number"]),
                "review_span_context": item.get("review_span_context"),
                "finding_tags": item.get("finding_tags", {}),
                # None until the first reaction is polled + submitted.
                "last_verdict": item.get("last_verdict"),
            })
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return out


def update_comment_record_reaction(
    *, install_id: int, comment_id: int, verdict: str,
) -> None:
    """Advance the dedup baseline AFTER a successful DD submit. The
    compare (current classification vs `last_verdict`) + the submit-
    first/advance-after ordering live in the reactions.py module
    docstring; this function only writes the baseline."""
    _table.update_item(
        Key={"PK": _inst_pk(install_id), "SK": _comment_record_sk(comment_id)},
        UpdateExpression="SET last_verdict = :v",
        ExpressionAttributeValues={":v": verdict},
    )
