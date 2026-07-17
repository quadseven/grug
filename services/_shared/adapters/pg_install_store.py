"""Postgres install store - exact-API port of the DDB install_store.

Same public surface, same single-table layout (see the "Single-table
layout (grug_kv)" glossary row in CONTEXT.md / specs/DESIGN.md for the
PK/SK shapes); storage is the grug_kv table from pg_base.py. Since the
#354 swap install_store.py is a facade re-exporting this module (no
runtime dual-backend seam: the rollback seam is git + the image tag -
rule-of-three, ADR-0001).

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
from typing import Any, Literal, NotRequired, Optional, TypedDict, cast

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

# Repo-level flags that are NOT persona enable/blocking pairs (those live
# in _DEFAULT_PERSONA_CONFIG, locked to the registry). Writable through
# set_repo_config; read with an explicit default in get_repo_config.
_EXTRA_REPO_FLAGS = frozenset({"dep_watch_enabled"})

# Repo-level flags whose value is a STRING, not a bool (the persona flags and
# _EXTRA_REPO_FLAGS are all bool). elder_voice (#288/#578) is "caveman" | "sage".
# These are exempt from the bool-only value check and validated per-flag below.
_STR_REPO_FLAGS = frozenset({"elder_voice"})

_DEFAULT_PERSONA_CONFIG = {
    "tpm_enabled": True,
    "code_reviewer_enabled": True,
    "code_reviewer_blocking": True,  # Elder blocks on real findings (fails open on degraded); matches registry blocking_default
    "guard_enabled": True,
    "guard_blocking": False,
    "warder_enabled": False,
    "pulse_enabled": False,
    "smasher_enabled": False,  # execution tracer (#469): opt-in per repo
    "walkthrough_enabled": True,  # Teller PR-walkthrough comment (#554)
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
    # A missing row and an empty row are the same shape: every field
    # falls through to its default below (no separate early-return to
    # keep in sync). Persona flags derive from _DEFAULT_PERSONA_CONFIG's
    # keys (#465, ADR-0010): adding a persona's flags to the default
    # dict makes them flow through this read path with no edit here.
    item = _get_item(_inst_pk(install_id), _repo_sk(repo_id)) or {}
    rid = item.get("enforcement_ruleset_id")
    cfg: dict[str, Any] = {
        flag: bool(item.get(flag, default))
        for flag, default in _DEFAULT_PERSONA_CONFIG.items()
    }
    cfg["enforcement_ruleset_id"] = int(rid) if rid is not None else None
    cfg["force_disable_enforcement"] = bool(item.get("force_disable_enforcement", False))
    cfg["dep_watch_enabled"] = bool(item.get("dep_watch_enabled", False))
    # Elder voice pack (#288/#578): stored only for entitled installs that
    # opted in; every other repo reads the caveman free default.
    cfg["elder_voice"] = str(item.get("elder_voice") or "caveman")
    return cfg


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
    updated_by_user_id: str,
    **persona_flags: bool | str | None,
) -> dict[str, Any]:
    """Upsert per-repo override; returns the FIELDS THAT WERE UPDATED
    (same contract as the DDB adapter). Sparse merge preserves fields
    managed by other writers (enforcement_ruleset_id).

    Persona flags arrive as keyword arguments validated against
    _DEFAULT_PERSONA_CONFIG's keys (#465, ADR-0010): any key in the
    default dict is writable, so a new persona's flags work here with
    no edit; an unknown key raises TypeError, preserving the explicit-
    signature era's unexpected-keyword behavior (typo protection).
    None = leave the stored value alone (sparse merge)."""
    unknown = (
        set(persona_flags)
        - set(_DEFAULT_PERSONA_CONFIG)
        - _EXTRA_REPO_FLAGS
        - _STR_REPO_FLAGS
    )
    if unknown:
        raise TypeError(
            f"set_repo_config() got unknown persona flag(s) {sorted(unknown)}; "
            f"known flags: {sorted(_DEFAULT_PERSONA_CONFIG)}"
        )
    # Values get the same rigor as keys (audit #477 M3): bool(value)
    # would silently store True for a truthy non-bool like "false" if a
    # caller ever passed a query-string value through. String-valued flags
    # (_STR_REPO_FLAGS, e.g. elder_voice) are exempt here and validated by
    # their own per-flag guard below.
    non_bool = {
        flag: value for flag, value in persona_flags.items()
        if flag not in _STR_REPO_FLAGS
        and value is not None and not isinstance(value, bool)
    }
    if non_bool:
        raise TypeError(
            f"set_repo_config() persona flags must be bool or None; got {non_bool!r}"
        )
    # elder_voice (#288/#578): must be "caveman" or "sage", and "sage" is a
    # paid pack gated on the install allowlist. This runs AFTER the flag is
    # accepted (it is in _STR_REPO_FLAGS) so the gate is actually reachable.
    voice = persona_flags.get("elder_voice")
    if voice is not None:
        if voice not in ("caveman", "sage"):
            raise ValueError(
                f"elder_voice must be one of ('caveman', 'sage'), got {voice!r}"
            )
        if voice == "sage" and not is_install_allowlisted(install_id):
            raise ValueError(
                f"elder_voice='sage' requires an allowlisted (paid) "
                f"installation; install {install_id} is not entitled"
            )
    now = datetime.now(timezone.utc).isoformat()
    updated_fields: dict[str, Any] = {
        flag: value
        for flag, value in persona_flags.items()
        if value is not None
    }
    attrs: dict[str, Any] = {
        "repo_full_name": repo_full_name,
        "updated_at": now,
        "updated_by_user_id": str(updated_by_user_id),
        **updated_fields,
    }
    _merge_attrs(_inst_pk(install_id), _repo_sk(repo_id), attrs)
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


class CommentFindingOrigin(TypedDict):
    backend: str
    model: str
    review_span_context: Optional[dict]


class CommentRecord(TypedDict):
    comment_id: int
    repo: str
    pr_number: int
    review_span_context: Optional[dict]
    finding_tags: dict[str, str]
    finding_origins: NotRequired[list[CommentFindingOrigin]]
    finding_text: NotRequired[str]
    head_sha: NotRequired[str]
    author_login: NotRequired[str]
    trust_reactors: NotRequired[bool]
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
    review_span_context: Optional[dict],
    finding_tags: dict[str, str],
    finding_origins: Optional[list[CommentFindingOrigin]] = None,
    finding_text: str = "",
    head_sha: str = "",
    author_login: str = "",
    trust_reactors: bool = True,
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
    if finding_origins:
        attrs["finding_origins"] = finding_origins
    if finding_text:
        attrs["finding_text"] = finding_text
    if head_sha:
        attrs["head_sha"] = head_sha
    if author_login:
        attrs["author_login"] = author_login
    if trust_reactors:
        attrs["trust_reactors"] = True
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


# --- Living Hunt: last completed Elder head per PR (#557) -----------------
# After a successful Elder pass we remember the head we reviewed so the next
# synchronize can scope the LLM to the delta (last..head) instead of the full
# PR base..head. SK is install-scoped; data carries the full repo name.

_ELDER_LAST_TTL_DAYS = 90


def _elder_last_sk(repo: str, pr_number: int) -> str:
    return f"ELDER#LAST#{repo}#{int(pr_number)}"


def put_elder_last_reviewed(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> None:
    """Record the head Elder just finished reviewing for this PR."""
    if not head_sha:
        return
    ttl = int(
        datetime.now(timezone.utc).timestamp() + _ELDER_LAST_TTL_DAYS * 86400
    )
    attrs = {
        "repo": repo,
        "pr_number": int(pr_number),
        "head_sha": head_sha,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
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
                "sk": _elder_last_sk(repo, pr_number),
                "data": encode_attrs(attrs),
                "ttl": ttl,
            },
        )


def get_elder_last_reviewed(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
) -> str | None:
    """Return the last Elder-reviewed head for this PR, or None."""
    item = _get_item(_inst_pk(install_id), _elder_last_sk(repo, pr_number))
    if not item:
        return None
    head = item.get("head_sha")
    return str(head) if head else None


# --- Review-findings ledger (#361 slice 1) ------------------------------
# Ledger rows are REPO-scoped (not install-scoped like the activity feed),
# so they live under their own pk=`LEDGER#<repo>` partition. The access
# pattern is the SAME grug_kv prefix-scan the activity feed uses; the sk
# encodes (class, pr, reviewer, seq) so a class prefix query is natural.

def _ledger_pk(repo: str) -> str:
    return f"LEDGER#{repo}"


def _ledger_sk(finding_class: str, pr: int, reviewer: str, digest: str) -> str:
    # zero-pad pr so lexicographic == numeric ordering within a class; the
    # trailing digest is CONTENT-derived (not ingest order) so the same
    # finding always maps to the same key - true idempotency across a
    # reordered corpus (Qodo review #536).
    return f"{finding_class}#{pr:07d}#{reviewer}#{digest}"


def _ledger_digest(row: dict[str, Any]) -> str:
    """Stable 12-hex identity of a finding from its content (finding text +
    timestamp + evidence) - independent of ingest order."""
    import hashlib
    material = "\x1f".join((
        str(row.get("finding", "")), str(row.get("ts", "")),
        str(row.get("evidence", "")),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def put_ledger_row(row: dict[str, Any]) -> None:
    """Upsert one ledger finding as a first-class grug_kv row. `row` is the
    raw JSONL dict ({repo, pr, reviewer, severity, class, finding, verdict,
    evidence, ts, commit}). The key is CONTENT-derived, so re-ingesting the
    corpus (even reordered) heals rows in place instead of duplicating."""
    repo = str(row["repo"])
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data)
            VALUES (%(pk)s, %(sk)s, %(data)s)
            ON CONFLICT (pk, sk) DO UPDATE SET data = %(data)s
            """,
            {
                "pk": _ledger_pk(repo),
                "sk": _ledger_sk(str(row["class"]), int(row["pr"]),
                                 str(row["reviewer"]), _ledger_digest(row)),
                "data": encode_attrs(dict(row)),
            },
        )


