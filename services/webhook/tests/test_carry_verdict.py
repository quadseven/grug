"""Elder carries a passed verdict to a head whose own diff is unchanged (#1648).

update-branch and a stack's cascade rebase make new heads with the same PR
diff. Before this, each one waited for a full model review of code Elder had
already passed.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from personas.code_reviewer import carry_verdict as cv
from personas.code_reviewer import dispatch as cr_dispatch

_DIFF = """diff --git a/src/x.py b/src/x.py
index 1111111..2222222 100644
--- a/src/x.py
+++ b/src/x.py
@@ -10,3 +10,4 @@ def f():
 context
-old
+new1
+new2
"""
# The same change after main gained lines above it: new blob ids and offsets.
_DIFF_SHIFTED = _DIFF.replace("1111111..2222222", "3333333..4444444").replace(
    "@@ -10,3 +10,4 @@", "@@ -42,3 +42,4 @@",
)
_DIFF_ONE_LINE_MORE = _DIFF + "+new3\n"

_APP_ID = "4242"
_PRIOR = "prior000000000aaaa"
_HEAD = "head0000000000bbbb"


class _ReviewRan(Exception):
    """The normal review path started: its diff fetch was reached."""


def _elder_run(conclusion="success", status="completed", app_id=_APP_ID):
    return {
        "id": 7, "name": "Grug - Elder", "status": status,
        "conclusion": conclusion, "app": {"id": int(app_id)},
    }


def _payload(*, action="review", base_sha="mainbase", base_ref="main"):
    return {
        "action": action,
        "installation": {"id": 11},
        "repository": {
            "id": 22, "name": "r", "owner": {"login": "o"},
            "default_branch": "main",
        },
        "pull_request": {
            "number": 5,
            "head": {"sha": _HEAD},
            "base": {"sha": base_sha, "ref": base_ref},
            "title": "t", "body": "b", "user": {"login": "dev"},
        },
    }


@pytest.fixture
def world(monkeypatch):
    """Every seam the carry decision and the start of a review touch."""
    import adapters.install_store as store

    state = {
        "record": None, "runs": [_elder_run()], "pr_diff": _DIFF,
        "threads": [], "posted": [], "stored": [], "diff_bases": [],
        "model_calls": 0,
    }

    def _full_diff(install_id, owner, repo, number, *, base_sha, head_sha):
        state["diff_bases"].append((base_sha, head_sha))
        return state["pr_diff"]

    def _review_fetch(*a, **k):
        raise _ReviewRan()

    def _model(*a, **k):
        state["model_calls"] += 1
        raise AssertionError("a carried verdict must not call the model")

    monkeypatch.setattr(store, "get_elder_carry_record", lambda **k: state["record"])
    monkeypatch.setattr(store, "get_elder_last_reviewed", lambda **k: None)
    monkeypatch.setattr(store, "put_elder_last_reviewed", lambda **k: state["stored"].append(k))
    monkeypatch.setattr(cr_dispatch, "steering_base_ref", lambda *a, **k: "mainhead")
    monkeypatch.setattr(cr_dispatch, "_load_pr_repo_config", lambda *a, **k: None)
    monkeypatch.setattr(cr_dispatch, "pr_skip_rule", lambda *a, **k: None)
    monkeypatch.setattr(cr_dispatch, "_review_snapshot_freshness_failure", lambda **k: None)
    monkeypatch.setattr(cr_dispatch, "_fetch_full_pr_diff", _full_diff)
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda i, fn: fn("tok"))
    monkeypatch.setattr(cr_dispatch, "fetch_pr_diff", _review_fetch)
    monkeypatch.setattr(cr_dispatch, "fetch_pr_diff_with_scope", _review_fetch)
    monkeypatch.setattr(cr_dispatch, "review_diff", _model)
    monkeypatch.setattr(cv, "get_app_id", lambda: _APP_ID)
    monkeypatch.setattr(cv, "with_install_token_retry", lambda i, fn: fn("tok"))
    monkeypatch.setattr(cv, "list_check_runs_for_ref", lambda *a, **k: state["runs"])
    monkeypatch.setattr(cv, "post_check_run", lambda *a, **k: state["posted"].append((a, k)) or {"id": 9})
    monkeypatch.setattr(cv, "record_check_verdict", lambda **k: None)
    monkeypatch.setattr(cv.carry_forward, "list_threads", lambda *a, **k: state["threads"])
    return state


def _record(diff=_DIFF, *, title="t", body="b", mode="blocking", head=_PRIOR):
    return {
        "head_sha": head, "patch_id": cv.patch_id(diff),
        "context_id": cv.context_id(mode=mode, title=title, body=body),
    }


# --- patch-id -------------------------------------------------------------

def test_patch_id_ignores_blob_ids_and_line_offsets():
    assert cv.patch_id(_DIFF) == cv.patch_id(_DIFF_SHIFTED)


def test_patch_id_sees_a_one_line_change():
    assert cv.patch_id(_DIFF) != cv.patch_id(_DIFF_ONE_LINE_MORE)


def test_patch_id_of_no_change_or_a_binary_file_is_empty():
    assert cv.patch_id("") == ""
    binary = "diff --git a/i.png b/i.png\nindex 1..2 100644\nBinary files a/i.png and b/i.png differ\n"
    assert cv.patch_id(binary) == ""


def test_only_a_passed_head_stores_a_patch_id():
    def boom():
        raise RuntimeError("diff unreadable")

    assert cv.passed_diff_id("failure", full_diff=_DIFF, fetch_pr_diff=boom) == ""
    assert cv.passed_diff_id("success", full_diff=_DIFF, fetch_pr_diff=boom) == cv.patch_id(_DIFF)
    assert cv.passed_diff_id("success", full_diff=None, fetch_pr_diff=lambda: _DIFF) == cv.patch_id(_DIFF)
    assert cv.passed_diff_id("success", full_diff=None, fetch_pr_diff=boom) == ""


def test_store_writes_patch_and_context_ids_beside_the_head(monkeypatch):
    import adapters.pg_install_store as store

    executed: list[dict] = []

    class _Conn:
        def execute(self, sql, params):
            executed.append(params)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(store, "get_pool", lambda: type("P", (), {"connection": lambda s: _Conn()})())
    store.put_elder_last_reviewed(
        install_id=9, repo="o/r", pr_number=7, head_sha="h", patch_id="p", context_id="c",
    )
    data = executed[0]["data"].obj
    assert (data["head_sha"], data["patch_id"], data["context_id"]) == ("h", "p", "c")
    assert datetime.fromisoformat(data["reviewed_at"]) <= datetime.now(timezone.utc)


# --- dispatch -------------------------------------------------------------

@pytest.mark.parametrize("action", ["review", "synchronize"])
def test_same_patch_id_carries_success_with_no_model_call(world, action):
    world["record"] = _record()
    world["pr_diff"] = _DIFF_SHIFTED

    out = cr_dispatch.dispatch_code_review(_payload(action=action), blocking=True)

    assert out == {"persona": "code_reviewer", "result": "pass", "carried_from": _PRIOR}
    assert world["model_calls"] == 0
    (args, kwargs), = world["posted"]
    check = args[3]
    assert (check.name, check.head_sha, check.conclusion) == ("Grug - Elder", _HEAD, "success")
    assert f"carried from {_PRIOR[:12]}: diff unchanged" in check.title
    assert check.summary.startswith(f"carried from {_PRIOR[:12]}: diff unchanged")
    assert kwargs["external_id"] == f"grug-cr:o/r#5:{_HEAD}"
    # The baseline moves to the new head with the same patch-id.
    assert world["stored"][0]["head_sha"] == _HEAD
    assert world["stored"][0]["patch_id"] == cv.patch_id(_DIFF)


def test_a_one_line_change_runs_the_full_review(world):
    world["record"] = _record()
    world["pr_diff"] = _DIFF_ONE_LINE_MORE

    with pytest.raises(_ReviewRan):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)
    assert world["posted"] == []


def test_a_stacked_pr_is_compared_against_its_own_base(world):
    world["record"] = _record()

    out = cr_dispatch.dispatch_code_review(
        _payload(base_sha="featbase", base_ref="feature/lower"), blocking=True,
    )

    assert out["result"] == "pass"
    # The PR's own base commit, not the default branch head steering reads.
    assert world["diff_bases"] == [("featbase", _HEAD)]


@pytest.mark.parametrize("runs", [
    [_elder_run("failure")],
    [_elder_run("neutral")],
    [_elder_run(status="in_progress", conclusion=None)],
    [_elder_run(app_id="999")],
    [],
])
def test_a_prior_head_without_elder_success_runs_the_full_review(world, runs):
    world["record"] = _record()
    world["runs"] = runs

    with pytest.raises(_ReviewRan):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)
    assert world["posted"] == []


@pytest.mark.parametrize("change", [
    {"record": None},
    {"record": {"head_sha": _PRIOR, "patch_id": "", "context_id": ""}},
    {"record": _record(head=_HEAD)},
    {"record": _record(body="other intent")},
    {"record": _record(mode="advisory")},
    {"threads": None},
])
def test_anything_unproven_runs_the_full_review(world, change):
    world["record"] = _record()
    world.update(change)

    with pytest.raises(_ReviewRan):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)
    assert world["posted"] == []


def test_an_unreadable_diff_runs_the_full_review(world, monkeypatch):
    world["record"] = _record()

    def _unreadable(*a, **k):
        raise RuntimeError("406")

    monkeypatch.setattr(cr_dispatch, "_fetch_full_pr_diff", _unreadable)
    with pytest.raises(_ReviewRan):
        cr_dispatch.dispatch_code_review(_payload(), blocking=True)


def test_rerun_always_reviews(world):
    world["record"] = _record()

    with pytest.raises(_ReviewRan):
        cr_dispatch.dispatch_code_review(_payload(action="rerun"), blocking=True)


def test_carry_forward_still_folds_open_blocking_threads(world, monkeypatch):
    world["record"] = _record()
    monkeypatch.setattr(
        cv.carry_forward, "apply_to_check",
        lambda carried, **k: ("failure", "Elder: 1 earlier finding(s) still open", k["summary"]),
    )

    out = cr_dispatch.dispatch_code_review(_payload(), blocking=True)

    assert out["result"] == "fail"
    assert world["posted"][0][0][3].conclusion == "failure"
    assert world["stored"] == []
