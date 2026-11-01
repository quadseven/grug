"""Cave Drawings (#1203): the pure core of Grug's learnings control plane.

Learnings are the operator-taught rules mined from replies (ADR-0020). This
module holds every decision about them that needs no I/O, so each one is
unit-testable without a store or a network:

- identity: a learning's id is the 12-hex digest of its stripped text, the
  same key the store uses (`LEARNING#<id>`).
- lifecycle: `active` (injected), `pending` (waiting for an explicit
  approval, e.g. after an import), `disabled` (switched off by an operator),
  `superseded` (replaced by an edited version, kept for history),
  `expired` (not re-affirmed within 90 days; never stored, derived at read
  time, #1571). A row written before #1203 has no status and reads as
  `active` until it expires.
- governance (#1571): a learned rule can silence Elder, so it never touches
  a security or SAST class, needs a human's stated reason, and lapses unless
  re-affirmed. See ADR-0026.
- deterministic path matching: one matcher, case-sensitive on every host,
  shared by the path preview and the classifier's scope check.
- prompt selection: only active rows, and never both sides of a declared
  conflict or a supersedes link. A suppression is reported, never silent.
- portable export and an import PLAN (dry run): validation, deduplication
  and repository resolution happen here; the store applies a clean plan in
  one transaction, and imported rows land `pending` until approved.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any, Iterable

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_DISABLED = "disabled"
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"
STATUSES = (
    STATUS_ACTIVE, STATUS_PENDING, STATUS_DISABLED, STATUS_SUPERSEDED, STATUS_EXPIRED,
)

# A rule stops firing this long after its last human affirmation: taught,
# re-taught, approved or edited. Shorter than the row TTL on purpose, so an
# expired rule stays visible and can be re-affirmed before it is purged.
EXPIRY_DAYS = 90

# Every class the SAST lane emits. `is_protected_finding` also matches the
# vocabulary below, so an LLM's own rule name for the same thing is covered;
# test_every_shipped_sast_rule_class_is_protected pins this list to the rules.
SAST_CLASSES = frozenset({
    "cleartext-secret-log", "command-injection", "exposed-secret",
    "hardcoded-credential", "iac-misconfig", "path-traversal", "sql-injection",
    "ssrf", "template-injection", "unsafe-deserialization", "weak-crypto", "xxe",
})

# Deliberately broad. A false match costs one preference; a miss costs a
# silenced security finding.
_SECURITY_TERMS = re.compile(
    r"secur|vulnerab|exploit|semgrep|opengrep|injection|traversal|deserializ"
    r"|privilege|permission|secret|credential|password|crypto|encrypt|sanitiz"
    r"|auth(?!or(?!i[sz]))"  # auth, authz, unauthorized; not author
    r"|sqli(?![a-z])"  # nosqli too
    r"|(?<![a-z])(cve|sast|csrf|xss|ssrf|xxe|md5)(?![a-z])"
    r"|api[\W_]?key|(access|bearer|session|jwt|api|leaked)[\W_]?tokens?",
    re.IGNORECASE,
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")

_MIN_REASON_WORDS = 4

EXPORT_FORMAT = "grug-cave-drawings"
EXPORT_VERSION = 1

# Fields that travel in an export. Usage counters and TTLs are local to one
# instance and are deliberately left behind.
_PORTABLE_FIELDS = (
    "text", "repo", "scope_path", "source_pr", "source_comment_id", "author",
    "created_at", "supersedes", "conflicts_with",
    "reason", "finding_class", "finding_category",
)

MAX_TEXT_CHARS = 1000
MAX_SCOPE_CHARS = 200
MAX_IMPORT_LEARNINGS = 2000
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def learning_id(text: str) -> str:
    """Stable 12-hex identity of a learning from its rule text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def effective_status(row: dict[str, Any], now: datetime | None = None) -> str:
    """The one lifecycle rule every reader uses: a rule that would be active
    but is past its expiry is expired, so it stops firing with no cleanup."""
    status = _lifecycle_status(row, now)
    if status == STATUS_ACTIVE and _past_expiry(row, now):
        return STATUS_EXPIRED
    return status


