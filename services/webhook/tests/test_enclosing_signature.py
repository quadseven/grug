"""Enclosing-signature context and bound-name downgrade (#1227).

Live instance 2026-10-17: a review posted three blocking markings on one
pull request - a NameError for `now`, a NameError for `authors`, and an
"inverted logic" claim built on the same misreading. Both names were
parameters of the enclosing method, whose signature sat about 40 lines
above the hunk in a file too large for the full-file block, so the model
never saw it.

Two halves of the fix, one contract each:
- the review and judge prompts carry the enclosing `def` signature of a
  hunk whose signature is outside the hunk and outside any full-file block;
- a not-defined finding for a name bound in an enclosing scope is
  downgraded to a non-blocking note before it can block the merge.
"""

from __future__ import annotations

import llm_client as lc
import review_evidence as ev
from llm_client import Hunk
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding, with_findings
from review_evidence import assemble_evidence


def _big_module() -> tuple[str, int, int]:
    """A Python module longer than the full-file budget, with a method
    whose multi-line signature sits 40 lines above the changed line.

    Returns (source, signature_line, changed_line)."""
    lines: list[str] = ["import datetime", ""]
    # Filler so the file exceeds the full-file context budget.
    for i in range(ev.MAX_FILE_CONTEXT_LINES):
        lines.append(f"CONST_{i} = {i}")
    lines.append("")
    lines.append("")
    lines.append("class Reviewer:")
    sig_line = len(lines) + 1
    lines.append("    def score(")
    lines.append("        self,")
    lines.append("        now: datetime.datetime,")
    lines.append("        authors: list[str],")
    lines.append("    ) -> int:")
    lines.append('        """Score a review."""')
    for i in range(34):
        lines.append(f"        step_{i} = {i}")
    changed = len(lines) + 1
    lines.append("        if now.hour > 12 and authors:")
    lines.append("            return len(authors)")
    lines.append("        return 0")
    lines.append("")
    return "\n".join(lines) + "\n", sig_line, changed


def _hunk_at(changed: int) -> Hunk:
    start = changed - 3
    body = (
        f"@@ -{start},6 +{start},6 @@ class Reviewer:\n"
        "         step_31 = 31\n"
        "         step_32 = 32\n"
        "         step_33 = 33\n"
        "-        if now.hour > 11 and authors:\n"
        "+        if now.hour > 12 and authors:\n"
        "             return len(authors)\n"
        "         return 0"
    )
    return Hunk(path="svc/reviewer.py", body=body)


# --- context half --------------------------------------------------------


def test_hunk_40_lines_below_signature_carries_signature_in_review_context():
    source, sig_line, changed = _big_module()
    assert changed - sig_line >= 40
    # Precondition: the file is too large for the full-file block, so on
    # main the signature is nowhere in the prompt.
    assert ev._render_file_block("svc/reviewer.py", source) == ""
    parts, _ = assemble_evidence(
        [_hunk_at(changed)],
        file_contents={"svc/reviewer.py": source},
    ).reviewer_view()
    joined = "\n".join(parts)
    assert "ENCLOSING SCOPE" in joined
    assert f"{sig_line}:     def score(" in joined
    assert "now: datetime.datetime," in joined
    assert "authors: list[str]," in joined
    assert "class Reviewer:" in joined
    # The signature block precedes the hunk it belongs to.
    assert joined.index("ENCLOSING SCOPE") < joined.index("```diff")


def test_judge_context_carries_the_same_signature():
    source, _, changed = _big_module()
    msgs = lc._build_judge_messages(
        [{"severity": "high", "rule_name": "undefined-name",
          "file": "svc/reviewer.py", "line": changed,
          "message": "`now` is not defined"}],
        assemble_evidence([_hunk_at(changed)], file_contents={"svc/reviewer.py": source}),
    )
    user = msgs[1]["content"]
    assert "ENCLOSING SCOPE" in user
    assert "now: datetime.datetime," in user


def test_no_signature_block_when_full_file_is_shown():
    """A small file already rides in the full-file block; repeating the
    signature would only spend tokens."""
    src = "def f(a, b):\n    x = 1\n    y = 2\n    return a + b\n"
    hunk = Hunk(path="s.py", body="@@ -4 +4 @@\n-    return a\n+    return a + b")
    parts, _ = assemble_evidence([hunk], file_contents={"s.py": src}).reviewer_view()
    joined = "\n".join(parts)
    assert "FULL FILE" in joined
    assert "ENCLOSING SCOPE" not in joined


