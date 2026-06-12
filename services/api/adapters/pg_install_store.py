# MIRRORED — sibling at services/webhook/adapters/pg_install_store.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Postgres install store - exact-API port of install_store.py (DDB).

Same public surface, same single-table layout (see install_store.py's
docstring for the PK/SK shapes); storage is the grug_kv table from
pg_base.py. At cutover install_store.py's implementation is REPLACED by
this module (no runtime dual-backend seam: DDB retires, the seam is
git + the image tag - rule-of-three, ADR-0001).

Semantics deliberately preserved from the DDB adapter:
- record_installation: atomic upsert that PRESERVES the original
  installed_at under concurrent duplicate deliveries (DDB
  if_not_exists -> single-statement jsonb upsert here).
- claim_delivery: win-once atomic claim (DDB conditional put ->
  INSERT ... ON CONFLICT with expired-row takeover; DDB's TTL machinery
  would have deleted an expired claim, Postgres must treat it as free).
- All reads filter TTL-expired rows (pg_base.TTL_LIVE) - DDB expiry is
  lazy-delete, ours is read-filter + hourly opportunistic purge.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, NotRequired, Optional, TypedDict

from psycopg.types.json import Jsonb

from adapters import pg_base
from adapters.pg_base import TTL_LIVE, decode_item, encode_attrs, get_pool
from review_types import verdict as _derive_verdict  # leaf import (no cycle)

log = logging.getLogger("grug.adapters.pg_install_store")


def _inst_pk(install_id: int | str) -> str:
    return f"INST#{install_id}"


def _user_pk(github_user_id: int | str) -> str:
    return f"USER#{github_user_id}"


def _get_item(pk: str, sk: str) -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT pk, sk, data FROM grug_kv WHERE pk = %s AND sk = %s AND {TTL_LIVE}",
            (pk, sk),
        ).fetchone()
    if not row:
        return None
    return decode_item(row[0], row[1], row[2])