def _lifecycle_status(row: dict[str, Any], now: datetime | None) -> str:
    """An operator's explicit `status` wins. Otherwise the reply-mined
    approval delay decides: a row taught with `active: false` is pending
    until `created_at + approval_delay_seconds` has passed. A row with
    neither field predates both features, when every stored learning was
    applied, so it stays active."""
    status = row.get("status")
    if status in STATUSES:
        return str(status)
    if row.get("active", True):
        return STATUS_ACTIVE
    return STATUS_ACTIVE if _delay_elapsed(row, now) else STATUS_PENDING


def _delay_elapsed(row: dict[str, Any], now: datetime | None) -> bool:
    delay = int(row.get("approval_delay_seconds") or 0)
    if delay <= 0:
        return False
    try:
        started = datetime.fromisoformat(str(row.get("created_at") or ""))
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return started.timestamp() + delay <= current.timestamp()


def _last_affirmed(row: dict[str, Any]) -> datetime | None:
    """When a human last stood behind this rule, or None if no date parses."""
    stamps: list[datetime] = []
    for key in ("created_at", "reinforced_at", "approved_at"):
        try:
            stamp = datetime.fromisoformat(str(row.get(key) or ""))
        except ValueError:
            continue
        stamps.append(stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc))
    return max(stamps) if stamps else None


def expires_at(row: dict[str, Any]) -> str:
    """The ISO instant this rule stops firing, or "" when it has no usable
    date, which reads as already expired."""
    last = _last_affirmed(row)
    return (last + timedelta(days=EXPIRY_DAYS)).isoformat() if last else ""


def _past_expiry(row: dict[str, Any], now: datetime | None) -> bool:
    last = _last_affirmed(row)
    if last is None:
        return True
    return (now or datetime.now(timezone.utc)) >= last + timedelta(days=EXPIRY_DAYS)


def _mentions_security(text: str) -> bool:
    """Match on a camelCase-split copy, so `storedXss` reads as two words."""
    return bool(_SECURITY_TERMS.search(_CAMEL_BOUNDARY.sub(" ", text or "")))


def is_protected_finding(rule_name: str, category: str | None = "") -> bool:
    """Whether a finding class is out of reach of every learned rule."""
    return (
        category == "security"
        or rule_name in SAST_CLASSES
        or _mentions_security(rule_name)
    )


def names_protected_class(text: str) -> bool:
    """Whether a rule's own words reach into security or SAST territory,
    whatever finding it was taught on."""
    return _mentions_security(text)


def touches_protected(row: dict[str, Any]) -> bool:
    return is_protected_finding(
        str(row.get("finding_class") or ""), str(row.get("finding_category") or ""),
    ) or names_protected_class(str(row.get("text") or ""))


def fired_review_key(pr: int | str, head_sha: str) -> str:
    """The one identity of a review in the usage counters and the fired audit."""
    return f"{pr}@{head_sha}"


def is_reasoned_dismissal(reply: str) -> bool:
    """A reaction, an emoji or a bare "not a bug" states no reason."""
    return len(re.findall(r"[^\W\d_]{2,}", reply or "")) >= _MIN_REASON_WORDS


def is_app_actor(login: str, actor_type: str = "") -> bool:
    """An App acts under `name[bot]` with `type: Bot`. Either mark disqualifies:
    an App can hold write access, so permission cannot tell it from a person."""
    return actor_type == "Bot" or (login or "").lower().endswith("[bot]")


def is_valid_repo(repo: str) -> bool:
    return bool(_REPO_RE.match(repo or ""))


# --- deterministic path matching ------------------------------------------

def validate_scope_path(scope: str) -> str | None:
    """None when `scope` is a usable repo-relative glob ("" = repo-wide),
    else a short reason. Rejects anything that could not name a file in the
    repository, so a preview never silently matches nothing for a typo."""
    if scope == "":
        return None
    if len(scope) > MAX_SCOPE_CHARS:
        return f"scope longer than {MAX_SCOPE_CHARS} characters"
    if any(ord(c) < 32 or ord(c) == 127 for c in scope):
        return "scope contains control characters"
    if "\\" in scope:
        return "scope must use / separators"
    if scope.startswith("/"):
        return "scope must be relative to the repository root"
    segments = scope.split("/")
    if any(seg == ".." for seg in segments):
        return "scope must not leave the repository"
    if any(seg == "" for seg in segments):
        return "scope has an empty path segment"
    if scope.count("[") != scope.count("]"):
        return "scope has an unbalanced [ ] class"
    return None