def list_ledger_rows(repo: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Every ledger finding for a repo (the corpus #527 / few-shot read),
    class-ordered. Same prefix-scan shape as list_check_verdicts."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT pk, sk, data FROM grug_kv
            WHERE pk = %s AND sk NOT IN ('PRACTICES', 'EXEMPLARS') AND {TTL_LIVE}
            -- keep the NOT IN list in lockstep with every derived-cache sk
            -- (PRACTICES #527, EXEMPLARS #538): a cached derivation leaking
            -- into the corpus scan feeds Elder its own output as ground truth
            ORDER BY sk COLLATE "C" ASC
            """,
            (_ledger_pk(repo),),
        ).fetchall()
    out = [decode_item(pk, sk, data) for pk, sk, data in rows]
    return out if limit is None else out[:limit]


def put_repo_practices(repo: str, practices: list[dict[str, Any]]) -> None:
    """Cache the derived best-practices for a repo (#527) - one row under
    the same LEDGER#<repo> partition, sk='PRACTICES'. Refreshed by the
    ingest/poller pass; read at review time to steer Elder's prompt."""
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data)
            VALUES (%(pk)s, %(sk)s, %(data)s)
            ON CONFLICT (pk, sk) DO UPDATE SET data = %(data)s
            """,
            {
                "pk": _ledger_pk(repo),
                "sk": "PRACTICES",
                "data": encode_attrs({"practices": practices}),
            },
        )


def get_repo_practices(repo: str) -> list[dict[str, Any]]:
    """The cached best-practices for a repo, or [] if none derived yet."""
    item = _get_item(_ledger_pk(repo), "PRACTICES")
    return list(item.get("practices", [])) if item else []


def put_repo_exemplars(repo: str, exemplars: list[dict[str, Any]]) -> None:
    """Cache the few-shot exemplars for a repo (#538) - one row under the
    same LEDGER#<repo> partition, sk='EXEMPLARS'. Refreshed by the ingest
    pass; read at review time for Elder's EXAMPLES section."""
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data)
            VALUES (%(pk)s, %(sk)s, %(data)s)
            ON CONFLICT (pk, sk) DO UPDATE SET data = %(data)s
            """,
            {
                "pk": _ledger_pk(repo),
                "sk": "EXEMPLARS",
                "data": encode_attrs({"exemplars": exemplars}),
            },
        )


def get_repo_exemplars(repo: str) -> list[dict[str, Any]]:
    """The cached few-shot exemplars for a repo, or [] if none derived."""
    item = _get_item(_ledger_pk(repo), "EXEMPLARS")
    return list(item.get("exemplars", [])) if item else []


# --- Reply-mined learnings (#670 slice 1, ADR-0020) ---------------------
# Learnings are OPERATOR-taught: a maintainer replied to a finding with a
# preference and grug's classifier restated it as a durable rule. They are
# repo-scoped like the ledger but live under their OWN partition
# (LEARN#<repo>) so they never collide with the ledger corpus scan or its
# PRACTICES / EXEMPLARS cache rows. Distinct from the outcome-taught ledger:
# see ADR-0020 for why the two corpora stay separate.


class Learning(TypedDict):
    text: str
    repo: str
    scope_path: NotRequired[str]
    source_pr: NotRequired[int]
    source_comment_id: NotRequired[int]
    author: NotRequired[str]
    created_at: NotRequired[str]
    usage_count: NotRequired[int]
    last_used_at: NotRequired[str]


def _learning_pk(repo: str) -> str:
    return f"LEARN#{repo}"


def _learning_digest(text: str) -> str:
    """Stable 12-hex identity of a learning from its rule text, so the same
    preference taught twice heals in place instead of duplicating."""
    import hashlib
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def _learning_sk(digest: str) -> str:
    return f"LEARNING#{digest}"


def _normalize_utc_iso(stamp: str) -> str:
    """Coerce a timestamp string to UTC ISO-8601 (+00:00), or now() when
    empty/unparseable. Uniform format keeps text ordering == time ordering."""
    if stamp:
        try:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


# Learnings decay if never reinforced: a taught rule that stays relevant gets
# re-taught (or its finding keeps recurring and the maintainer re-confirms),
# refreshing the TTL; a now-wrong rule ages out on its own. This bounds the
# blast radius of a stale rule in slice 1, which ships no delete command yet
# (the sibling PRACTICES/EXEMPLARS caches that ride the same prompt also expire).
_LEARNING_TTL_DAYS = 180


def put_learning(
    *,
    repo: str,
    text: str,
    scope_path: str = "",
    source_pr: int = 0,
    source_comment_id: int = 0,
    author: str = "",
    created_at: str = "",
) -> None:
    """Upsert one durable learning under LEARN#<repo>. Keyed by the rule
    text's content digest. A re-teach of the same text REFRESHES the mutable
    metadata (scope, source, author), the TTL, and `reinforced_at`, but
    PRESERVES the original created_at and usage counters. Listing orders by
    reinforced_at, so a re-taught old rule moves back into the prompt window
    (matching the 'remembered' ack) while first-taught provenance survives."""
    rule = text.strip()
    if not (repo and rule):
        return
    # Normalize the timestamp to a uniform UTC ISO format: listing orders by
    # this value AS TEXT, and lexicographic == chronological only when every
    # stored stamp shares one format/offset. A caller-supplied stamp in
    # another offset or precision would misorder the newest-first prompt
    # window (Qodo correctness).
    now = _normalize_utc_iso(created_at)
    ttl = int(datetime.now(timezone.utc).timestamp() + _LEARNING_TTL_DAYS * 86400)
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                -- take the NEW row's fields (incl. a fresh reinforced_at), but
                -- keep the ORIGINAL created_at + usage counters. Refreshes the
                -- TTL (use-it-or-lose-it) and lets a narrowed scope win.
                SET ttl = %(ttl)s,
                    data = EXCLUDED.data || jsonb_build_object(
                        'created_at', grug_kv.data->'created_at',
                        'usage_count', grug_kv.data->'usage_count',
                        'last_used_at', grug_kv.data->'last_used_at'
                    )
            """,
            {
                "pk": _learning_pk(repo),
                "sk": _learning_sk(_learning_digest(rule)),
                "ttl": ttl,
                "data": encode_attrs({
                    "text": rule,
                    "repo": repo,
                    "scope_path": scope_path,
                    "source_pr": int(source_pr),
                    "source_comment_id": int(source_comment_id),
                    "author": author,
                    "created_at": now,
                    "reinforced_at": now,
                    "usage_count": 0,
                    "last_used_at": "",
                }),
            },
        )


def list_learnings(repo: str, limit: int | None = None) -> list[Learning]:
    """Every durable learning for a repo, oldest first. Read at review time
    to steer Elder's prompt (ADR-0020). Empty list when none taught yet."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT pk, sk, data FROM grug_kv
            WHERE pk = %s AND sk LIKE 'LEARNING#%%' AND {TTL_LIVE}
            -- order by REINFORCED time (a re-teach bumps it), so a just-
            -- reinforced old rule moves into the newest-first prompt window;
            -- fall back to created_at for pre-reinforced_at rows.
            ORDER BY COALESCE(data->>'reinforced_at', data->>'created_at') ASC,
                     sk COLLATE "C" ASC
            """,
            (_learning_pk(repo),),
        ).fetchall()
    out = [cast("Learning", decode_item(pk, sk, data)) for pk, sk, data in rows]
    return out if limit is None else out[:limit]


def get_learning_by_source_comment(
    repo: str, source_comment_id: int,
) -> Optional[Learning]:
    """The learning taught by a specific reply comment, or None. Lets an SQS
    redelivery detect that this reply was already classified-and-stored, so
    it skips the non-deterministic classifier instead of re-running it and
    possibly storing a second, differently-worded rule (Qodo reliability)."""
    if not source_comment_id:
        return None
    for row in list_learnings(repo):
        if int(row.get("source_comment_id") or 0) == int(source_comment_id):
            return row
    return None


def get_comment_record(
    install_id: int, comment_id: int | str,
) -> Optional[CommentRecord]:
    """The stored CommentRecord for one of grug's own finding comments, or
    None. Used to join a maintainer's inline REPLY (via in_reply_to_id) back
    to the finding it answers - the same identity the reaction poller uses,
    but keyed to a single comment instead of scanning the whole install."""
    item = _get_item(_inst_pk(install_id), _comment_record_sk(comment_id))
    return cast("Optional[CommentRecord]", item) if item else None


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


_PULSE_NUDGE_TTL_DAYS = 7


_DEP_WATCH_TTL_DAYS = 7


def claim_dep_watch_report(install_id: int, repo: str) -> bool:
    """Win-once weekly claim for a Guard dependency quarantine report
    (#491) - same shape as claim_pulse_nudge."""
    pg_base.maybe_purge_expired()
    ttl = int(
        datetime.now(timezone.utc).timestamp() + _DEP_WATCH_TTL_DAYS * 86400
    )
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = %(data)s, ttl = %(ttl)s
                WHERE grug_kv.ttl IS NOT NULL
                  AND grug_kv.ttl <= EXTRACT(EPOCH FROM now())
            RETURNING pk
            """,
            {
                "pk": _inst_pk(install_id),
                "sk": f"DEPWATCH#{repo}",
                "data": Jsonb({"ttl": ttl}),
                "ttl": ttl,
            },
        ).fetchone()
    return row is not None


def release_dep_watch_report(install_id: int, repo: str) -> None:
    """Release a dep-watch claim whose report WRITE definitively failed
    (codex PR #492) - the claim must represent a FILED report, not an
    attempt. Best-effort."""
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM grug_kv WHERE pk = %s AND sk = %s",
            (_inst_pk(install_id), f"DEPWATCH#{repo}"),
        )


def list_dep_watch_repos(install_id: int) -> list[dict[str, Any]]:
    """Repo rows with dep_watch_enabled=true (#491) - the Pulse
    store-driven targeting pattern."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT sk, data FROM grug_kv
            WHERE pk = %s AND sk LIKE 'REPO#%%'
              AND data->>'dep_watch_enabled' = 'true' AND {TTL_LIVE}
            """,
            (_inst_pk(install_id),),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for sk, data in rows:
        _, sep, id_str = sk.partition("#")
        try:
            rid = int(id_str)
        except (TypeError, ValueError):
            continue
        full = (data or {}).get("repo_full_name", "")
        if sep and full:
            out.append({"id": rid, "full_name": full})
    return out


def claim_pulse_nudge(install_id: int, repo: str, pr_number: int) -> bool:
    """Win-once claim for a Pulse stuck-PR nudge (#472): True = nudge now;
    False = a nudge inside the TTL window already happened (or a
    concurrent poller run won). Same atomic upsert-if-expired shape as
    claim_delivery. Best-effort caller: any DB error propagates."""
    pg_base.maybe_purge_expired()
    ttl = int(
        datetime.now(timezone.utc).timestamp() + _PULSE_NUDGE_TTL_DAYS * 86400
    )
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = %(data)s, ttl = %(ttl)s
                WHERE grug_kv.ttl IS NOT NULL
                  AND grug_kv.ttl <= EXTRACT(EPOCH FROM now())
            RETURNING pk
            """,
            {
                "pk": _inst_pk(install_id),
                "sk": f"PULSE#{repo}#{pr_number}",
                "data": Jsonb({"ttl": ttl}),
                "ttl": ttl,
            },
        ).fetchone()
    return row is not None


def list_pulse_enabled_repos(install_id: int) -> list[dict[str, Any]]:
    """Repo rows with pulse_enabled=true for an install (codex PR #489):
    Pulse targets CONFIGURED repos from the store instead of paginating
    /installation/repositories - an enabled repo can never be starved by
    a discovery-page prefix, and idle ticks cost zero GitHub calls.
    Returns [{"id": repo_id, "full_name": ...}] (the run_pulse shape)."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT sk, data FROM grug_kv
            WHERE pk = %s AND sk LIKE 'REPO#%%'
              AND data->>'pulse_enabled' = 'true' AND {TTL_LIVE}
            """,
            (_inst_pk(install_id),),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for sk, data in rows:
        _, sep, id_str = sk.partition("#")
        try:
            rid = int(id_str)
        except (TypeError, ValueError):
            continue
        full = (data or {}).get("repo_full_name", "")
        if sep and full:
            out.append({"id": rid, "full_name": full})
    return out


def release_pulse_nudge(install_id: int, repo: str, pr_number: int) -> None:
    """Release a pulse-nudge claim whose COMMENT POST failed (codex PR
    #489): the claim must represent a COMPLETED nudge, not an attempt -
    otherwise a transient GitHub failure silently burns the weekly slot
    for exactly the stale PR Pulse exists to recover. Best-effort."""
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM grug_kv WHERE pk = %s AND sk = %s",
            (_inst_pk(install_id), f"PULSE#{repo}#{pr_number}"),
        )


_REVIEW_CLAIM_TTL_DAYS = 30
ReviewClaimStatus = Literal["acquired", "completed", "busy"]


def _review_pk(
    install_id: int, repo: str, pr_number: int, persona: str, head_sha: str
) -> str:
    return f"REVIEW#{install_id}:{repo}:{pr_number}:{persona}:{head_sha}"


def claim_review(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    persona: str,
    head_sha: str,
) -> bool:
    """Win-once idempotency claim keyed on one exact review input (#397).

    ``head_sha`` is the historical API name; callers pass the canonical
    snapshot identity covering base, head, title, and body. True means this
    caller won and should review; False means that exact snapshot was already
    claimed. This remains distinct from the per-webhook ``claim_delivery``.

    Atomic single statement with the SAME TTL-takeover semantics as
    claim_delivery (an expired claim reads as free). Fails OPEN on a missing
    identity (a possible double-review beats a silently-skipped one). The
    claim is on ATTEMPT, not completion: an `errored` review heals via the
    rerun queue (#305/#418) and an explicit `/rerun` (#87) both re-run
    through `dispatch_code_review` DIRECTLY, bypassing this webhook-path gate,
    so neither idempotency layer wedges a legitimate re-review. Any other
    database error propagates - run_elder_job's fail-open catch runs the
    review anyway, never swallowing into a false 'claimed'."""
    if not head_sha:
        return True
    pg_base.maybe_purge_expired()
    ttl = int(
        datetime.now(timezone.utc).timestamp() + _REVIEW_CLAIM_TTL_DAYS * 86400
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
                "pk": _review_pk(install_id, repo, pr_number, persona, head_sha),
                "data": Jsonb({"ttl": ttl}),
                "ttl": ttl,
            },
        ).fetchone()
    return row is not None


def acquire_review_claim(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    persona: str,
    head_sha: str,
    owner_token: str,
    lease_seconds: int,
) -> ReviewClaimStatus:
    """Acquire a recoverable lease for one durable review attempt.

    Unlike ``claim_review``, this separates an in-flight attempt from a
    completed review. If a worker dies before it can release its claim, a later
    SQS delivery can atomically take over after the lease expires. Existing
    rows created by ``claim_review`` have no state marker and are treated as
    completed, preserving same-snapshot deduplication across deployments.
    """
    if not head_sha:
        return "acquired"
    if not owner_token:
        raise ValueError("owner_token must be non-empty")
    lease_seconds = max(1, int(lease_seconds))
    pg_base.maybe_purge_expired()
    now = int(datetime.now(timezone.utc).timestamp())
    lease_expires_at = now + lease_seconds
    row_ttl = now + _REVIEW_CLAIM_TTL_DAYS * 86400
    pk = _review_pk(install_id, repo, pr_number, persona, head_sha)
    in_progress = {
        "review_claim_state": "in_progress",
        "review_claim_owner": owner_token,
        "lease_expires_at": lease_expires_at,
        # Storage retention is deliberately longer than the ownership lease.
        # Otherwise the generic TTL purge can delete a live claim between
        # heartbeats. Takeover keys on lease_expires_at below.
        "ttl": row_ttl,
    }
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, 'META', %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = %(data)s, ttl = %(ttl)s
                WHERE (
                    grug_kv.data->>'review_claim_state' = 'in_progress'
                    AND COALESCE(
                        NULLIF(grug_kv.data->>'lease_expires_at', '')::bigint, 0
                    ) <= EXTRACT(EPOCH FROM now())
                ) OR (
                    grug_kv.ttl IS NOT NULL
                    AND grug_kv.ttl <= EXTRACT(EPOCH FROM now())
                )
            RETURNING pk
            """,
            {
                "pk": pk,
                "data": Jsonb(in_progress),
                "ttl": row_ttl,
            },
        ).fetchone()
        if row is not None:
            return "acquired"
        existing = conn.execute(
            "SELECT data->>'review_claim_state' FROM grug_kv "
            "WHERE pk = %s AND sk = 'META'",
            (pk,),
        ).fetchone()
    # A legacy claim or an explicit completed marker both mean that this
    # snapshot already finished. A concurrently released row returns busy so
    # the queue retries instead of incorrectly acknowledging the message.
    if existing is not None and existing[0] in {None, "completed"}:
        return "completed"
    return "busy"


def complete_review_claim(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    persona: str,
    head_sha: str,
    owner_token: str,
) -> bool:
    """Convert an owned in-flight lease into a 30-day completed claim."""
    if not head_sha:
        return True
    if not owner_token:
        raise ValueError("owner_token must be non-empty")
    ttl = int(
        datetime.now(timezone.utc).timestamp() + _REVIEW_CLAIM_TTL_DAYS * 86400
    )
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            UPDATE grug_kv
            SET data = %(data)s, ttl = %(ttl)s
            WHERE pk = %(pk)s AND sk = 'META'
              AND data->>'review_claim_state' = 'in_progress'
              AND data->>'review_claim_owner' = %(owner)s
            RETURNING pk
            """,
            {
                "pk": _review_pk(
                    install_id, repo, pr_number, persona, head_sha,
                ),
                "owner": owner_token,
                "data": Jsonb({
                    "review_claim_state": "completed",
                    "ttl": ttl,
                }),
                "ttl": ttl,
            },
        ).fetchone()
    return row is not None