def record_installation(
    *,
    install_id: int,
    account_login: str,
    account_type: str,
    installed_by_user_id: int,
) -> None:
    """Idempotent upsert of INST#<id> META row on `installation:created`.

    Single-statement upsert: concurrent duplicate webhook deliveries
    cannot race-overwrite the original install timestamp (the jsonb
    merge keeps the existing installed_at when present - same guarantee
    the DDB if_not_exists expression gave).
    """
    now = datetime.now(timezone.utc).isoformat()
    attrs = {
        "account_login": account_login,
        "account_type": account_type,
        "installed_by_user_id": str(installed_by_user_id),
        "GSI1PK": str(installed_by_user_id),
        "GSI1SK": _inst_pk(install_id),
        "installed_at": now,
    }
    pg_base.maybe_purge_expired()
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, gsi1pk, gsi1sk)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(g1pk)s, %(g1sk)s)
            ON CONFLICT (pk, sk) DO UPDATE SET
                data = (grug_kv.data || %(data)s)
                       || jsonb_build_object(
                              'installed_at',
                              COALESCE(grug_kv.data->'installed_at',
                                       %(data)s->'installed_at')),
                gsi1pk = %(g1pk)s,
                gsi1sk = %(g1sk)s
            """,
            {
                "pk": _inst_pk(install_id),
                "sk": "META",
                "data": encode_attrs(attrs),
                "g1pk": attrs["GSI1PK"],
                "g1sk": attrs["GSI1SK"],
            },
        )


def delete_installation(install_id: int) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM grug_kv WHERE pk = %s AND sk = %s",
            (_inst_pk(install_id), "META"),
        )


def get_installation(install_id: int) -> dict[str, Any] | None:
    return _get_item(_inst_pk(install_id), "META")


def is_install_allowlisted(install_id: int) -> bool:
    """Return True iff INST#<id> exists AND its installer is allowlisted.

    Two-hop lookup, same shape + logging as the DDB adapter (callers
    and dashboards grep these event names).
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
    # PROJECTED read (audit H2): the DDB original used
    # ProjectionExpression="allowlisted" so the webhook NEVER holds the
    # OAuth blobs that live on the same row; Postgres has no IAM
    # projection, so this query is the enforcement now.
    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT data->'allowlisted' FROM grug_kv "
            f"WHERE pk = %s AND sk = 'META' AND {TTL_LIVE}",
            (_user_pk(user_id),),
        ).fetchone()
    user = {"allowlisted": row[0]} if row is not None else None
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
    """Install_ids whose installer is allowlisted (poller batch, #247b).

    The DDB version SCANned; here a single WHERE does it. The per-id
    re-check via is_install_allowlisted is kept for log parity (the
    allowlist_miss_* events are how the operator debugs poller gaps).
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT pk FROM grug_kv WHERE sk = 'META' AND pk LIKE %s AND {TTL_LIVE}",
            ("INST#%",),
        ).fetchall()
    install_ids: list[int] = []
    for (pk,) in rows:
        _, sep, id_str = pk.partition("#")
        if not sep:
            continue
        try:
            iid = int(id_str)
        except (TypeError, ValueError):
            log.warning("install_pk_unparsable", extra={"pk": pk})
            continue
        if is_install_allowlisted(iid):
            install_ids.append(iid)
    return install_ids


# ---------------------------------------------------------------------------
# Per-repo persona toggles
# ---------------------------------------------------------------------------

_DEFAULT_PERSONA_CONFIG = {
    "tpm_enabled": True,
    "code_reviewer_enabled": True,
    "code_reviewer_blocking": False,
}


def _repo_sk(repo_id: int | str) -> str:
    return f"REPO#{repo_id}"


def list_user_installations(github_user_id: str) -> list[dict[str, Any]]:
    """INST# rows installed by this user - the GSI1 query becomes an
    indexed column lookup."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT pk, sk, data FROM grug_kv
            WHERE gsi1pk = %s AND gsi1sk LIKE 'INST#%%' AND {TTL_LIVE}
            """,
            (str(github_user_id),),
        ).fetchall()
    return [decode_item(*r) for r in rows]


def get_repo_config(install_id: int, repo_id: int) -> dict[str, Any]:
    item = _get_item(_inst_pk(install_id), _repo_sk(repo_id))
    if not item:
        return {
            **_DEFAULT_PERSONA_CONFIG,
            "enforcement_ruleset_id": None,
            "force_disable_enforcement": False,
        }
    rid = item.get("enforcement_ruleset_id")
    return {
        "tpm_enabled": bool(
            item.get("tpm_enabled", _DEFAULT_PERSONA_CONFIG["tpm_enabled"])
        ),
        "code_reviewer_enabled": bool(
            item.get(
                "code_reviewer_enabled",
                _DEFAULT_PERSONA_CONFIG["code_reviewer_enabled"],
            )
        ),
        "code_reviewer_blocking": bool(
            item.get(
                "code_reviewer_blocking",
                _DEFAULT_PERSONA_CONFIG["code_reviewer_blocking"],
            )
        ),
        "enforcement_ruleset_id": int(rid) if rid is not None else None,
        "force_disable_enforcement": bool(item.get("force_disable_enforcement", False)),
    }


def _merge_attrs(pk: str, sk: str, attrs: dict[str, Any]) -> None:
    """Sparse jsonb merge-upsert (DDB update_item SET parity: creates the
    row when absent, merges fields when present).

    Known divergence (audit L8, deliberate): merging into a TTL-EXPIRED
    row does not clear its ttl - the write lands on a row reads ignore
    and the purge later removes. DDB would resurrect a sparse live row
    (its own latent bug: such rows KeyError in the list readers). The
    PG behavior is the safer of the two."""
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data) VALUES (%(pk)s, %(sk)s, %(data)s)
            ON CONFLICT (pk, sk) DO UPDATE SET data = grug_kv.data || %(data)s
            """,
            {"pk": pk, "sk": sk, "data": encode_attrs(attrs)},
        )


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
    """Upsert per-repo override; returns the FIELDS THAT WERE UPDATED
    (same contract as the DDB adapter). Sparse merge preserves fields
    managed by other writers (enforcement_ruleset_id)."""
    now = datetime.now(timezone.utc).isoformat()
    attrs: dict[str, Any] = {
        "repo_full_name": repo_full_name,
        "tpm_enabled": bool(tpm_enabled),
        "updated_at": now,
        "updated_by_user_id": str(updated_by_user_id),
    }
    if code_reviewer_enabled is not None:
        attrs["code_reviewer_enabled"] = bool(code_reviewer_enabled)
    if code_reviewer_blocking is not None:
        attrs["code_reviewer_blocking"] = bool(code_reviewer_blocking)
    _merge_attrs(_inst_pk(install_id), _repo_sk(repo_id), attrs)

    updated_fields: dict[str, Any] = {"tpm_enabled": bool(tpm_enabled)}
    if code_reviewer_enabled is not None:
        updated_fields["code_reviewer_enabled"] = bool(code_reviewer_enabled)
    if code_reviewer_blocking is not None:
        updated_fields["code_reviewer_blocking"] = bool(code_reviewer_blocking)
    return updated_fields