def path_matches(scope: str, path: str) -> bool:
    """Whether a learning scoped to `scope` applies to `path`.

    Deterministic: `fnmatchcase` (never `fnmatch`, which lowercases on some
    hosts), `*` crosses directories, and a `**/` prefix also matches zero
    directories so `**/mw/*.py` covers `mw/a.py` at the root."""
    if not scope:
        return True
    if fnmatchcase(path, scope):
        return True
    return "**/" in scope and fnmatchcase(path, scope.replace("**/", ""))


def preview_paths(scope: str, paths: Iterable[str]) -> list[str]:
    """Exactly the files a learning with `scope` applies to, sorted."""
    return sorted({p for p in paths if path_matches(scope, p)})


# --- search -----------------------------------------------------------------

def matches_query(row: dict[str, Any], query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    haystack = " ".join(
        str(row.get(k, "")) for k in ("text", "scope_path", "author", "repo")
    ).lower()
    return q in haystack


# --- prompt selection -------------------------------------------------------

@dataclass
class Selection:
    selected: list[dict[str, Any]]
    suppressed: list[dict[str, str]] = field(default_factory=list)


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or learning_id(str(row.get("text", ""))))


def _recency(row: dict[str, Any]) -> str:
    return str(row.get("reinforced_at") or row.get("created_at") or "")


def select_for_prompt(rows: list[dict[str, Any]]) -> Selection:
    """The learnings a review may be given, in the input order.

    Only active rows, and never one that touches a security or SAST class:
    this check runs at read time, so a row an operator edited, imported or
    approved cannot get past it. When two active rows are linked by
    `supersedes` the successor wins; when linked by `conflicts_with`
    (declared on either side) the more recently taught one wins. Every row
    dropped here is listed in `suppressed`, so the caller logs it instead of
    injecting two contradictory rules or dropping one silently."""
    active = [r for r in rows if effective_status(r) == STATUS_ACTIVE]
    by_id = {_row_id(r): r for r in active}
    drop = {
        _row_id(r): {"id": _row_id(r), "reason": "protected-class", "kept": ""}
        for r in active if touches_protected(r)
    }
    for lose_id, entry in _supersede_drops(active, by_id).items():
        drop.setdefault(lose_id, entry)
    for lose_id, entry in _conflict_drops(active, by_id).items():
        drop.setdefault(lose_id, entry)
    selected = [r for r in active if _row_id(r) not in drop]
    suppressed = [drop[_row_id(r)] for r in active if _row_id(r) in drop]
    return Selection(selected=selected, suppressed=suppressed)


def _supersede_drops(
    active: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Active predecessors of an active successor: the successor wins."""
    drop: dict[str, dict[str, str]] = {}
    for row in active:
        rid = _row_id(row)
        old = str(row.get("supersedes") or "")
        if old in by_id and old != rid:
            drop.setdefault(old, {"id": old, "reason": "superseded", "kept": rid})
    return drop


def _conflict_drops(
    active: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """For each declared conflict between two active rows, the older one.
    Ties break on id so the choice is deterministic."""
    drop: dict[str, dict[str, str]] = {}
    for row in active:
        rid = _row_id(row)
        for other in (str(o) for o in row.get("conflicts_with") or []):
            if other == rid or other not in by_id:
                continue
            pair = sorted((row, by_id[other]), key=lambda r: (_recency(r), _row_id(r)))
            lose_id, keep_id = _row_id(pair[0]), _row_id(pair[1])
            drop.setdefault(lose_id, {"id": lose_id, "reason": "conflict", "kept": keep_id})
    return drop


# --- export / import --------------------------------------------------------

def export_document(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A portable, versioned document of learnings Grug can re-import."""
    items: list[dict[str, Any]] = []
    for row in rows:
        item = {k: row[k] for k in _PORTABLE_FIELDS if row.get(k) not in (None, "", [])}
        item["status"] = effective_status(row)
        item["expires_at"] = expires_at(row)
        items.append(item)
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "learnings": items,
    }


@dataclass
class ImportPlan:
    """The dry-run result. `repo` is the resolved target repository;
    `items` has one entry per learning in the document, in order, with an
    `action` of create, duplicate or invalid."""
    repo: str = ""
    errors: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(i["action"] != "invalid" for i in self.items)

    @property
    def to_create(self) -> list[dict[str, Any]]:
        return [i["learning"] for i in self.items if i["action"] == "create"]

    def as_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "ok": self.ok, "errors": self.errors, "items": self.items}


