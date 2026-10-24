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
from collections.abc import Mapping
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


def list_all_install_ids() -> list[int]:
    """Every live INST#<id> row's id, regardless of allowlist status
    (grug#842: reconciliation needs the UNFILTERED set - a row whose
    installer fell off the allowlist is still a row that exists, and an
    install missing from GitHub entirely is exactly what this compares
    against `list_app_installations()` to find)."""
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
            install_ids.append(int(id_str))
        except (TypeError, ValueError):
            log.warning("install_pk_unparsable", extra={"pk": pk})
    return install_ids


def list_allowlisted_installs() -> list[int]:
    """Install_ids whose installer is allowlisted (poller batch, #247b).

    The DDB version SCANned; here a single WHERE does it. The per-id
    re-check via is_install_allowlisted is kept for log parity (the
    allowlist_miss_* events are how the operator debugs poller gaps).
    """
    return [iid for iid in list_all_install_ids() if is_install_allowlisted(iid)]


# ---------------------------------------------------------------------------
# Per-repo persona toggles
# ---------------------------------------------------------------------------

# Repo-level flags that are NOT persona enable/blocking pairs (those live
# in _DEFAULT_PERSONA_CONFIG, locked to the registry). Writable through
# set_repo_config; read with an explicit default in get_repo_config.
_EXTRA_REPO_FLAGS = frozenset(
    {
        "dep_watch_enabled",
        "reopen_watch_enabled",
        "guard_hygiene_watch_enabled",
        # Chief's ISSUE-time DoR advisory. Its OWN flag, not `tpm_enabled`:
        # turning Chief on for pull requests must not silently start
        # commenting on every issue in the repo. Default OFF.
        "issue_dor_enabled",
        # grug#947: opt-in for the check-run sweep/reconcile periodic pass -
        # same store-driven targeting as dep_watch/pulse, not a blanket
        # every-repo scan. Default OFF.
        "check_run_reconcile_enabled",
    }
)

# Repo-level flags whose value is a STRING, not a bool (the persona flags and
# _EXTRA_REPO_FLAGS are all bool). elder_voice (#288/#578) is "caveman" | "sage".
# These are exempt from the bool-only value check and validated per-flag below.
_STR_REPO_FLAGS = frozenset({"elder_voice"})

# Repo-level flags whose value is a list of strings. guard_hygiene_dead_ref_
# patterns (#778) is per-install infrastructure data (decommissioned host/
# path names) - it CANNOT ship as a hardcoded list in this PUBLIC repo the
# way workflow_hygiene.py's diff-time list does, so it is read back here
# from the store instead. Exempt from the bool-only value check; validated
# per-flag below (list of non-empty str, or None to leave unchanged).
_LIST_REPO_FLAGS = frozenset({"guard_hygiene_dead_ref_patterns"})

# Repo-level flags whose value is a positive INTEGER (grug#1041: the Elder
# complexity caps). Unset reads back as None, which the scanner resolves to its
# env default, so the default is defined in exactly one place. The upper bound
# keeps a typo from silently disabling the source.
_INT_REPO_FLAGS = frozenset({"complexity_cyclomatic_cap", "complexity_cognitive_cap"})
_INT_REPO_FLAG_MAX = 1000

# Repo-level flags whose value is a non-negative INTEGER SECONDS duration
# (learning approval delay). Zero means "immediate / active on teach".
_DURATION_REPO_FLAGS = frozenset({"learning_approval_delay_seconds"})
_LEARNING_APPROVAL_DELAY_MAX_SECONDS = 30 * 86400  # 30 days cap

_DEFAULT_PERSONA_CONFIG = {
    "tpm_enabled": True,
    "code_reviewer_enabled": True,
    "code_reviewer_blocking": True,  # Elder blocks on real findings (fails open on degraded); matches registry blocking_default
    "guard_enabled": True,
    "guard_blocking": False,
    "warder_enabled": False,
    "warder_gate_blocking": False,  # grug#533: SLO/monitor deploy-gate, advisory by default
    "sentinel_enabled": True,  # safety net, not an opt-in tracer (grug#721)
    "pulse_enabled": False,
    "pulse_comment_nudge_enabled": False,  # #656: suggested-fix replies on stale threads
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


def _read_int_repo_flag(flag: str, raw: Any) -> int | None:
    """The stored value of an _INT_REPO_FLAGS flag, or None when it is absent or
    not an int in [1, _INT_REPO_FLAG_MAX] (the same range the write path
    enforces). Never raises; a present-but-unreadable value is logged so a bad
    cell does not silently fall back to the default."""
    if raw is None:
        return None
    if not isinstance(raw, bool) and isinstance(raw, int) and 1 <= raw <= _INT_REPO_FLAG_MAX:
        return raw
    log.warning(
        "repo_config_int_flag_invalid",
        extra={"flag": flag, "kind": type(raw).__name__},
    )
    return None


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
    cfg["force_disable_enforcement"] = bool(
        item.get("force_disable_enforcement", False)
    )
    # Learning approval delay: seconds a newly-taught learning stays pending
    # before becoming active. Zero (default) = active immediately.
    cfg["learning_approval_delay_seconds"] = int(
        item.get("learning_approval_delay_seconds") or 0
    )
    # Derived from _EXTRA_REPO_FLAGS rather than one hand-written line per
    # flag (#655). The old form needed an edit in TWO places to add a flag,
    # and nothing enforced the pair - a flag added to _EXTRA_REPO_FLAGS but
    # missed here would validate on write and then read back as absent,
    # silently disabling the feature it gates. All such flags default False.
    for _flag in _EXTRA_REPO_FLAGS:
        cfg[_flag] = bool(item.get(_flag, False))
    # Elder voice pack (#288/#578): stored only for entitled installs that
    # opted in; every other repo reads the caveman free default.
    cfg["elder_voice"] = str(item.get("elder_voice") or "caveman")
    # Guard hygiene-watch dead-ref patterns (#778): absent row/field reads
    # back as (), the same "unconfigured, not clean" shape scan_dead_refs
    # already treats as a no-op - the visibility fix is the caller logging
    # that empty state, not this read path.
    cfg["guard_hygiene_dead_ref_patterns"] = tuple(
        str(p) for p in (item.get("guard_hygiene_dead_ref_patterns") or ())
    )
    # grug#1041: None = unset; the scanner applies its env default. A stored
    # value that is not a clean in-range int (hand-edited row, legacy bool) also
    # reads back as unset: callers like the dashboard listing and the
    # enforcement pass have no fallback around this read.
    for _flag in _INT_REPO_FLAGS:
        cfg[_flag] = _read_int_repo_flag(_flag, item.get(_flag))
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


def _validate_str_repo_flags(persona_flags: dict[str, Any], install_id: int) -> None:
    """Per-flag validation for _STR_REPO_FLAGS values, split out of
    set_repo_config (#778, Elder: cyclomatic 17 > cap 15) so a scalar
    flag's semantics live in one seam instead of growing the write path
    every time a new kind of flag is added.

    elder_voice (#288/#578): must be "caveman" or "sage", and "sage" is a
    paid pack gated on the install allowlist. Runs AFTER the caller has
    already confirmed the flag is known (it is in _STR_REPO_FLAGS)."""
    voice = persona_flags.get("elder_voice")
    if voice is None:
        return
    if voice not in ("caveman", "sage"):
        raise ValueError(
            f"elder_voice must be one of ('caveman', 'sage'), got {voice!r}"
        )
    if voice == "sage" and not is_install_allowlisted(install_id):
        raise ValueError(
            f"elder_voice='sage' requires an allowlisted (paid) "
            f"installation; install {install_id} is not entitled"
        )


def _validate_list_repo_flags(persona_flags: dict[str, Any]) -> None:
    """Per-flag validation for _LIST_REPO_FLAGS values, split out of
    set_repo_config for the same reason as _validate_str_repo_flags: the
    write path's branching should not grow with every new flag KIND.

    Loops over _LIST_REPO_FLAGS rather than naming
    guard_hygiene_dead_ref_patterns directly, so the next list-valued
    flag needs no new branch here - only a new frozenset member. Each
    supplied value must be a list of non-empty strings; guard_hygiene_
    dead_ref_patterns (#778) is the first of these - decommissioned-infra
    names that are the ONLY writer path into the store (they never
    appear in this repo's source, see hygiene_watch.py's module
    docstring), so a typo here is silent for the whole category rather
    than caught by a linter."""
    for flag in _LIST_REPO_FLAGS:
        value = persona_flags.get(flag)
        if value is None:
            continue
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ValueError(
                f"{flag} must be a list of non-empty strings, got {value!r}"
            )


def _validate_int_repo_flags(persona_flags: dict[str, Any]) -> None:
    """Each supplied _INT_REPO_FLAGS value must be an int in
    [1, _INT_REPO_FLAG_MAX]. bool is an int subclass, so it is refused
    explicitly."""
    for flag in _INT_REPO_FLAGS:
        value = persona_flags.get(flag)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= _INT_REPO_FLAG_MAX
        ):
            raise ValueError(
                f"{flag} must be a positive integer <= {_INT_REPO_FLAG_MAX}, "
                f"got {value!r}"
            )