def get_enforcement_id(install_id: int, repo_id: int) -> int | None:
    item = _get_item(_inst_pk(install_id), _repo_sk(repo_id))
    if not item:
        return None
    val = item.get("enforcement_ruleset_id")
    return int(val) if val is not None else None


def set_enforcement_id(
    install_id: int,
    repo_id: int,
    ruleset_id: int | None,
) -> None:
    """Update only enforcement_ruleset_id (REMOVE semantics for None)."""
    if ruleset_id is not None:
        _merge_attrs(
            _inst_pk(install_id),
            _repo_sk(repo_id),
            {"enforcement_ruleset_id": ruleset_id},
        )
    else:
        with get_pool().connection() as conn:
            conn.execute(
                """
                UPDATE grug_kv SET data = data - 'enforcement_ruleset_id'
                WHERE pk = %s AND sk = %s
                """,
                (_inst_pk(install_id), _repo_sk(repo_id)),
            )


def is_persona_enabled(install_id: int, repo_id: int, persona: str) -> bool:
    cfg = get_repo_config(install_id, repo_id)
    key = f"{persona}_enabled"
    return bool(cfg.get(key, _DEFAULT_PERSONA_CONFIG.get(key, True)))


# ---------------------------------------------------------------------------
# Elder reaction-poll comment records (#245)
# ---------------------------------------------------------------------------


class CommentRecord(TypedDict):
    comment_id: int
    repo: str
    pr_number: int
    review_span_context: Optional[dict]
    finding_tags: dict[str, str]
    last_verdict: NotRequired[Optional[str]]


def _comment_record_sk(comment_id: int | str) -> str:
    return f"CRCOMMENT#{comment_id}"


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
    ttl = int(
        datetime.now(timezone.utc).timestamp() + _COMMENT_RECORD_TTL_DAYS * 86400
    )
    attrs = {
        "comment_id": int(comment_id),
        "repo": repo,
        "pr_number": int(pr_number),
        "review_span_context": review_span_context,
        "finding_tags": finding_tags,
        "ttl": ttl,
    }
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = %(data)s, ttl = %(ttl)s
            """,
            {
                "pk": _inst_pk(install_id),
                "sk": _comment_record_sk(comment_id),
                "data": encode_attrs(attrs),
                "ttl": ttl,
            },
        )


class CheckVerdictRecord(TypedDict):
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
    created_at: str,
    degraded_reason: Optional[str] = None,
) -> None:
    """Upsert a Check verdict; the denormalized `verdict` badge is DERIVED
    via review_types.verdict (never a parameter) - ADR-0003 invariant
    enforced by construction, identical to the DDB adapter."""
    ttl = int(
        datetime.now(timezone.utc).timestamp() + _CHECK_VERDICT_TTL_DAYS * 86400
    )
    attrs: dict[str, Any] = {
        "persona": persona,
        "repo": repo,
        "pr_number": int(pr_number),
        "head_sha": head_sha,
        "conclusion": conclusion,
        "summary": summary,
        "findings_count": int(findings_count),
        "blocking": bool(blocking),
        "verdict": _derive_verdict(
            conclusion=conclusion,
            findings_count=int(findings_count),
            degraded_reason=degraded_reason,
        ),
        "created_at": created_at,
        "ttl": ttl,
    }
    if degraded_reason:
        attrs["degraded_reason"] = degraded_reason
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = %(data)s, ttl = %(ttl)s
            """,
            {
                "pk": _inst_pk(install_id),
                "sk": _check_verdict_sk(head_sha, persona),
                "data": encode_attrs(attrs),
                "ttl": ttl,
            },
        )


