"""Cave Drawings (#1203): the pure core of Grug's learnings control plane.

Learnings are the operator-taught rules mined from replies (ADR-0020). This
module holds every decision about them that needs no I/O, so each one is
unit-testable without a store or a network:

- identity: a learning's id is the 12-hex digest of its stripped text, the
  same key the store uses (`LEARNING#<id>`).
- lifecycle: `active` (injected), `pending` (waiting for an explicit
  approval, e.g. after an import), `disabled` (switched off by an operator),
  `superseded` (replaced by an edited version, kept for history). A row
  written before #1203 has no status and reads as `active`.
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
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Iterable

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_DISABLED = "disabled"
STATUS_SUPERSEDED = "superseded"
STATUSES = (STATUS_ACTIVE, STATUS_PENDING, STATUS_DISABLED, STATUS_SUPERSEDED)

EXPORT_FORMAT = "grug-cave-drawings"
EXPORT_VERSION = 1

# Fields that travel in an export. Usage counters and TTLs are local to one
# instance and are deliberately left behind.
_PORTABLE_FIELDS = (
    "text", "repo", "scope_path", "source_pr", "source_comment_id", "author",
    "created_at", "supersedes", "conflicts_with",
)

MAX_TEXT_CHARS = 1000
MAX_SCOPE_CHARS = 200
MAX_IMPORT_LEARNINGS = 2000
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def learning_id(text: str) -> str:
    """Stable 12-hex identity of a learning from its rule text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def effective_status(row: dict[str, Any], now: datetime | None = None) -> str:
    """The one lifecycle rule every reader uses.

    An operator's explicit `status` wins. Otherwise the reply-mined
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

    Only active rows. When two active rows are linked by `supersedes` the
    successor wins; when linked by `conflicts_with` (declared on either
    side) the more recently taught one wins. Every row dropped for a link is
    listed in `suppressed`, so the caller logs it instead of injecting two
    contradictory rules or dropping one silently."""
    active = [r for r in rows if effective_status(r) == STATUS_ACTIVE]
    by_id = {_row_id(r): r for r in active}
    drop = _supersede_drops(active, by_id)
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