def _validate_duration_repo_flags(persona_flags: dict[str, Any]) -> None:
    """Each supplied _DURATION_REPO_FLAGS value must be a non-negative int
    of seconds, capped at _LEARNING_APPROVAL_DELAY_MAX_SECONDS. Zero means
    "active immediately" (the default)."""
    for flag in _DURATION_REPO_FLAGS:
        value = persona_flags.get(flag)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= _LEARNING_APPROVAL_DELAY_MAX_SECONDS
        ):
            raise ValueError(
                f"{flag} must be a non-negative integer of seconds "
                f"<= {_LEARNING_APPROVAL_DELAY_MAX_SECONDS}, got {value!r}"
            )


def set_repo_config(
    *,
    install_id: int,
    repo_id: int,
    repo_full_name: str,
    updated_by_user_id: str,
    **persona_flags: bool | int | str | list[str] | None,
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
        - _LIST_REPO_FLAGS
        - _INT_REPO_FLAGS
        - _DURATION_REPO_FLAGS
    )
    if unknown:
        raise TypeError(
            f"set_repo_config() got unknown persona flag(s) {sorted(unknown)}; "
            f"known flags: {sorted(_DEFAULT_PERSONA_CONFIG)}"
        )
    # Values get the same rigor as keys (audit #477 M3): bool(value)
    # would silently store True for a truthy non-bool like "false" if a
    # caller ever passed a query-string value through. String-valued flags
    # (_STR_REPO_FLAGS, e.g. elder_voice) and list-valued flags
    # (_LIST_REPO_FLAGS, e.g. guard_hygiene_dead_ref_patterns) are exempt
    # here and validated by their own per-kind helper below.
    non_bool = {
        flag: value
        for flag, value in persona_flags.items()
        if flag not in _STR_REPO_FLAGS
        and flag not in _LIST_REPO_FLAGS
        and flag not in _INT_REPO_FLAGS
        and flag not in _DURATION_REPO_FLAGS
        and value is not None
        and not isinstance(value, bool)
    }
    if non_bool:
        raise TypeError(
            f"set_repo_config() persona flags must be bool or None; got {non_bool!r}"
        )
    _validate_str_repo_flags(persona_flags, install_id)
    _validate_list_repo_flags(persona_flags)
    _validate_int_repo_flags(persona_flags)
    _validate_duration_repo_flags(persona_flags)
    now = datetime.now(timezone.utc).isoformat()
    updated_fields: dict[str, Any] = {
        flag: value for flag, value in persona_flags.items() if value is not None
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
    # Living review (#1187): the finding's annotated state, `{"state":
    # "addressed"|"resolved"|"dismissed", "sha": <short sha or "">}`, or
    # absent/None while the finding is open.
    resolution: NotRequired[Optional[dict[str, str]]]
    # Epoch seconds when the record expires (created + the record TTL); the
    # reaction poll derives a comment's age from it.
    ttl: NotRequired[int]


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
    ttl = int(datetime.now(timezone.utc).timestamp() + _COMMENT_RECORD_TTL_DAYS * 86400)
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
    ttl = int(datetime.now(timezone.utc).timestamp() + _CHECK_VERDICT_TTL_DAYS * 86400)
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


def get_check_verdict(
    install_id: int,
    head_sha: str,
    persona: str,
) -> Optional[CheckVerdictRecord]:
    """One persona's stored Check verdict for one commit, or None if that
    persona never posted a verdict for this head_sha (or it has expired
    past its 90-day TTL). Direct pk+sk point lookup - same shape as
    `get_comment_record` - so a caller checking "did Elder's last review
    on this PR still have blocking findings" costs one row read, not a
    bulk `list_check_verdicts` scan."""
    item = _get_item(_inst_pk(install_id), _check_verdict_sk(head_sha, persona))
    return cast("Optional[CheckVerdictRecord]", item) if item else None


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
    ttl = int(datetime.now(timezone.utc).timestamp() + _ELDER_LAST_TTL_DAYS * 86400)
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


# --- Hunt Board: canonical review session per PR (grug#1207) --------------
# One row per PR holding `review_session.to_dict` output. Writes are
# compare-and-swap on the session's `version` so concurrent personas never
# overwrite each other (`review_session.update_session` re-reads and
# re-applies on a lost race). Install-scoped like ELDER#LAST.

_REVIEW_SESSION_TTL_DAYS = 90


def _review_session_sk(repo: str, pr_number: int) -> str:
    return f"HUNT#{repo}#{int(pr_number)}"


def get_review_session(
    *, install_id: int, repo: str, pr_number: int,
) -> dict[str, Any] | None:
    """The stored session dict (with its `version`), or None for none yet."""
    item = _get_item(_inst_pk(install_id), _review_session_sk(repo, pr_number))
    if not item:
        return None
    item.pop("PK", None)
    item.pop("SK", None)
    item.pop("ttl", None)
    return item


def put_review_session(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    data: dict[str, Any],
    expected_version: int,
) -> bool:
    """Store `data` only if the stored version is still `expected_version`
    (0 = no live row). Returns False when another writer got there first.

    An expired row counts as absent, the same takeover rule as
    claim_delivery, since reads already treat it as gone.
    """
    ttl = int(datetime.now(timezone.utc).timestamp() + _REVIEW_SESSION_TTL_DAYS * 86400)
    params = {
        "pk": _inst_pk(install_id),
        "sk": _review_session_sk(repo, pr_number),
        "data": encode_attrs({**data, "ttl": ttl}),
        "ttl": ttl,
        "expected": str(int(expected_version)),
    }
    with get_pool().connection() as conn:
        if int(expected_version) == 0:
            cur = conn.execute(
                """
                INSERT INTO grug_kv (pk, sk, data, ttl)
                VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
                ON CONFLICT (pk, sk) DO UPDATE
                    SET data = EXCLUDED.data, ttl = EXCLUDED.ttl
                    WHERE grug_kv.ttl IS NOT NULL
                      AND grug_kv.ttl <= EXTRACT(EPOCH FROM now())
                """,
                params,
            )
        else:
            cur = conn.execute(
                f"""
                UPDATE grug_kv SET data = %(data)s, ttl = %(ttl)s
                WHERE pk = %(pk)s AND sk = %(sk)s
                  AND data->>'version' = %(expected)s
                  AND {TTL_LIVE}
                """,  # noqa: S608 - TTL_LIVE is a fixed pg_base clause, not input
                params,
            )
        return cur.rowcount == 1


# --- Elder auto-pause: push log + pause row per PR (grug#1188) -----------
# Two rows beside ELDER#LAST. The push log holds the epoch seconds of the
# PR's recent pushes; the pause row exists while Elder is paused. Both are
# install-scoped like ELDER#LAST.


def _elder_pushes_sk(repo: str, pr_number: int) -> str:
    return f"ELDER#PUSHES#{repo}#{int(pr_number)}"


def _elder_paused_sk(repo: str, pr_number: int) -> str:
    return f"ELDER#PAUSED#{repo}#{int(pr_number)}"


def record_elder_push(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    window_seconds: int,
    pushed_at: int | None = None,
) -> int:
    """Append one push and return how many pushes fall inside the window.

    One atomic statement: rapid pushes arrive as concurrent webhook
    deliveries, and a read-modify-write would drop some of them. Entries
    older than the window are pruned on the same write. The row's TTL is
    one window past the newest push, so an expired row only holds pushes
    the cutoff would drop anyway.
    """
    now = int(pushed_at if pushed_at is not None else datetime.now(timezone.utc).timestamp())
    cutoff = now - int(window_seconds)
    attrs = {"repo": repo, "pr_number": int(pr_number), "pushes": [now]}
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = jsonb_set(
                        grug_kv.data, '{pushes}',
                        COALESCE(
                            (SELECT jsonb_agg(p)
                             FROM jsonb_array_elements(grug_kv.data->'pushes') AS p
                             WHERE p::numeric > %(cutoff)s),
                            '[]'::jsonb
                        ) || to_jsonb(%(now)s::bigint)
                    ),
                    ttl = %(ttl)s
            RETURNING jsonb_array_length(data->'pushes')
            """,
            {
                "pk": _inst_pk(install_id),
                "sk": _elder_pushes_sk(repo, pr_number),
                "data": encode_attrs(attrs),
                "ttl": now + int(window_seconds),
                "cutoff": cutoff,
                "now": now,
            },
        ).fetchone()
    return int(row[0]) if row else 1


def pause_elder(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> bool:
    """Pause Elder's per-push reviews for this PR. True only for the caller
    that moved it from not paused to paused, so the pause note posts once.
    An expired pause row reads as free (same TTL takeover as claim_delivery).
    """
    ttl = int(datetime.now(timezone.utc).timestamp() + _ELDER_LAST_TTL_DAYS * 86400)
    attrs = {
        "repo": repo,
        "pr_number": int(pr_number),
        "head_sha": head_sha,
        "paused_at": datetime.now(timezone.utc).isoformat(),
    }
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
                "sk": _elder_paused_sk(repo, pr_number),
                "data": encode_attrs(attrs),
                "ttl": ttl,
            },
        ).fetchone()
    return row is not None


def is_elder_paused(*, install_id: int, repo: str, pr_number: int) -> bool:
    """True while Elder's per-push reviews are paused for this PR."""
    return _get_item(_inst_pk(install_id), _elder_paused_sk(repo, pr_number)) is not None


def resume_elder(*, install_id: int, repo: str, pr_number: int) -> bool:
    """Lift the pause and clear the push log. True when a live pause was
    lifted. The push log goes too: pushes made before the resume must not
    re-pause Elder on the first push after it."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            DELETE FROM grug_kv
            WHERE pk = %(pk)s AND sk IN (%(paused)s, %(pushes)s)
            RETURNING sk, {TTL_LIVE}
            """,  # noqa: S608 - TTL_LIVE is a fixed pg_base clause, not input
            {
                "pk": _inst_pk(install_id),
                "paused": _elder_paused_sk(repo, pr_number),
                "pushes": _elder_pushes_sk(repo, pr_number),
            },
        ).fetchall()
    paused_sk = _elder_paused_sk(repo, pr_number)
    return any(sk == paused_sk and live for sk, live in rows)