def renew_review_claim(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    persona: str,
    head_sha: str,
    owner_token: str,
    lease_seconds: int,
) -> bool:
    """Extend one owned in-flight lease without changing its identity."""
    if not head_sha:
        return True
    if not owner_token:
        raise ValueError("owner_token must be non-empty")
    lease_seconds = max(1, int(lease_seconds))
    now = int(datetime.now(timezone.utc).timestamp())
    lease_expires_at = now + lease_seconds
    row_ttl = now + _REVIEW_CLAIM_TTL_DAYS * 86400
    lease_data = {
        "lease_expires_at": lease_expires_at,
        "ttl": row_ttl,
    }
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            UPDATE grug_kv
            SET data = data || %(lease_data)s, ttl = %(ttl)s
            WHERE pk = %(pk)s AND sk = 'META'
              AND data->>'review_claim_state' = 'in_progress'
              AND data->>'review_claim_owner' = %(owner)s
            RETURNING pk
            """,
            {
                "pk": _review_pk(
                    install_id, repo, pr_number, persona, head_sha,
                ),
                "owner": owner_token,
                "lease_data": Jsonb(lease_data),
                "ttl": row_ttl,
            },
        ).fetchone()
    return row is not None


def release_review_claim(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    persona: str,
    head_sha: str,
    owner_token: str | None = None,
) -> bool:
    """Release one exact review-input claim.

    Durable review workers call this only when the claimed attempt did not
    complete: the snapshot became stale, dispatch raised, or publication
    failed. A later SQS delivery can then claim the same snapshot and finish
    the review. Successful reviews retain the claim and continue to deduplicate
    same-snapshot webhook events for the normal 30-day TTL. When
    ``owner_token`` is provided, deletion is conditional on ownership so a
    slow worker cannot delete a lease that a newer worker took over.
    """
    if not head_sha:
        return True
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            DELETE FROM grug_kv
            WHERE pk = %(pk)s AND sk = 'META'
              AND (CAST(%(owner)s AS text) IS NULL
                   OR data->>'review_claim_owner' = %(owner)s)
            RETURNING pk
            """,
            {
                "pk": _review_pk(
                    install_id, repo, pr_number, persona, head_sha,
                ),
                "owner": owner_token,
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
        record: CommentRecord = {
            "comment_id": int(item["comment_id"]),
            "repo": item["repo"],
            "pr_number": int(item["pr_number"]),
            "review_span_context": item.get("review_span_context"),
            "finding_tags": item.get("finding_tags", {}),
            "last_verdict": item.get("last_verdict"),
        }
        if item.get("finding_origins"):
            record["finding_origins"] = item["finding_origins"]
        if item.get("finding_text"):
            record["finding_text"] = str(item["finding_text"])
        if item.get("head_sha"):
            record["head_sha"] = str(item["head_sha"])
        if item.get("author_login"):
            record["author_login"] = str(item["author_login"])
        if item.get("trust_reactors"):
            record["trust_reactors"] = True
        out.append(record)
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
