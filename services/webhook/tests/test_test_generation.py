"""Tests for /grug tests (#1459, finishing touches test generation).

- find_test_targets selects changed public functions no test in the PR
  references, and names every candidate it skips with a reason
- run_tests routes through test_action, lands ONE bot commit, refuses
  fork PRs through the shared autofix commit path, and replies naming
  every file changed and every item skipped
"""

from __future__ import annotations

import pytest
import test_action
from personas.code_reviewer.test_generation import (
    find_test_targets,
    merge_generated_tests,
)
from personas.code_reviewer.test_generation import test_path_for as path_for

DIFF = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,2 +1,5 @@
 def old():
     return 1
+
+def fresh(value):
+    return value
"""
SOURCE = "def old():\n    return 1\n\ndef fresh(value):\n    return value\n"


def test_find_test_targets_selects_changed_public_function_without_a_test():
    targets, skipped = find_test_targets(DIFF, {"pkg/mod.py": SOURCE})
    assert [(t.name, t.line, t.test_path) for t in targets] == [
        ("fresh", 4, "pkg/tests/test_mod.py"),
    ]
    assert skipped == []


def test_find_test_targets_ignores_function_a_test_already_references():
    contents = {
        "pkg/mod.py": SOURCE,
        "pkg/tests/test_mod.py": "def test_fresh():\n    assert fresh(1) == 1\n",
    }
    targets, skipped = find_test_targets(DIFF, contents)
    assert targets == []
    assert skipped == []


def test_find_test_targets_skips_private_and_unreadable_and_unparseable():
    diff = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -0,0 +1,2 @@
+def _hidden(value):
+    return value
diff --git a/pkg/broken.py b/pkg/broken.py
--- a/pkg/broken.py
+++ b/pkg/broken.py
@@ -0,0 +1 @@
+def broken(:
diff --git a/pkg/gone.py b/pkg/gone.py
--- a/pkg/gone.py
+++ b/pkg/gone.py
@@ -0,0 +1 @@
+def gone():
"""
    contents = {
        "pkg/mod.py": "def _hidden(value):\n    return value\n",
        "pkg/broken.py": "def broken(:\n",
    }
    targets, skipped = find_test_targets(diff, contents)
    assert targets == []
    assert ("pkg/broken.py", "Python file does not parse") in skipped
    assert ("pkg/gone.py", "could not read changed Python file") in skipped


def test_test_path_for_places_tests_beside_the_source_package():
    assert path_for("pkg/mod.py") == "pkg/tests/test_mod.py"
    assert path_for("mod.py") == "tests/test_mod.py"
    assert path_for("a/b/c.py") == "a/b/tests/test_c.py"


def test_merge_generated_tests_creates_a_parseable_new_file():
    out = merge_generated_tests(None, "def test_fresh():\n    assert True\n")
    assert "def test_fresh():" in out
    compile(out, "test_mod.py", "exec")


def test_merge_generated_tests_appends_after_existing_tests():
    existing = "def test_old():\n    assert True\n"
    out = merge_generated_tests(existing, "def test_fresh():\n    assert True\n")
    assert out.startswith(existing.rstrip("\n"))
    assert "def test_fresh():" in out
    compile(out, "test_mod.py", "exec")


def test_merge_generated_tests_rejects_empty_or_unparseable_code():
    with pytest.raises(ValueError):
        merge_generated_tests(None, "   \n")
    with pytest.raises(ValueError):
        merge_generated_tests(None, "def test_fresh(:\n")


def _run(**overrides):
    kwargs = {
        "install_id": 1, "owner": "o", "repo": "r", "pr_number": 2,
        "head_sha": "head",
        "token_fn": lambda callback: callback("token"),
        "fetch_diff": lambda *_: DIFF,
        "fetch_file": lambda *_args: SOURCE,
        "generate": lambda *_: "def test_fresh():\n    assert fresh(2) == 2\n",
        "apply_commit": lambda *args: "new-head",
        "post_reply": lambda body: None,
        "fetch_pr_head": lambda *_: {"repo": {"full_name": "o/r"}},
    }
    kwargs.update(overrides)
    return test_action.run_tests(**kwargs)


def test_run_tests_commits_once_and_reports_the_new_test_file():
    commits = []
    replies = []
    result = _run(
        fetch_file=lambda *_args: SOURCE if _args[3] == "pkg/mod.py" else None,
        apply_commit=lambda *args: commits.append(args) or "new-head",
        post_reply=replies.append,
    )
    assert result["changed"] == ["pkg/tests/test_mod.py"]
    assert len(commits) == 1
    assert commits[0][5] == "Generate grug unit tests on PR #2"
    assert "def test_fresh():" in commits[0][4]["pkg/tests/test_mod.py"]
    assert "`pkg/tests/test_mod.py`" in replies[0]
    assert "Skipped:\n- (none)" in replies[0]


def test_run_tests_appends_to_an_existing_test_file():
    commits = []
    existing = "def test_old():\n    assert True\n"

    def fetch_file(*args):
        return SOURCE if args[3] == "pkg/mod.py" else existing

    result = _run(
        fetch_file=fetch_file,
        apply_commit=lambda *args: commits.append(args) or "new-head",
    )
    assert result["changed"] == ["pkg/tests/test_mod.py"]
    merged = commits[0][4]["pkg/tests/test_mod.py"]
    assert "def test_old():" in merged
    assert "def test_fresh():" in merged


def test_run_tests_refuses_a_fork_pr_before_generating_anything():
    commits = []
    replies = []
    result = _run(
        fetch_pr_head=lambda *_: {"repo": {"full_name": "someone-else/r"}},
        generate=lambda *_: (_ for _ in ()).throw(
            AssertionError("generate must not run for a fork PR"),
        ),
        apply_commit=lambda *args: commits.append(args) or "new-head",
        post_reply=replies.append,
    )
    assert result["reason"] == "fork_pr"
    assert commits == []
    assert "this PR comes from a fork" in replies[0]


def test_run_tests_reports_fork_refusal_without_a_commit():
    from autofix import CommitRefused

    replies = []

    def refuse(*_args):
        raise CommitRefused("this PR comes from a fork")

    result = _run(
        fetch_file=lambda *_args: SOURCE if _args[3] == "pkg/mod.py" else None,
        apply_commit=refuse,
        post_reply=replies.append,
    )
    assert result["reason"] == "commit_failed"
    assert "this PR comes from a fork" in replies[0]
    assert "pkg/tests/test_mod.py" in replies[0]


def test_run_tests_names_a_generation_failure_as_skipped_and_commits_nothing():
    commits = []

    def boom(*_args):
        raise RuntimeError("model unavailable")

    result = _run(
        fetch_file=lambda *_args: SOURCE if _args[3] == "pkg/mod.py" else None,
        generate=boom,
        apply_commit=lambda *args: commits.append(args) or "new-head",
    )
    assert result["status"] == "no_op"
    assert commits == []
    assert result["skipped"] == [
        ("pkg/mod.py:4 fresh", "generation failed (RuntimeError)"),
    ]


def test_run_tests_no_op_when_every_changed_function_is_covered():
    commits = []
    contents = {
        "pkg/mod.py": SOURCE,
        "pkg/tests/test_mod.py": "def test_fresh():\n    assert fresh(1) == 1\n",
    }
    result = _run(
        fetch_file=lambda *args: contents.get(args[3]),
        apply_commit=lambda *args: commits.append(args) or "new-head",
    )
    assert result["status"] == "no_op"
    assert commits == []