def test_no_signature_block_without_content_or_for_non_python():
    hunk = Hunk(path="a.py", body="@@ -5 +5 @@\n+x")
    parts, _ = assemble_evidence([hunk]).reviewer_view()
    assert "ENCLOSING SCOPE" not in "\n".join(parts)
    big_yaml = "\n".join(f"k{i}: v" for i in range(900))
    parts, _ = assemble_evidence(
        [Hunk(path="a.yml", body="@@ -850 +850 @@\n+k: v")],
        file_contents={"a.yml": big_yaml},
    ).reviewer_view()
    assert "ENCLOSING SCOPE" not in "\n".join(parts)


def test_signature_inside_hunk_is_not_repeated():
    source, sig_line, _ = _big_module()
    body = (
        f"@@ -{sig_line},3 +{sig_line},3 @@ class Reviewer:\n"
        "     def score(\n"
        "         self,\n"
        "-        now,\n"
        "+        now: datetime.datetime,"
    )
    parts, _ = assemble_evidence(
        [Hunk(path="svc/reviewer.py", body=body)],
        file_contents={"svc/reviewer.py": source},
    ).reviewer_view()
    assert "ENCLOSING SCOPE" not in "\n".join(parts)


def test_unparseable_source_degrades_to_diff_only():
    bad = "def f(:\n" + "\n".join("x" for _ in range(900))
    parts, _ = assemble_evidence(
        [Hunk(path="b.py", body="@@ -850 +850 @@\n+x")],
        file_contents={"b.py": bad},
    ).reviewer_view()
    assert "ENCLOSING SCOPE" not in "\n".join(parts)


# --- judge / verification half -------------------------------------------


def downgrade_bound_names(findings, contents):
    # Imported per call so the context-half tests above fail on their own
    # assertions on a base without this module's new function.
    from personas.code_reviewer.verify import downgrade_bound_names as fn
    return fn(findings, contents)


def _finding(**kw) -> Finding:
    base = {
        "file": "svc/reviewer.py",
        "line": 1,
        "severity": "high",
        "rule_name": "undefined-name",
        "message": "msg",
        "suggestion": None,
    }
    base.update(kw)
    return Finding(**base)


def test_not_defined_finding_for_enclosing_parameter_is_not_blocking():
    source, _, changed = _big_module()
    f = _finding(
        line=changed,
        rule_name="name-error",
        message="NameError: `now` is not defined in this scope; this line crashes.",
    )
    out, downgraded = downgrade_bound_names((f,), {"svc/reviewer.py": source})
    assert len(out) == 1 and len(downgraded) == 1
    assert out[0].severity == "low"
    assert downgraded[0].names == ("now",)
    evaluation = with_findings(
        CodeReviewEvaluation(findings=(f,), conclusion="failure"), out,
    )
    assert evaluation.conclusion != "failure"


def test_second_live_name_also_downgraded():
    source, _, changed = _big_module()
    f = _finding(
        line=changed + 1,
        severity="critical",
        rule_name="runtime-error",
        message="`authors` is undefined here and raises NameError.",
    )
    out, downgraded = downgrade_bound_names((f,), {"svc/reviewer.py": source})
    assert out[0].severity == "low"
    assert downgraded[0].names == ("authors",)


def test_module_level_and_builtin_names_count_as_bound():
    src = "import os\n\ndef f():\n    return os.getcwd(), len('x')\n"
    for name in ("os", "len"):
        f = _finding(file="m.py", line=4, message=f"`{name}` is not defined")
        out, downgraded = downgrade_bound_names((f,), {"m.py": src})
        assert out[0].severity == "low", name
        assert downgraded


def test_truly_undefined_name_still_blocks():
    source, _, changed = _big_module()
    f = _finding(line=changed, message="NameError: `deadline` is not defined")
    out, downgraded = downgrade_bound_names((f,), {"svc/reviewer.py": source})
    assert out == (f,) and downgraded == ()


def test_name_bound_in_another_function_still_blocks():
    src = (
        "def a(now):\n"
        "    return now\n"
        "\n"
        "def b():\n"
        "    return now\n"
    )
    f = _finding(file="m.py", line=5, message="`now` is not defined")
    out, downgraded = downgrade_bound_names((f,), {"m.py": src})
    assert out == (f,) and downgraded == ()


def test_local_assigned_after_use_still_blocks():
    src = "def f():\n    print(x)\n    x = 1\n"
    f = _finding(file="m.py", line=2, message="`x` is not defined")
    out, downgraded = downgrade_bound_names((f,), {"m.py": src})
    assert out == (f,) and downgraded == ()


def test_mixed_names_with_one_unbound_still_blocks():
    source, _, changed = _big_module()
    f = _finding(
        line=changed,
        message="`now` and `deadline` are not defined",
    )
    out, downgraded = downgrade_bound_names((f,), {"svc/reviewer.py": source})
    assert out == (f,) and downgraded == ()


