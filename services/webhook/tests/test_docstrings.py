from __future__ import annotations

from personas.code_reviewer.docstrings import find_docstring_targets, insert_docstring

import docstring_action


def test_find_docstring_targets_selects_changed_public_functions_without_docstrings():
    diff = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,2 +1,5 @@
 def old():
     return 1
+
+def fresh(value):
+    return value
"""
    source = "def old():\n    return 1\n\ndef fresh(value):\n    return value\n"
    targets, skipped = find_docstring_targets(diff, {"pkg/mod.py": source})
    assert [(target.name, target.line) for target in targets] == [("fresh", 4)]
    assert skipped == []


def test_added_lines_skips_header_without_dropping_first_content_line(monkeypatch):
    from types import SimpleNamespace

    from personas.code_reviewer.docstrings import _added_lines

    monkeypatch.setattr(
        "personas.code_reviewer.docstrings.parse_diff",
        lambda _: (SimpleNamespace(
            new_start=1, file_path="pkg/mod.py", body="+def fresh():",
        ),),
    )
    assert _added_lines("unused") == {"pkg/mod.py": {1}}


def test_insert_docstring_preserves_function_body_and_indentation():
    source = "def fresh(value):\n    return value\n"
    out = insert_docstring(source, line=1, docstring="Return the supplied value.")
    assert out == (
        'def fresh(value):\n    """Return the supplied value."""\n'
        "    return value\n"
    )


def test_insert_docstring_rejects_empty_function_body(monkeypatch):
    import ast

    import pytest

    function = ast.FunctionDef(
        name="fresh", args=ast.arguments(
            posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[],
            defaults=[],
        ), body=[], decorator_list=[],
    )
    function.lineno = function.end_lineno = 1
    function.col_offset = 0
    module = ast.Module(body=[function], type_ignores=[])
    monkeypatch.setattr(
        "personas.code_reviewer.docstrings.ast.parse",
        lambda *_args, **_kwargs: module,
    )
    with pytest.raises(ValueError, match="empty body"):
        insert_docstring("def fresh():\n", line=1, docstring="Description.")


def test_run_docstrings_commits_once_and_reports_changed_file():
    diff = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,0 +1,2 @@
+def fresh(value):
+    return value
"""
    commits = []
    replies = []
    result = docstring_action.run_docstrings(
        install_id=1, owner="o", repo="r", pr_number=2, head_sha="head",
        token_fn=lambda callback: callback("token"),
        fetch_diff=lambda *_: diff,
        fetch_file=lambda *_: "def fresh(value):\n    return value\n",
        generate=lambda *_: "Return the supplied value.",
        apply_commit=lambda *args: commits.append(args) or "new-head",
        post_reply=replies.append,
    )
    assert result["changed"] == ["pkg/mod.py"]
    assert len(commits) == 1
    assert "`pkg/mod.py`" in replies[0]
    assert "Skipped:\n- (none)" in replies[0]


def test_run_docstrings_reports_fork_refusal_without_commit():
    from autofix import CommitRefused

    diff = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,0 +1,2 @@
+def fresh(value):
+    return value
"""
    replies = []

    def refuse(*_args):
        raise CommitRefused("this PR comes from a fork")

    result = docstring_action.run_docstrings(
        install_id=1, owner="o", repo="r", pr_number=2, head_sha="head",
        token_fn=lambda callback: callback("token"),
        fetch_diff=lambda *_: diff,
        fetch_file=lambda *_: "def fresh(value):\n    return value\n",
        generate=lambda *_: "Return the supplied value.",
        apply_commit=refuse,
        post_reply=replies.append,
    )
    assert result["reason"] == "commit_failed"
    assert "this PR comes from a fork" in replies[0]
    assert "pkg/mod.py" in replies[0]
