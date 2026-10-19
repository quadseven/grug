"""Cave Drawings admin API (#1203): list, search, inspect, edit, disable,
approve, delete, path preview, export and the dry-run-first import. Route
functions are called directly against the real Postgres test database, the
same way test_admin.py drives the admin router."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import cave_drawings as cd


@pytest.fixture()
def api(pg_store):
    import learnings

    return learnings


@pytest.fixture()
def store(pg_store):
    from adapters import pg_install_store

    return pg_install_store


def _admin():
    from adapters.user_store import UserIdentity

    return UserIdentity(
        github_user_id="100", login="op", role="admin", tier="lifetime",
        allowlisted=True, created_at="", allowlisted_at=None, allowlisted_by=None,
    )


def _teach(store, text, repo="o/r", **kw):
    store.put_learning(repo=repo, text=text, **kw)
    return cd.learning_id(text)


def test_list_shows_scope_matcher_provenance_status_and_usage(api, store):
    lid = _teach(store, "prefer early returns", scope_path="src/*.py",
                 source_pr=4, source_comment_id=55, author="dev")
    store.record_learning_usage("o/r", [lid], review_key="9@abc")
    [view] = api.list_learnings(repo="o/r", q="", status="", user=_admin())["learnings"]
    assert view["id"] == lid and view["status"] == cd.STATUS_ACTIVE
    assert view["scope"] == {"kind": "repo", "repo": "o/r", "org": "o"}
    assert view["matcher"] == {"glob": "src/*.py", "repo_wide": False, "engine": "fnmatchcase"}
    assert view["source"]["pr"] == 4 and view["source"]["comment_id"] == 55
    assert view["source"]["author"] == "dev"
    assert view["source"]["thread_url"] == "https://github.com/o/r/pull/4#discussion_r55"
    assert view["usage_count"] == 1 and view["last_used_at"]
    assert view["created_at"] and view["updated_at"]


def test_list_across_repos_with_search_and_status_filter(api, store):
    _teach(store, "prefer early returns", repo="o/a")
    b = _teach(store, "name the caller", repo="o/b")
    store.set_learning_status("o/b", b, cd.STATUS_DISABLED, actor="op")
    allrows = api.list_learnings(repo="", q="", status="", user=_admin())["learnings"]
    assert {v["repo"] for v in allrows} == {"o/a", "o/b"}
    hits = api.list_learnings(repo="", q="CALLER", status="", user=_admin())["learnings"]
    assert [v["text"] for v in hits] == ["name the caller"]
    off = api.list_learnings(repo="", q="", status="disabled", user=_admin())["learnings"]
    assert [v["id"] for v in off] == [b]


def test_list_rejects_a_malformed_repo_or_status(api):
    with pytest.raises(HTTPException) as e:
        api.list_learnings(repo="no-slash", q="", status="", user=_admin())
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        api.list_learnings(repo="", q="", status="bogus", user=_admin())
    assert e.value.status_code == 400


def test_inspect_edit_disable_approve_delete(api, store):
    lid = _teach(store, "rule v1")
    assert api.get_learning("o", "r", lid, user=_admin())["text"] == "rule v1"
    with pytest.raises(HTTPException) as e:
        api.get_learning("o", "r", "000000000000", user=_admin())
    assert e.value.status_code == 404

    edited = api.patch_learning("o", "r", lid, api.LearningPatch(text="rule v2"), user=_admin())
    new_id = edited["id"]
    assert edited["supersedes"] == lid and edited["version"] == 2
    old = api.get_learning("o", "r", lid, user=_admin())
    assert old["status"] == cd.STATUS_SUPERSEDED and old["superseded_by"] == new_id

    with pytest.raises(HTTPException) as e:
        api.patch_learning("o", "r", new_id, api.LearningPatch(scope_path="/abs"), user=_admin())
    assert e.value.status_code == 400

    assert api.disable_learning("o", "r", new_id, user=_admin())["status"] == "disabled"
    assert api.approve_learning("o", "r", new_id, user=_admin())["status"] == "active"
    with pytest.raises(HTTPException) as e:
        api.approve_learning("o", "r", lid, user=_admin())  # superseded
    assert e.value.status_code == 409

    assert api.delete_learning("o", "r", new_id, user=_admin()) == {"deleted": True, "id": new_id}
    with pytest.raises(HTTPException) as e:
        api.delete_learning("o", "r", new_id, user=_admin())
    assert e.value.status_code == 404


def test_preview_lists_exactly_the_matching_files(api):
    body = api.PreviewRequest(
        scope_path="**/mw/*.py", paths=["src/mw/a.py", "mw/b.py", "src/api/c.py"],
    )
    out = api.preview_learning_paths(body, user=_admin())
    assert out == {"scope_path": "**/mw/*.py", "repo_wide": False,
                   "matches": ["mw/b.py", "src/mw/a.py"], "match_count": 2,
                   "total_files": 3}


def test_preview_fetches_the_repo_tree_when_no_paths_are_given(api, monkeypatch):
    calls = []

    def fake_tree(install_id, repo, ref):
        calls.append((install_id, repo, ref))
        return ["a/mw/x.py", "b.py"]

    monkeypatch.setattr(api, "_fetch_repo_paths", fake_tree)
    body = api.PreviewRequest(scope_path="**/mw/*.py", repo="o/r", install_id=5)
    out = api.preview_learning_paths(body, user=_admin())
    assert out["matches"] == ["a/mw/x.py"] and calls == [(5, "o/r", "HEAD")]


def test_preview_rejects_invalid_paths_and_missing_sources(api):
    with pytest.raises(HTTPException) as e:
        api.preview_learning_paths(api.PreviewRequest(scope_path="../x", paths=[]), user=_admin())
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        api.preview_learning_paths(api.PreviewRequest(scope_path="*.py"), user=_admin())
    assert e.value.status_code == 400


def test_export_import_round_trip_is_dry_run_first_then_pending(api, store):
    _teach(store, "rule one", repo="o/src", scope_path="src/*.py", source_pr=3)
    _teach(store, "rule two", repo="o/src")
    doc = api.export_learnings(repo="o/src", user=_admin())
    assert doc["format"] == cd.EXPORT_FORMAT and len(doc["learnings"]) == 2

    # Dry run (the default) writes nothing.
    req = api.ImportRequest(document=doc, target_repo="o/dst")
    plan = api.import_learnings(req, user=_admin())
    assert plan["ok"] and plan["applied"] is False
    assert store.list_learnings("o/dst") == []

    applied = api.import_learnings(
        api.ImportRequest(document=doc, target_repo="o/dst", dry_run=False), user=_admin(),
    )
    assert applied["applied"] is True and len(applied["created"]) == 2
    rows = store.list_learnings("o/dst")
    assert {r["status"] for r in rows} == {cd.STATUS_PENDING}
    assert {r["text"] for r in rows} == {"rule one", "rule two"}

    # Re-importing is a no-op: every row is now a duplicate.
    again = api.import_learnings(
        api.ImportRequest(document=doc, target_repo="o/dst", dry_run=False), user=_admin(),
    )
    assert again["created"] == []
    assert [i["action"] for i in again["items"]] == ["duplicate", "duplicate"]


def test_import_refuses_to_apply_an_ambiguous_or_invalid_plan(api, store):
    doc = cd.export_document([
        {"text": "a", "repo": "o/one"}, {"text": "b", "repo": "o/two"},
    ])
    with pytest.raises(HTTPException) as e:
        api.import_learnings(api.ImportRequest(document=doc, dry_run=False), user=_admin())
    assert e.value.status_code == 422
    assert "ambiguous" in str(e.value.detail)

    bad = cd.export_document([{"text": "ok", "repo": "o/r"}])
    bad["learnings"].append({"text": "x", "repo": "o/r", "scope_path": "../up"})
    with pytest.raises(HTTPException) as e:
        api.import_learnings(api.ImportRequest(document=bad, dry_run=False), user=_admin())
    assert e.value.status_code == 422
    assert store.list_learnings("o/r") == []  # nothing applied


def test_import_conflict_after_dry_run_rolls_back_and_reports_409(api, store, monkeypatch):
    doc = cd.export_document([{"text": "fresh", "repo": "o/r"}, {"text": "late", "repo": "o/r"}])

    real_plan = cd.plan_import

    calls = []

    def plan_then_race(*a, **kw):
        plan = real_plan(*a, **kw)
        calls.append(1)
        if len(calls) == 2:  # the final plan, after the duplicate check
            _teach(store, "late")  # lands between the plan and the apply
        return plan

    monkeypatch.setattr(api, "plan_import", plan_then_race)
    with pytest.raises(HTTPException) as e:
        api.import_learnings(api.ImportRequest(document=doc, dry_run=False), user=_admin())
    assert e.value.status_code == 409
    assert [r["text"] for r in store.list_learnings("o/r")] == ["late"]


def test_every_route_is_admin_only(api):
    from auth.dependencies import require_admin

    assert api.router.routes
    for route in api.router.routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert require_admin in deps, route.path


def test_router_is_mounted_on_the_app():
    from fastapi.testclient import TestClient

    import main

    # No session cookie: the admin gate answers (401/403), not a 404.
    resp = TestClient(main.app).get("/api/v1/admin/learnings")
    assert resp.status_code in (401, 403)
