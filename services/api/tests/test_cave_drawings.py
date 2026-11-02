"""Pure-core tests for Cave Drawings (#1203): the deterministic path
matcher, scope validation, prompt selection (lifecycle + conflicts), and the
portable export/import plan. No store, no network."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cave_drawings as cd


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# --- deterministic path matching ------------------------------------------

def test_empty_scope_is_repo_wide():
    assert cd.path_matches("", "anything/at/all.py")


@pytest.mark.parametrize(
    ("scope", "path", "expected"),
    [
        ("**/middleware/*.py", "src/middleware/auth.py", True),
        # `**/` also matches at the repo root (zero directories).
        ("**/middleware/*.py", "middleware/auth.py", True),
        ("**/middleware/*.py", "src/handlers/auth.py", False),
        ("services/api/*.py", "services/api/main.py", True),
        ("services/api/*.py", "services/webhook/main.py", False),
        # Case-sensitive on every platform: the matcher must not depend on
        # the host OS (fnmatch.fnmatch lowercases on Windows).
        ("**/Middleware/*.py", "src/middleware/auth.py", False),
        ("*.md", "README.md", True),
    ],
)
def test_path_matches_is_deterministic(scope, path, expected):
    assert cd.path_matches(scope, path) is expected


def test_preview_paths_lists_exactly_the_matching_files_sorted_and_unique():
    paths = [
        "src/middleware/z.py", "src/middleware/a.py", "src/api/x.py",
        "src/middleware/a.py", "middleware/root.py", "docs/x.md",
    ]
    assert cd.preview_paths("**/middleware/*.py", paths) == [
        "middleware/root.py", "src/middleware/a.py", "src/middleware/z.py",
    ]
    assert cd.preview_paths("", ["b", "a"]) == ["a", "b"]


@pytest.mark.parametrize(
    "scope",
    [
        "/etc/*.py",          # absolute
        "../outside/*.py",    # escapes the repo
        "src/../../x",        # escapes the repo mid-path
        "src\\win\\*.py",     # backslash separators
        "src//double.py",     # empty segment
        "src/x.py\ninjected", # control characters
        "a" * 300,            # absurdly long
        "src/[unclosed",      # unbalanced bracket class
    ],
)
def test_validate_scope_path_rejects_invalid_paths(scope):
    assert cd.validate_scope_path(scope) is not None


@pytest.mark.parametrize("scope", ["", "**/*.py", "services/api/*.py", "docs/[ab]*.md"])
def test_validate_scope_path_accepts_valid_globs(scope):
    assert cd.validate_scope_path(scope) is None


# --- lifecycle + prompt selection -----------------------------------------

def _row(text, *, status=None, at=None, **kw):
    at = at or _ago(30)
    row = {"text": text, "repo": "o/r", "reinforced_at": at, "created_at": at, **kw}
    if status is not None:
        row["status"] = status
    row["id"] = cd.learning_id(text)
    return row


def test_rows_without_status_are_active_for_back_compat():
    assert cd.effective_status({"text": "x", "created_at": _ago(1)}) == cd.STATUS_ACTIVE


def test_approval_delay_keeps_a_mined_learning_pending_until_it_elapses():
    from datetime import datetime, timezone

    row = {"text": "x", "active": False, "approval_delay_seconds": 3600,
           "created_at": "2026-01-01T00:00:00+00:00"}
    early = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    late = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    assert cd.effective_status(row, now=early) == cd.STATUS_PENDING
    assert cd.effective_status(row, now=late) == cd.STATUS_ACTIVE
    # An operator's explicit status overrides the timer either way.
    assert cd.effective_status({**row, "status": "disabled"}, now=late) == cd.STATUS_DISABLED
    assert cd.effective_status({**row, "status": "active"}, now=early) == cd.STATUS_ACTIVE
    # Inactive with no delay never activates on its own.
    assert cd.effective_status({"text": "x", "active": False}) == cd.STATUS_PENDING


def test_plan_import_bounds_the_document_size():
    doc = cd.export_document([])
    doc["learnings"] = [{"text": f"r{i}", "repo": "o/r"} for i in range(cd.MAX_IMPORT_LEARNINGS + 1)]
    plan = cd.plan_import(doc, target_repo=None, existing_ids=set())
    assert plan.errors and plan.items == []


def test_only_active_learnings_are_selected_for_the_prompt():
    rows = [
        _row("active rule"),
        _row("pending rule", status=cd.STATUS_PENDING),
        _row("disabled rule", status=cd.STATUS_DISABLED),
        _row("old rule", status=cd.STATUS_SUPERSEDED),
    ]
    sel = cd.select_for_prompt(rows)
    assert [r["text"] for r in sel.selected] == ["active rule"]


def test_conflicting_active_learnings_are_never_both_injected():
    older = _row("allow bare except in scripts", at=_ago(30))
    newer = _row(
        "never allow bare except", at=_ago(20),
        conflicts_with=[older["id"]],
    )
    sel = cd.select_for_prompt([older, newer])
    assert [r["text"] for r in sel.selected] == ["never allow bare except"]
    # The suppression is reported, not silent.
    assert sel.suppressed == [
        {"id": older["id"], "reason": "conflict", "kept": newer["id"]},
    ]


def test_conflict_declared_on_the_older_row_still_keeps_the_newer():
    newer = _row("rule B", at=_ago(10))
    older = _row("rule A", at=_ago(30), conflicts_with=[newer["id"]])
    sel = cd.select_for_prompt([older, newer])
    assert [r["text"] for r in sel.selected] == ["rule B"]


def test_a_superseded_learning_left_active_is_not_injected_beside_its_successor():
    # Belt and braces: even if the old row's status was never flipped (a
    # partial write, a hand edit), the successor's `supersedes` link wins.
    old = _row("rule v1")
    new = _row("rule v2", at=_ago(20), supersedes=old["id"])
    sel = cd.select_for_prompt([old, new])
    assert [r["text"] for r in sel.selected] == ["rule v2"]
    assert sel.suppressed[0]["reason"] == "superseded"


# --- search -----------------------------------------------------------------

def test_matches_query_searches_text_scope_and_author_case_insensitively():
    row = _row("Prefer early returns", scope_path="**/mw/*.py", author="Dev1")
    assert cd.matches_query(row, "EARLY")
    assert cd.matches_query(row, "mw/")
    assert cd.matches_query(row, "dev1")
    assert not cd.matches_query(row, "nothing")
    assert cd.matches_query(row, "")


# --- export / import (round trip) ------------------------------------------

def test_export_then_plan_import_round_trips_into_an_empty_instance():
    rows = [
        _row("rule one", scope_path="src/*.py", source_pr=3, author="dev"),
        _row("rule two"),
    ]
    doc = cd.export_document(rows)
    assert doc["format"] == cd.EXPORT_FORMAT and doc["version"] == cd.EXPORT_VERSION
    # Instance-local counters never travel.
    assert all("usage_count" not in item for item in doc["learnings"])

    plan = cd.plan_import(doc, target_repo=None, existing_ids=set())
    assert plan.errors == []
    assert plan.repo == "o/r"
    assert [i["action"] for i in plan.items] == ["create", "create"]
    assert [i["learning"]["text"] for i in plan.items] == ["rule one", "rule two"]
    assert plan.items[0]["learning"]["scope_path"] == "src/*.py"
    assert plan.items[0]["learning"]["source_pr"] == 3


def test_plan_import_rejects_an_unknown_format_or_version():
    assert cd.plan_import({"format": "other", "version": 1, "learnings": []},
                          target_repo="o/r", existing_ids=set()).errors
    assert cd.plan_import({"format": cd.EXPORT_FORMAT, "version": 99, "learnings": []},
                          target_repo="o/r", existing_ids=set()).errors
    assert cd.plan_import("not a dict", target_repo="o/r", existing_ids=set()).errors


def test_plan_import_flags_an_ambiguous_repository():
    doc = cd.export_document([_row("a"), {**_row("b"), "repo": "o/other"}])
    plan = cd.plan_import(doc, target_repo=None, existing_ids=set())
    assert any("ambiguous" in e for e in plan.errors)
    # An explicit target resolves it.
    plan = cd.plan_import(doc, target_repo="o/new", existing_ids=set())
    assert plan.errors == [] and plan.repo == "o/new"


def test_plan_import_rejects_a_malformed_target_repo():
    doc = cd.export_document([_row("a")])
    plan = cd.plan_import(doc, target_repo="not-a-repo", existing_ids=set())
    assert plan.errors


def test_plan_import_marks_duplicates_against_the_instance_and_within_the_file():
    existing = _row("already here")
    doc = cd.export_document([_row("already here"), _row("fresh"), _row("fresh ")])
    plan = cd.plan_import(doc, target_repo=None, existing_ids={existing["id"]})
    assert [i["action"] for i in plan.items] == ["duplicate", "create", "duplicate"]


def test_plan_import_marks_invalid_paths_and_empty_text_invalid():
    doc = cd.export_document([_row("ok"), _row("bad scope", scope_path="../x")])
    doc["learnings"].append({"text": "   ", "repo": "o/r"})
    plan = cd.plan_import(doc, target_repo=None, existing_ids=set())
    actions = [i["action"] for i in plan.items]
    assert actions == ["create", "invalid", "invalid"]
    assert plan.ok is False  # an invalid row blocks the apply step


# --- governed learnings (#1571) ---------------------------------------------

_NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _taught(days_ago: int, **extra):
    stamp = (_NOW - timedelta(days=days_ago)).isoformat()
    return {"text": "prefer early returns", "created_at": stamp, "reinforced_at": stamp, **extra}


def test_a_rule_taught_89_days_ago_is_still_active():
    assert cd.effective_status(_taught(89), now=_NOW) == cd.STATUS_ACTIVE


def test_a_rule_past_90_days_expires_with_no_cleanup():
    assert cd.effective_status(_taught(91), now=_NOW) == cd.STATUS_EXPIRED


def test_expiry_outranks_a_stored_active_status():
    assert cd.effective_status(_taught(91, status="active"), now=_NOW) == cd.STATUS_EXPIRED


def test_a_reaffirmation_restarts_the_90_days():
    row = _taught(120, approved_at=(_NOW - timedelta(days=5)).isoformat())
    assert cd.effective_status(row, now=_NOW) == cd.STATUS_ACTIVE
    assert cd.expires_at(row) == (_NOW + timedelta(days=85)).isoformat()


def test_a_disabled_rule_stays_disabled_past_expiry():
    assert cd.effective_status(_taught(91, status="disabled"), now=_NOW) == cd.STATUS_DISABLED


def test_a_rule_with_no_usable_date_is_expired_not_immortal():
    assert cd.effective_status({"text": "x", "created_at": "junk"}, now=_NOW) == cd.STATUS_EXPIRED


@pytest.mark.parametrize(
    ("rule", "category"),
    [
        ("whatever", "security"),
        ("exposed-secret", "general"),
        ("sql-injection", "general"),
        ("iac-misconfig", "general"),
        ("weak-crypto", "general"),
        ("missing-authz-check", "general"),
    ],
)
def test_security_and_sast_classes_are_protected(rule, category):
    assert cd.is_protected_finding(rule, category)


def test_an_ordinary_class_is_not_protected():
    assert not cd.is_protected_finding("null-deref", "correctness")


@pytest.mark.parametrize(
    "text",
    [
        "unauthenticated route", "unauthorized access", "missing_auth_check",
        "stored_xss", "leaked token", "the API_KEY in tests", "md5 is fine here",
        "nosqli", "api.key", "storedXss", "missingAuthCheck", "apiKey",
    ],
)
def test_security_vocabulary_reaches_the_forms_people_write(text):
    assert cd.names_protected_class(text)


@pytest.mark.parametrize(
    "text",
    ["ask the commit author first", "the token budget of the model", "prefer early returns"],
)
def test_security_vocabulary_leaves_ordinary_rules_alone(text):
    assert not cd.names_protected_class(text)


def test_every_shipped_sast_rule_class_is_protected():
    """A new SAST class must not arrive unprotected."""
    root = Path(__file__).resolve().parents[2] / "webhook" / "sast_rules"
    classes = set()
    for path in root.glob("*.yml"):
        classes.update(re.findall(r"vuln_class:\s*([a-z0-9-]+)", path.read_text()))
    assert classes, "no SAST rules found: the guard would pass on nothing"
    assert classes - cd.SAST_CLASSES == set()


def test_a_learning_on_a_security_finding_never_reaches_a_prompt():
    row = _taught(1, finding_class="sql-injection", finding_category="general")
    row["created_at"] = row["reinforced_at"] = datetime.now(timezone.utc).isoformat()
    sel = cd.select_for_prompt([row])
    assert sel.selected == []
    assert sel.suppressed[0]["reason"] == "protected-class"


def test_a_learning_whose_text_names_a_security_class_never_reaches_a_prompt():
    row = _taught(1, text="Do not flag sql injection in the admin tools.")
    row["created_at"] = row["reinforced_at"] = datetime.now(timezone.utc).isoformat()
    sel = cd.select_for_prompt([row])
    assert sel.selected == [] and sel.suppressed[0]["reason"] == "protected-class"


def test_an_expired_learning_never_reaches_a_prompt():
    assert cd.select_for_prompt([_taught(400)]).selected == []


@pytest.mark.parametrize("reply", ["", "no", "wrong", "\U0001F44E", "not a bug"])
def test_a_bare_dismissal_is_not_a_reason(reply):
    assert not cd.is_reasoned_dismissal(reply)


def test_a_dismissal_that_explains_itself_is_a_reason():
    assert cd.is_reasoned_dismissal("This is fine because the caller validates the id.")


def test_export_carries_origin_reason_and_expiry():
    row = _taught(1, reason="the caller validates", finding_class="null-deref", author="dev")
    item = cd.export_document([row])["learnings"][0]
    assert item["reason"] == "the caller validates"
    assert item["finding_class"] == "null-deref"
    assert item["expires_at"] == cd.expires_at(row)
