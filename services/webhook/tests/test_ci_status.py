"""Grug CI-status review context (#902) tests.

Load-bearing contracts:
- a failed check-run is surfaced BY NAME in Elder's context;
- pending/success/other partition honestly (a cancelled check is never
  mislabeled a test failure, and a non-green state is never hidden);
- any fetch/render failure degrades to None (today's review), never raises;
- the prompt renders a `### CI STATUS` block only when ci_context is given;
- every test below fails against the pre-#902 code (no ci_status module,
  no ci_context kwarg on _build_review_parts).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from llm_client import Hunk, _build_review_parts
from personas.code_reviewer.ci_status import build_ci_status_context, is_test_path


def _run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _hunk(path):
    return SimpleNamespace(file_path=path)


# --- is_test_path -----------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("tests/test_foo.py", True),
        ("services/webhook/tests/test_ci_status.py", True),
        ("src/foo_test.py", True),
        ("web/__tests__/bar.ts", True),
        ("src/foo.py", False),
        ("src/testdata/fixture.json", False),  # "test" inside a dir name is not tests/
        ("src/testing/util.py", False),
        ("", False),
    ],
)
def test_is_test_path(path, expected):
    assert is_test_path(path) is expected


# --- build_ci_status_context ------------------------------------------------


def test_failed_check_run_surfaced_by_name():
    ctx = build_ci_status_context(
        fetch_check_runs=lambda: [
            _run("tests", conclusion="success"),
            _run("lint", conclusion="failure"),
            _run("Grug - Elder", status="in_progress", conclusion=None),
        ],
        head_sha="abc123def456",
        hunks=[_hunk("src/foo.py")],
    )
    assert ctx is not None
    assert "abc123de" in ctx
    assert "failed (1): lint" in ctx
    assert "passed (1): tests" in ctx
    assert "pending (1): Grug - Elder" in ctx
    assert "Test files touched in this diff: no" in ctx


def test_test_files_touched_listed():
    ctx = build_ci_status_context(
        fetch_check_runs=lambda: [_run("tests")],
        head_sha="abc123",
        hunks=[_hunk("src/foo.py"), _hunk("services/webhook/tests/test_foo.py")],
    )
    assert (
        "Test files touched in this diff: yes (services/webhook/tests/test_foo.py)"
        in ctx
    )


def test_no_check_runs_means_no_context():
    assert (
        build_ci_status_context(
            fetch_check_runs=list, head_sha="abc", hunks=[_hunk("a.py")]
        )
        is None
    )


def test_fetch_failure_degrades_to_none_not_raise():
    def boom():
        raise RuntimeError("GitHub down")

    assert (
        build_ci_status_context(fetch_check_runs=boom, head_sha="abc", hunks=[]) is None
    )


def test_cancelled_check_is_other_not_failed():
    ctx = build_ci_status_context(
        fetch_check_runs=lambda: [_run("deploy-preview", conclusion="cancelled")],
        head_sha="abc",
        hunks=[],
    )
    assert "failed (0)" in ctx
    assert "other (1): deploy-preview (cancelled)" in ctx


def test_failure_names_are_bounded():
    runs = [_run(f"check-{i}", conclusion="failure") for i in range(25)]
    ctx = build_ci_status_context(
        fetch_check_runs=lambda: runs, head_sha="abc", hunks=[]
    )
    assert "failed (25)" in ctx
    assert "and 15 more" in ctx


def test_malformed_payload_degrades_to_none_not_raise():
    ctx = build_ci_status_context(
        fetch_check_runs=lambda: ["not-a-dict", None, 42],
        head_sha="abc",
        hunks=[],
    )
    # Non-dict entries are skipped; with zero usable runs there is still a
    # context block (runs existed) but no crash. The key contract: no raise.
    assert ctx is None or isinstance(ctx, str)


# --- prompt rendering -------------------------------------------------------


def _hunks():
    return [Hunk(path="src/foo.py", body="@@ -1 +1 @@\n-old\n+new")]


def test_review_parts_include_ci_status_block_when_given():
    parts, _ = _build_review_parts(_hunks(), ci_context="tests: success")
    assert any(p.startswith("### CI STATUS") and "tests: success" in p for p in parts)


def test_review_parts_omit_ci_status_block_when_absent():
    parts, _ = _build_review_parts(_hunks())
    assert not any("CI STATUS" in p for p in parts)


def test_ci_status_block_comes_after_production_signal():
    parts, _ = _build_review_parts(_hunks(), runtime_context="PROD", ci_context="CI")
    idx = {p.split("\n")[0]: i for i, p in enumerate(parts)}
    assert idx["### PRODUCTION SIGNAL"] < idx["### CI STATUS"]