def test_unrelated_rule_and_missing_evidence_are_untouched():
    source, _, changed = _big_module()
    other = _finding(line=changed, rule_name="sql-injection",
                     message="`now` reaches a query")
    advisory = _finding(line=changed, severity="medium",
                        message="`now` is not defined")
    no_source = _finding(file="gone.py", line=3, message="`now` is not defined")
    no_name = _finding(line=changed, message="a name here is not defined")
    findings = (other, advisory, no_source, no_name)
    out, downgraded = downgrade_bound_names(findings, {"svc/reviewer.py": source})
    assert out == findings and downgraded == ()


def test_unparseable_source_keeps_finding():
    f = _finding(file="m.py", line=1, message="`now` is not defined")
    out, downgraded = downgrade_bound_names((f,), {"m.py": "def f(:\n"})
    assert out == (f,) and downgraded == ()



# --- bindings that do not exist at runtime (#1338) ------------------------


_TYPE_CHECKING_SRC = (
    "from typing import TYPE_CHECKING\n"
    "import typing\n"
    "\n"
    "if TYPE_CHECKING:\n"
    "    from widgets import Widget\n"
    "if typing.TYPE_CHECKING:\n"
    "    from gadgets import Gadget\n"
    "else:\n"
    "    Gadget = object\n"
    "\n"
    "def f(x):\n"
    "    return isinstance(x, Widget)\n"
)


def test_type_checking_only_import_used_at_runtime_still_blocks():
    """`if TYPE_CHECKING:` never runs, so `Widget` is unbound at line 12."""
    f = _finding(file="m.py", line=12, message="NameError: `Widget` is not defined")
    out, downgraded = downgrade_bound_names((f,), {"m.py": _TYPE_CHECKING_SRC})
    assert out == (f,) and downgraded == ()
    assert out[0].severity == "high"


def test_type_checking_else_branch_and_plain_import_still_bind():
    """No over-correction: the `else` of a TYPE_CHECKING test runs, and the
    same name imported unconditionally is bound."""
    f = _finding(file="m.py", line=12, message="`Gadget` is not defined")
    out, downgraded = downgrade_bound_names((f,), {"m.py": _TYPE_CHECKING_SRC})
    assert out[0].severity == "low" and downgraded[0].names == ("Gadget",)

    src = "from widgets import Widget\n\ndef f(x):\n    return isinstance(x, Widget)\n"
    f = _finding(file="m.py", line=4, message="`Widget` is not defined")
    out, downgraded = downgrade_bound_names((f,), {"m.py": src})
    assert out[0].severity == "low" and downgraded[0].names == ("Widget",)


_EXCEPT_SRC = (
    "def f():\n"
    "    try:\n"
    "        run()\n"
    "    except OSError as err:\n"
    "        log(err)\n"
    "    return err\n"
)


def test_except_name_used_after_its_handler_still_blocks():
    """Python deletes `err` when the handler ends: line 6 raises."""
    f = _finding(file="m.py", line=6, message="`err` is not defined after the except block")
    out, downgraded = downgrade_bound_names((f,), {"m.py": _EXCEPT_SRC})
    assert out == (f,) and downgraded == ()
    assert out[0].severity == "high"


def test_except_name_used_inside_its_handler_is_downgraded():
    f = _finding(file="m.py", line=5, message="`err` is not defined")
    out, downgraded = downgrade_bound_names((f,), {"m.py": _EXCEPT_SRC})
    assert out[0].severity == "low" and downgraded[0].names == ("err",)


def test_with_target_is_bound_after_the_with_line():
    src = (
        "def f(path):\n"
        "    with open(path) as fh, lock() as (a, b):\n"
        "        return fh.read(), a, b\n"
    )
    for name in ("fh", "a", "b"):
        f = _finding(file="m.py", line=3, message=f"`{name}` is not defined")
        out, downgraded = downgrade_bound_names((f,), {"m.py": src})
        assert out[0].severity == "low", name
        assert downgraded[0].names == (name,)


def test_dispatch_downgrade_turns_a_failing_review_into_a_pass():
    """Wiring: the Elder conclusion no longer fails on the live shape."""
    from personas.code_reviewer import dispatch

    source, _, changed = _big_module()
    f = _finding(line=changed, message="NameError: `now` is not defined")
    evaluation = CodeReviewEvaluation(findings=(f,), conclusion="failure")
    out = dispatch._apply_bound_name_downgrade(
        evaluation, {"svc/reviewer.py": source}, installation_id=1,
        owner="o", repo_name="r", pull_number=1, arm="tier1",
    )
    assert out.conclusion == "success"
    assert out.findings[0].severity == "low"
    assert "Downgraded to a note" in out.findings[0].message