def _resolve_repo(learnings: list[Any], target_repo: str | None) -> tuple[str, str | None]:
    if target_repo:
        if not is_valid_repo(target_repo):
            return "", f"target repository {target_repo!r} is not owner/name"
        return target_repo, None
    sources = sorted({
        str(x.get("repo", "")) for x in learnings if isinstance(x, dict) and x.get("repo")
    })
    if len(sources) > 1:
        return "", (
            "ambiguous repository: the document holds learnings for "
            f"{len(sources)} repositories; name a target repository"
        )
    if not sources or not is_valid_repo(sources[0]):
        return "", "no repository: name a target repository"
    return sources[0], None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def plan_import(
    doc: Any, *, target_repo: str | None, existing_ids: set[str],
) -> ImportPlan:
    """Validate and deduplicate an export document without writing."""
    plan = ImportPlan()
    learnings, err = _document_learnings(doc)
    if not err:
        plan.repo, err = _resolve_repo(learnings, target_repo)
    if err:
        plan.errors.append(err)
        return plan
    seen: set[str] = set()
    plan.items = [
        _plan_item(index, raw, plan.repo, existing_ids, seen)
        for index, raw in enumerate(learnings)
    ]
    return plan


def _document_learnings(doc: Any) -> tuple[list[Any], str | None]:
    """The document's learnings list, or the reason it is not importable."""
    if not isinstance(doc, dict) or doc.get("format") != EXPORT_FORMAT:
        return [], f"not a {EXPORT_FORMAT} document"
    if doc.get("version") != EXPORT_VERSION:
        return [], f"unsupported version {doc.get('version')!r}; expected {EXPORT_VERSION}"
    learnings = doc.get("learnings")
    if not isinstance(learnings, list):
        return [], "learnings must be a list"
    if len(learnings) > MAX_IMPORT_LEARNINGS:
        return [], f"more than {MAX_IMPORT_LEARNINGS} learnings in one document"
    return learnings, None


def _invalid_reason(raw: Any) -> str | None:
    """Why one exported learning cannot be imported, or None."""
    if not isinstance(raw, dict):
        return "not an object"
    text = str(raw.get("text", "")).strip()
    if not text:
        return "empty text"
    if len(text) > MAX_TEXT_CHARS:
        return f"text longer than {MAX_TEXT_CHARS} characters"
    return validate_scope_path(str(raw.get("scope_path", "") or ""))


def _portable_learning(raw: dict[str, Any], repo: str) -> dict[str, Any]:
    """The fields of one valid exported learning that an import writes."""
    conflicts = raw.get("conflicts_with")
    return {
        "text": str(raw.get("text", "")).strip(),
        "repo": repo,
        "scope_path": str(raw.get("scope_path", "") or ""),
        "source_pr": _int_or_zero(raw.get("source_pr")),
        "source_comment_id": _int_or_zero(raw.get("source_comment_id")),
        "author": str(raw.get("author", "") or ""),
        "created_at": str(raw.get("created_at", "") or ""),
        "supersedes": str(raw.get("supersedes", "") or ""),
        "reason": str(raw.get("reason", "") or ""),
        "finding_class": str(raw.get("finding_class", "") or ""),
        "finding_category": str(raw.get("finding_category", "") or ""),
        "conflicts_with": [c for c in conflicts if isinstance(c, str)]
        if isinstance(conflicts, list) else [],
    }


def _plan_item(
    index: int, raw: Any, repo: str, existing_ids: set[str], seen: set[str],
) -> dict[str, Any]:
    """One row of the plan: create, duplicate or invalid. Adds a created id
    to `seen`, so a later repeat in the same document is a duplicate."""
    reason = _invalid_reason(raw)
    if reason:
        text = str(raw.get("text", "")).strip()[:120] if isinstance(raw, dict) else ""
        return {"index": index, "action": "invalid", "reason": reason, "text": text}
    learning = _portable_learning(raw, repo)
    lid = learning_id(learning["text"])
    entry: dict[str, Any] = {"index": index, "id": lid}
    if lid in existing_ids:
        return {**entry, "action": "duplicate", "reason": "already on this instance"}
    if lid in seen:
        return {**entry, "action": "duplicate", "reason": "repeated in the document"}
    seen.add(lid)
    return {**entry, "action": "create", "learning": learning}