def list_check_verdicts(
    install_id: int,
    limit: int | None = 50,
) -> list[CheckVerdictRecord]:
    """An install's Check verdicts, newest-first. Sorting moves into SQL
    (created_at is an ISO-8601 string; lexicographic == chronological);
    the limit is still applied AFTER the full fetch shape the DDB
    adapter documented (callers like /activity re-filter)."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT pk, sk, data FROM grug_kv
            WHERE pk = %s AND sk LIKE 'ACT#%%' AND {TTL_LIVE}
            ORDER BY data->>'created_at' COLLATE "C" DESC
            """,
            (_inst_pk(install_id),),
        ).fetchall()
    out: list[CheckVerdictRecord] = []
    for pk, sk, data in rows:
        item = decode_item(pk, sk, data)
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
        out.append(rec)
    return out if limit is None else out[:limit]


_DELIVERY_CLAIM_TTL_HOURS = 24


def _delivery_pk(delivery_id: str) -> str:
    return f"DELIVERY#{delivery_id}"


def claim_delivery(delivery_id: str) -> bool:
    """Win-once idempotency claim. True = this caller won; False = the
    delivery was already claimed (redelivery / async retry -> SKIP).

    Atomic single statement. The ON CONFLICT UPDATE arm takes over a row
    ONLY when its claim has TTL-expired - DDB's TTL machinery would have
    deleted such a row, so an expired claim must read as free here too.
    Fails OPEN on empty delivery_id (same reasoning as the DDB adapter:
    a possible double-review beats a silently-skipped one). Any other
    database error propagates - callers degrade explicitly, we never
    swallow into a false 'claimed'.
    """
    if not delivery_id:
        return True
    pg_base.maybe_purge_expired()
    ttl = int(
        datetime.now(timezone.utc).timestamp() + _DELIVERY_CLAIM_TTL_HOURS * 3600
    )
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, 'META', %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = %(data)s, ttl = %(ttl)s
                WHERE grug_kv.ttl IS NOT NULL
                  AND grug_kv.ttl <= EXTRACT(EPOCH FROM now())
            RETURNING pk
            """,
            {
                "pk": _delivery_pk(delivery_id),
                "data": Jsonb({"ttl": ttl}),
                "ttl": ttl,
            },
        ).fetchone()
    return row is not None


def list_comment_records(install_id: int) -> list[CommentRecord]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT pk, sk, data FROM grug_kv
            WHERE pk = %s AND sk LIKE 'CRCOMMENT#%%' AND {TTL_LIVE}
            """,
            (_inst_pk(install_id),),
        ).fetchall()
    out: list[CommentRecord] = []
    for pk, sk, data in rows:
        item = decode_item(pk, sk, data)
        out.append(
            {
                "comment_id": int(item["comment_id"]),
                "repo": item["repo"],
                "pr_number": int(item["pr_number"]),
                "review_span_context": item.get("review_span_context"),
                "finding_tags": item.get("finding_tags", {}),
                "last_verdict": item.get("last_verdict"),
            }
        )
    return out


def update_comment_record_reaction(
    *,
    install_id: int,
    comment_id: int,
    verdict: str,
) -> None:
    _merge_attrs(
        _inst_pk(install_id),
        _comment_record_sk(comment_id),
        {"last_verdict": verdict},
    )