# --- Accepted guard removals: one row per PR head (grug#1405) -------------
# `/grug accept-removal <path>` from a trusted maintainer answers a
# `removed-guarded-path` finding. The head SHA is part of the key, so an
# acceptance can never carry over to a new head: a push reads a different
# row, which is empty until someone accepts again.


def _removal_accept_sk(repo: str, pr_number: int, head_sha: str) -> str:
    return f"REMOVAL#ACCEPT#{repo}#{int(pr_number)}#{head_sha}"


def record_removal_acceptance(
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    head_sha: str,
    path: str,
    accepted_by: str,
    comment_id: int,
) -> None:
    """Record that `accepted_by` accepted deleting `path` on `head_sha`.

    One atomic upsert that merges the path into the head's row, so two
    acceptances landing at once both stay. An expired row is replaced, not
    merged into. Raises on a database error; the
    caller tells the commenter it did not take."""
    if not head_sha or not path or not accepted_by:
        raise ValueError("an acceptance needs a head, a path and who accepted it")
    ttl = int(datetime.now(timezone.utc).timestamp() + _ELDER_LAST_TTL_DAYS * 86400)
    entry = {path: {
        "accepted_by": accepted_by,
        "comment_id": int(comment_id),
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }}
    attrs = {"repo": repo, "pr_number": int(pr_number), "head_sha": head_sha,
             "accepted": entry}
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO grug_kv (pk, sk, data, ttl)
            VALUES (%(pk)s, %(sk)s, %(data)s, %(ttl)s)
            ON CONFLICT (pk, sk) DO UPDATE
                SET data = CASE
                        WHEN grug_kv.ttl IS NOT NULL
                             AND grug_kv.ttl <= EXTRACT(EPOCH FROM now())
                        THEN %(data)s
                        ELSE jsonb_set(
                            grug_kv.data, '{accepted}',
                            COALESCE(grug_kv.data->'accepted', '{}'::jsonb)
                                || %(entry)s
                        )
                    END,
                    ttl = %(ttl)s
            """,
            {
                "pk": _inst_pk(install_id),
                "sk": _removal_accept_sk(repo, pr_number, head_sha),
                "data": encode_attrs(attrs),
                "entry": encode_attrs(entry),
                "ttl": ttl,
            },
        )


def get_removal_acceptances(
    *, install_id: int, repo: str, pr_number: int, head_sha: str,
) -> dict[str, dict[str, Any]]:
    """The accepted guard removals on exactly this head, by path. Empty for
    any other head. Raises on a database error; the caller keeps the
    finding."""
    if not head_sha:
        return {}
    item = _get_item(_inst_pk(install_id), _removal_accept_sk(repo, pr_number, head_sha))
    accepted = (item or {}).get("accepted")
    if not isinstance(accepted, dict):
        return {}
    return {
        str(path): dict(row) for path, row in accepted.items()
        if isinstance(row, dict) and row.get("accepted_by")
    }


# --- Review-findings ledger (#361 slice 1) ------------------------------
# Ledger rows are REPO-scoped (not install-scoped like the activity feed),
# so they live under their own pk=`LEDGER#<repo>` partition. The access
# pattern is the SAME grug_kv prefix-scan the activity feed uses; the sk
# encodes (class, pr, reviewer, seq) so a class prefix query is natural.


def _ledger_pk(repo: str) -> str:
    return f"LEDGER#{repo}"


def _ledger_sk(finding_class: str, pr: int | None, reviewer: str, digest: str) -> str:
    # zero-pad pr so lexicographic == numeric ordering within a class; the
    # trailing digest is CONTENT-derived (not ingest order) so the same
    # finding always maps to the same key - true idempotency across a
    # reordered corpus (LORE review #536).
    #
    # `pr` may be None (#764): a consensus row written outside a PR context.
    # `NOPR` is deliberately non-numeric - under the `COLLATE "C"` byte
    # ordering the scan uses, 'N' (0x4E) sorts after every digit (0x30-0x39),
    # so unattributed rows land at the end of their class instead of
    # colliding with a real PR number. A zero-pad sentinel like 0000000
    # would be indistinguishable from PR #0.
    pr_part = f"{pr:07d}" if pr is not None else "NOPR"
    return f"{finding_class}#{pr_part}#{reviewer}#{digest}"


def _ledger_digest(row: dict[str, Any]) -> str:
    """Stable 12-hex identity of a finding from its content (finding text +
    timestamp + evidence) - independent of ingest order."""
    import hashlib

    material = "\x1f".join(
        (
            str(row.get("finding", "")),
            str(row.get("ts", "")),
            str(row.get("evidence", "")),
        )
    )
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
                "sk": _ledger_sk(
                    str(row["class"]),
                    # Mirror parse_row: null/absent pr is a valid row, not a
                    # crash. Without this the store raised TypeError on the
                    # same rows parse_row was silently dropping (#764).
                    int(row["pr"]) if row.get("pr") is not None else None,
                    str(row["reviewer"]),
                    _ledger_digest(row),
                ),
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
    # Cave Drawings (#1203) lifecycle. Absent on rows written before it.
    id: NotRequired[str]
    status: NotRequired[str]
    version: NotRequired[int]
    history: NotRequired[list[dict[str, Any]]]
    supersedes: NotRequired[str]
    superseded_by: NotRequired[str]
    conflicts_with: NotRequired[list[str]]
    approved_by: NotRequired[str]
    approved_at: NotRequired[str]
    last_used_review: NotRequired[str]


def _learning_pk(repo: str) -> str:
    return f"LEARN#{repo}"


def _learning_digest(text: str) -> str:
    """Stable 12-hex identity of a learning from its rule text, so the same
    preference taught twice heals in place instead of duplicating."""
    from cave_drawings import learning_id

    return learning_id(text)


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
    approval_delay_seconds: int = 0,
    active: bool | None = None,
) -> None:
    """Upsert one durable learning under LEARN#<repo>. Keyed by the rule
    text's content digest. A re-teach of the same text REFRESHES the mutable
    metadata (scope, source, author), the TTL, and `reinforced_at`, but
    PRESERVES the original created_at and usage counters. Listing orders by
    reinforced_at, so a re-taught old rule moves back into the prompt window
    (matching the 'remembered' ack) while first-taught provenance survives.

    `approval_delay_seconds` (org-configurable, default 0) is how long a
    newly-taught learning stays pending before becoming active. Zero =
    active immediately. When `active` is not given it is derived:
    active iff `approval_delay_seconds <= 0`. A pending learning is stored
    but not returned by `list_learnings` until its delay has elapsed."""
    rule = text.strip()
    if not (repo and rule):
        return
    now = _normalize_utc_iso(created_at)
    ttl = int(datetime.now(timezone.utc).timestamp() + _LEARNING_TTL_DAYS * 86400)
    delay = int(approval_delay_seconds)
    is_active = (delay <= 0) if active is None else bool(active)
    activated_at = now if is_active else ""
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
                    -- #1203: an operator's lifecycle decision (disabled,
                    -- pending, superseded), its history and its links are
                    -- never undone by the same reply being mined again.
                    || jsonb_strip_nulls(jsonb_build_object(
                        'status', grug_kv.data->'status',
                        'history', grug_kv.data->'history',
                        'version', grug_kv.data->'version',
                        'supersedes', grug_kv.data->'supersedes',
                        'superseded_by', grug_kv.data->'superseded_by',
                        'conflicts_with', grug_kv.data->'conflicts_with',
                        'approved_by', grug_kv.data->'approved_by',
                        'approved_at', grug_kv.data->'approved_at',
                        'last_used_review', grug_kv.data->'last_used_review'
                    ))
            """,
            {
                "pk": _learning_pk(repo),
                "sk": _learning_sk(_learning_digest(rule)),
                "ttl": ttl,
                "data": encode_attrs(
                    {
                        "text": rule,
                        "repo": repo,
                        "scope_path": scope_path,
                        "source_pr": int(source_pr),
                        "source_comment_id": int(source_comment_id),
                        "author": author,
                        "created_at": now,
                        "reinforced_at": now,
                        "active": is_active,
                        "approval_delay_seconds": delay,
                        "activated_at": activated_at,
                        "usage_count": 0,
                        "last_used_at": "",
                    }
                ),
            },
        )


def increment_learning_usage(repo: str, digest: str) -> None:
    """Mark one learning as used: bump usage_count and stamp last_used_at.
    Called when a learning is rendered into a review or /grug ask prompt.
    Best-effort: never blocks a review. TTL and reinforced_at are untouched
    (re-teaching is what refreshes relevance)."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_pool().connection() as conn:
            conn.execute(
                """
                UPDATE grug_kv
                SET data = jsonb_set(
                    jsonb_set(data, '{usage_count}',
                        to_jsonb(COALESCE((data->>'usage_count')::int, 0) + 1)),
                    '{last_used_at}', to_jsonb(%(now)s)
                )
                WHERE pk = %(pk)s AND sk = %(sk)s AND {TTL_LIVE}
                """,
                {"now": now, "pk": _learning_pk(repo), "sk": _learning_sk(digest)},
            )
    except Exception as e:  # noqa: BLE001 - usage tracking must not fail a review
        log.warning(
            "learning_increment_usage_failed",
            extra={"repo": repo, "digest": digest, "kind": type(e).__name__},
        )


def delete_learning(repo: str, digest: str) -> bool:
    """Delete one learning by (repo, text-digest). Returns True if a row
    was removed. Used by the /grug learnings delete command."""
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM grug_kv WHERE pk = %s AND sk = %s",
                (_learning_pk(repo), _learning_sk(digest)),
            )
    except Exception as e:  # noqa: BLE001 - never crash the command path
        log.warning(
            "learning_delete_failed",
            extra={"repo": repo, "digest": digest, "kind": type(e).__name__},
        )
        return False
    return bool(cur.rowcount)


def list_learnings(
    repo: str, limit: int | None = None, *, include_inactive: bool = False,
) -> list[Learning]:
    """Every durable learning for a repo that is ACTIVE now, oldest first.
    Read at review time to steer Elder's prompt (ADR-0020). Empty list when
    none taught (or none yet active) yet. A learning taught with a positive
    org approval delay stays pending until `created_at + delay` has elapsed;
    read-time activation needs no background job.

    `include_inactive=True` returns every live row whatever its status
    (pending, disabled, superseded): the operator view and redelivery
    detection need the whole set (#1203).
    """
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
    now = datetime.now(timezone.utc)
    out: list[Learning] = []
    for pk, sk, data in rows:
        row = _decode_learning(pk, sk, data)
        if include_inactive or _learning_is_active(row, now):
            out.append(row)
    return out if limit is None else out[:limit]


def _decode_learning(pk: str, sk: str, data: dict[str, Any]) -> Learning:
    """A learning row with its id (the SK digest) attached, so callers
    address it without recomputing the digest from the text."""
    item = decode_item(pk, sk, data)
    item["id"] = sk.split("#", 1)[1] if "#" in sk else ""
    return cast("Learning", item)


def _learning_is_active(row: Learning, now: datetime) -> bool:
    """True when this learning is active at `now`. One rule for every
    reader, owned by `cave_drawings.effective_status`: an operator's status
    (#1203) wins; otherwise the approval-delay flag decides."""
    from cave_drawings import STATUS_ACTIVE, effective_status

    return effective_status(cast("dict[str, Any]", row), now=now) == STATUS_ACTIVE


def get_learning_by_source_comment(
    repo: str,
    source_comment_id: int,
) -> Optional[Learning]:
    """The learning taught by a specific reply comment, or None. Lets an SQS
    redelivery detect that this reply was already classified-and-stored, so
    it skips the non-deterministic classifier instead of re-running it and
    possibly storing a second, differently-worded rule (LORE reliability)."""
    if not source_comment_id:
        return None
    # Every status: a reply whose learning is pending or disabled was still
    # already classified, and must not be classified again.
    for row in list_learnings(repo, include_inactive=True):
        if int(row.get("source_comment_id") or 0) == int(source_comment_id):
            return row
    return None


# --- Cave Drawings: learnings lifecycle (#1203) -------------------------
# The operator control plane over the rows above. Every mutation runs in
# one transaction with the row locked (SELECT ... FOR UPDATE), appends an
# entry to the row's `history`, and never touches the usage counters. The
# decisions themselves (which transitions are legal, what an import may
# create, which rows reach a prompt) live in the pure `cave_drawings`
# module; this section only persists them.

# Fields a read adds that must never be written back into the jsonb.
_LEARNING_READ_ONLY = ("PK", "SK", "id")


class LearningImportConflict(RuntimeError):
    """An import collided with a learning that appeared after its dry run.
    The whole import was rolled back."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _learning_ttl() -> int:
    return int(datetime.now(timezone.utc).timestamp() + _LEARNING_TTL_DAYS * 86400)


def _learning_data(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in _LEARNING_READ_ONLY}


def _lock_learning(conn: Any, repo: str, learning_id: str) -> Optional[Learning]:
    row = conn.execute(
        f"SELECT pk, sk, data FROM grug_kv WHERE pk = %s AND sk = %s AND {TTL_LIVE} "  # noqa: S608 - TTL_LIVE is a fixed pg_base clause, not input
        "FOR UPDATE",
        (_learning_pk(repo), _learning_sk(learning_id)),
    ).fetchone()
    return _decode_learning(row[0], row[1], row[2]) if row else None


def _write_learning(conn: Any, repo: str, learning_id: str, row: Mapping[str, Any]) -> None:
    conn.execute(
        "UPDATE grug_kv SET data = %s WHERE pk = %s AND sk = %s",
        (encode_attrs(_learning_data(row)), _learning_pk(repo), _learning_sk(learning_id)),
    )


def _history_entry(action: str, actor: str, **extra: Any) -> dict[str, Any]:
    return {"at": _now_iso(), "action": action, "actor": actor, **extra}


def get_learning(repo: str, learning_id: str) -> Optional[Learning]:
    """One learning by id, or None (missing or TTL-expired)."""
    item = _get_item(_learning_pk(repo), _learning_sk(learning_id))
    if not item:
        return None
    item["id"] = learning_id
    return cast("Learning", item)


def list_learning_repos() -> list[str]:
    """Every repository that holds at least one live learning, sorted."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT pk FROM grug_kv
            WHERE pk LIKE 'LEARN#%%' AND sk LIKE 'LEARNING#%%' AND {TTL_LIVE}
            """,  # noqa: S608 - TTL_LIVE is a fixed pg_base clause, not input
        ).fetchall()
    return sorted(pk.split("#", 1)[1] for (pk,) in rows)


def set_learning_status(
    repo: str, learning_id: str, status: str, *, actor: str,
) -> Learning:
    """Approve (-> active) or disable a learning. A superseded learning is
    history: it cannot be switched back on (edit the successor instead).
    Raises LookupError when missing, ValueError on an illegal transition."""
    from cave_drawings import (
        STATUS_ACTIVE, STATUS_DISABLED, STATUS_SUPERSEDED, effective_status,
    )

    if status not in (STATUS_ACTIVE, STATUS_DISABLED):
        raise ValueError(f"status must be {STATUS_ACTIVE} or {STATUS_DISABLED}")
    with get_pool().connection() as conn, conn.transaction():
        row = _lock_learning(conn, repo, learning_id)
        if row is None:
            raise LookupError(learning_id)
        current = effective_status(cast("dict[str, Any]", row))
        if current == STATUS_SUPERSEDED:
            raise ValueError("a superseded learning cannot change status")
        if current == status:
            return row
        out: dict[str, Any] = dict(row)
        out["status"] = status
        # Keep the approval-delay flag in step, so a reader of either field
        # sees the operator's decision.
        out["active"] = status == STATUS_ACTIVE
        action = "approved" if status == STATUS_ACTIVE else "disabled"
        if status == STATUS_ACTIVE:
            out["approved_by"] = actor
            out["approved_at"] = out["activated_at"] = _now_iso()
        out["history"] = [
            *(row.get("history") or []),
            _history_entry(action, actor, previous_status=current),
        ]
        _write_learning(conn, repo, learning_id, out)
    return cast("Learning", out)


def _validated_edit_text(text: str | None, scope_path: str | None) -> str | None:
    """The stripped new text (None = unchanged). Raises ValueError on a
    scope or text that no learning may carry."""
    from cave_drawings import MAX_TEXT_CHARS, validate_scope_path

    reason = validate_scope_path(scope_path) if scope_path is not None else None
    if reason:
        raise ValueError(reason)
    new_text = text.strip() if text is not None else None
    if new_text is not None and not (0 < len(new_text) <= MAX_TEXT_CHARS):
        raise ValueError(f"text must be 1..{MAX_TEXT_CHARS} characters")
    return new_text


def _in_place_changes(
    row: Learning, scope_path: str | None, conflicts_with: list[str] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The row with its new scope and conflict links, and a record of what
    actually changed (for the history entry)."""
    changes: dict[str, Any] = {}
    out: dict[str, Any] = dict(row)
    if scope_path is not None and scope_path != row.get("scope_path", ""):
        changes["scope_path"] = {"from": row.get("scope_path", ""), "to": scope_path}
        out["scope_path"] = scope_path
    if conflicts_with is not None:
        links = sorted(set(conflicts_with))
        if links != sorted(row.get("conflicts_with") or []):
            changes["conflicts_with"] = links
        out["conflicts_with"] = links
    return out, changes


def _clear_expired_learning(conn: Any, repo: str, learning_id: str) -> None:
    """An expired row is gone as far as every read is concerned; delete it
    so it cannot block an insert under the same key."""
    conn.execute(
        f"DELETE FROM grug_kv WHERE pk = %s AND sk = %s AND NOT {TTL_LIVE}",  # noqa: S608 - TTL_LIVE is a fixed pg_base clause, not input
        (_learning_pk(repo), _learning_sk(learning_id)),
    )


def _supersede_learning(
    conn: Any, repo: str, row: Learning, edited: dict[str, Any],
    *, new_text: str, actor: str, changes: dict[str, Any],
) -> dict[str, Any]:
    """Insert the successor of `row` carrying `new_text`, and retire `row`
    as superseded. Both rows keep the full history."""
    from cave_drawings import STATUS_SUPERSEDED, effective_status, learning_id as _lid

    old_id = str(row.get("id", ""))
    new_id = _lid(new_text)
    if _lock_learning(conn, repo, new_id) is not None:
        raise ValueError("a learning with that text already exists")
    history = list(row.get("history") or [])
    successor: dict[str, Any] = {
        **_learning_data(edited),
        "text": new_text,
        # The successor keeps the predecessor's state: editing a pending
        # rule does not approve it, editing a disabled one does not switch
        # it back on.
        "status": effective_status(cast("dict[str, Any]", row)),
        "version": int(row.get("version") or 1) + 1,
        "supersedes": old_id,
        "reinforced_at": _now_iso(),
        "usage_count": 0,
        "last_used_at": "",
        "last_used_review": "",
        "history": [*history, _history_entry(
            "edited", actor, previous_id=old_id,
            previous_text=row.get("text", ""), changes=changes,
        )],
    }
    successor.pop("superseded_by", None)
    _clear_expired_learning(conn, repo, new_id)
    conn.execute(
        "INSERT INTO grug_kv (pk, sk, data, ttl) VALUES (%s, %s, %s, %s)",
        (_learning_pk(repo), _learning_sk(new_id), encode_attrs(successor), _learning_ttl()),
    )
    retired: dict[str, Any] = {
        **row,
        "status": STATUS_SUPERSEDED,
        "superseded_by": new_id,
        "history": [*history, _history_entry("superseded", actor, superseded_by=new_id)],
    }
    _write_learning(conn, repo, old_id, retired)
    return {**successor, "id": new_id}


def edit_learning(
    repo: str,
    learning_id: str,
    *,
    actor: str,
    text: str | None = None,
    scope_path: str | None = None,
    conflicts_with: list[str] | None = None,
) -> Learning:
    """Edit a learning. Scope and conflict links change in place. A new
    TEXT is a new rule with a new id: the successor row is created with
    `supersedes`, a bumped version and the full history, and the old row
    becomes `superseded` (kept, never injected). One transaction.
    Raises LookupError when missing, ValueError on invalid input."""
    from cave_drawings import STATUS_SUPERSEDED, effective_status

    new_text = _validated_edit_text(text, scope_path)
    with get_pool().connection() as conn, conn.transaction():
        row = _lock_learning(conn, repo, learning_id)
        if row is None:
            raise LookupError(learning_id)
        if effective_status(cast("dict[str, Any]", row)) == STATUS_SUPERSEDED:
            raise ValueError("a superseded learning is history; edit its successor")
        for other in conflicts_with or []:
            if other == learning_id or _lock_learning(conn, repo, other) is None:
                raise ValueError(f"conflicts_with names an unknown learning {other!r}")
        out, changes = _in_place_changes(row, scope_path, conflicts_with)
        if new_text is not None and new_text != row.get("text"):
            return cast("Learning", _supersede_learning(
                conn, repo, row, out, new_text=new_text, actor=actor, changes=changes,
            ))
        if changes:
            out["history"] = [
                *(row.get("history") or []), _history_entry("edited", actor, changes=changes),
            ]
            _write_learning(conn, repo, learning_id, out)
    return cast("Learning", out)


def import_learnings(
    repo: str, learnings: list[dict[str, Any]], *, actor: str,
) -> list[str]:
    """Apply the `to_create` rows of a clean import plan, all or nothing.
    Every row lands `pending`: nothing an import brings reaches a review
    until an operator approves it. A row that collides with a learning
    created since the dry run aborts the whole import
    (LearningImportConflict) and nothing is written."""
    import psycopg

    from cave_drawings import STATUS_PENDING, learning_id as _lid

    ids: list[str] = []
    now = _now_iso()
    try:
        with get_pool().connection() as conn, conn.transaction():
            for item in learnings:
                text = str(item.get("text", "")).strip()
                lid = _lid(text)
                pk, sk = _learning_pk(repo), _learning_sk(lid)
                _clear_expired_learning(conn, repo, lid)
                data: dict[str, Any] = {
                    "text": text,
                    "repo": repo,
                    "scope_path": str(item.get("scope_path", "") or ""),
                    "source_pr": int(item.get("source_pr") or 0),
                    "source_comment_id": int(item.get("source_comment_id") or 0),
                    "author": str(item.get("author", "") or ""),
                    "created_at": _normalize_utc_iso(str(item.get("created_at", "") or "")),
                    "reinforced_at": now,
                    "usage_count": 0,
                    "last_used_at": "",
                    "status": STATUS_PENDING,
                    "active": False,
                    "version": 1,
                    "history": [_history_entry("imported", actor)],
                }
                for link in ("supersedes", "conflicts_with"):
                    if item.get(link):
                        data[link] = item[link]
                conn.execute(
                    "INSERT INTO grug_kv (pk, sk, data, ttl) VALUES (%s, %s, %s, %s)",
                    (pk, sk, encode_attrs(data), _learning_ttl()),
                )
                ids.append(lid)
    except psycopg.errors.UniqueViolation as e:
        raise LearningImportConflict(str(e).splitlines()[0]) from e
    return ids


def record_learning_usage(
    repo: str, learning_ids: list[str], *, review_key: str,
) -> int:
    """Count one use of each learning actually supplied to a review.
    Idempotent per `review_key` (PR number and head sha): the several prompt
    builds of one review (chunks, the judge, a backend fallback) count once.
    Returns how many rows were incremented."""
    if not (repo and learning_ids and review_key):
        return 0
    with get_pool().connection() as conn:
        cur = conn.execute(
            f"""
            UPDATE grug_kv
            SET data = data || jsonb_build_object(
                'usage_count', COALESCE((data->>'usage_count')::int, 0) + 1,
                'last_used_at', %(now)s::text,
                'last_used_review', %(key)s::text
            )
            WHERE pk = %(pk)s AND sk = ANY(%(sks)s) AND {TTL_LIVE}
              AND COALESCE(data->>'last_used_review', '') <> %(key)s
            """,  # noqa: S608 - TTL_LIVE is a fixed pg_base clause, not input
            {
                "now": _now_iso(),
                "key": review_key,
                "pk": _learning_pk(repo),
                "sks": [_learning_sk(i) for i in learning_ids],
            },
        )
        return cur.rowcount or 0


def get_comment_record(
    install_id: int,
    comment_id: int | str,
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
    ttl = int(datetime.now(timezone.utc).timestamp() + _DELIVERY_CLAIM_TTL_HOURS * 3600)
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
    ttl = int(datetime.now(timezone.utc).timestamp() + _DEP_WATCH_TTL_DAYS * 86400)
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


def _list_flag_enabled_repos(install_id: int, flag: str) -> list[dict[str, Any]]:
    """Repo rows with `flag`=true - the shared store-driven targeting
    pattern used by Pulse/dep_watch/reopen_watch (FLINT DRY finding,
    PR #744: list_dep_watch_repos and list_reopen_watch_repos were
    identical except for the flag column name)."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT sk, data FROM grug_kv
            WHERE pk = %s AND sk LIKE 'REPO#%%'
              AND data->>%s = 'true' AND {TTL_LIVE}
            """,
            (_inst_pk(install_id), flag),
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


def list_dep_watch_repos(install_id: int) -> list[dict[str, Any]]:
    """Repo rows with dep_watch_enabled=true (#491) - the Pulse
    store-driven targeting pattern."""
    return _list_flag_enabled_repos(install_id, "dep_watch_enabled")


def list_reopen_watch_repos(install_id: int) -> list[dict[str, Any]]:
    """Repo rows with reopen_watch_enabled=true (grug#730 audit finding) -
    same store-driven targeting pattern as dep_watch/pulse."""
    return _list_flag_enabled_repos(install_id, "reopen_watch_enabled")


def list_check_run_reconcile_repos(install_id: int) -> list[dict[str, Any]]:
    """Repo rows with check_run_reconcile_enabled=true (grug#947) - same
    store-driven targeting pattern as dep_watch/pulse/reopen_watch."""
    return _list_flag_enabled_repos(install_id, "check_run_reconcile_enabled")


def list_hygiene_watch_repos(install_id: int) -> list[dict[str, Any]]:
    """Repo rows with guard_hygiene_watch_enabled=true (#655) - same
    store-driven targeting pattern as dep_watch/pulse."""
    return _list_flag_enabled_repos(install_id, "guard_hygiene_watch_enabled")


_HYGIENE_WATCH_TTL_DAYS = 7


def claim_hygiene_watch_report(install_id: int, repo: str) -> bool:
    """Win-once weekly claim for a Guard hygiene quarantine report (#655) -
    same shape as claim_dep_watch_report. A separate `sk` namespace so the
    two weekly reports never contend for one claim."""
    pg_base.maybe_purge_expired()
    ttl = int(datetime.now(timezone.utc).timestamp() + _HYGIENE_WATCH_TTL_DAYS * 86400)
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
                "sk": f"HYGIENEWATCH#{repo}",
                "data": Jsonb({"ttl": ttl}),
                "ttl": ttl,
            },
        ).fetchone()
    return row is not None


def release_hygiene_watch_report(install_id: int, repo: str) -> None:
    """Release a hygiene-watch claim whose report WRITE definitively failed -
    the claim must represent a FILED report, not an attempt. Best-effort."""
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM grug_kv WHERE pk = %s AND sk = %s",
            (_inst_pk(install_id), f"HYGIENEWATCH#{repo}"),
        )


def claim_pulse_nudge(install_id: int, repo: str, pr_number: int) -> bool:
    """Win-once claim for a Pulse stuck-PR nudge (#472): True = nudge now;
    False = a nudge inside the TTL window already happened (or a
    concurrent poller run won). Same atomic upsert-if-expired shape as
    claim_delivery. Best-effort caller: any DB error propagates."""
    pg_base.maybe_purge_expired()
    ttl = int(datetime.now(timezone.utc).timestamp() + _PULSE_NUDGE_TTL_DAYS * 86400)
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


def list_comment_nudge_repos(install_id: int) -> list[dict[str, Any]]:
    """Repo rows with pulse_comment_nudge_enabled=true (#656): mirrors
    list_pulse_enabled_repos - store-driven targeting so an enabled repo can
    never be starved by a discovery-page prefix. Returns [{"id", "full_name"}]."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT sk, data FROM grug_kv
            WHERE pk = %s AND sk LIKE 'REPO#%%'
              AND data->>'pulse_comment_nudge_enabled' = 'true'
              AND (ttl IS NULL OR ttl > EXTRACT(EPOCH FROM now()))
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
    ttl = int(datetime.now(timezone.utc).timestamp() + _REVIEW_CLAIM_TTL_DAYS * 86400)
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
    ttl = int(datetime.now(timezone.utc).timestamp() + _REVIEW_CLAIM_TTL_DAYS * 86400)
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
                    install_id,
                    repo,
                    pr_number,
                    persona,
                    head_sha,
                ),
                "owner": owner_token,
                "data": Jsonb(
                    {
                        "review_claim_state": "completed",
                        "ttl": ttl,
                    }
                ),
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
                    install_id,
                    repo,
                    pr_number,
                    persona,
                    head_sha,
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
                    install_id,
                    repo,
                    pr_number,
                    persona,
                    head_sha,
                ),
                "owner": owner_token,
            },
        ).fetchone()
    return row is not None


def _decode_comment_record_rows(rows: list[tuple[Any, ...]]) -> list[CommentRecord]:
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
        if isinstance(item.get("ttl"), (int, float)):
            record["ttl"] = int(item["ttl"])
        if isinstance(item.get("resolution"), dict):
            record["resolution"] = item["resolution"]
        out.append(record)
    return out


def list_comment_records(install_id: int) -> list[CommentRecord]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT pk, sk, data FROM grug_kv
            WHERE pk = %s AND sk LIKE 'CRCOMMENT#%%' AND {TTL_LIVE}
            """,
            (_inst_pk(install_id),),
        ).fetchall()
    return _decode_comment_record_rows(rows)


def list_comment_records_for_pr(
    install_id: int,
    repo: str,
    pr_number: int,
) -> list[CommentRecord]:
    """CommentRecords for one PR (Sentinel's per-finding-resolution check,
    grug#743 audit) - same key shape as list_comment_records, filtered by
    the JSONB repo/pr_number fields rather than pulling the whole
    install's 30-day comment history on every PR close event."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT pk, sk, data FROM grug_kv
            WHERE pk = %s AND sk LIKE 'CRCOMMENT#%%'
              AND data->>'repo' = %s AND (data->>'pr_number')::int = %s AND {TTL_LIVE}
            """,
            (_inst_pk(install_id), repo, pr_number),
        ).fetchall()
    return _decode_comment_record_rows(rows)


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


def update_comment_record_resolution(
    *,
    install_id: int,
    comment_id: int,
    state: Optional[str],
    sha: str = "",
) -> None:
    """Record a finding's living-review state (#1187). `state=None` clears
    it (the finding is open again). Sparse merge, so the reaction fields on
    the same row are untouched."""
    resolution = {"state": state, "sha": sha} if state else None
    _merge_attrs(
        _inst_pk(install_id),
        _comment_record_sk(comment_id),
        {"resolution": resolution},
    )
