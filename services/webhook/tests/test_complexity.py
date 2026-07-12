"""Tests for the deterministic complexity source (#532)."""

from __future__ import annotations

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.complexity import (
    cognitive_complexity,
    cyclomatic_complexity,
    scan_complexity,
)
import ast


def _func(src):
    return ast.parse(src).body[0]


class TestMetrics:
    def test_flat_function_is_one(self):
        assert cyclomatic_complexity(_func("def f():\n    return 1\n")) == 1
        assert cognitive_complexity(_func("def f():\n    return 1\n")) == 0

    def test_branches_and_boolops_count(self):
        src = (
            "def f(x):\n"
            "    if x and x > 0 or x < -1:\n"   # 1 if + boolop(and,or)=+2
            "        for i in range(x):\n"       # +1
            "            if i:\n"                # +1
            "                pass\n"
        )
        # 1 base + if + (3 boolop values -1 =2) + for + if = 6
        assert cyclomatic_complexity(_func(src)) == 6

    def test_cognitive_penalizes_nesting(self):
        flat = _func("def f(x):\n    if x: pass\n    if x: pass\n    if x: pass\n")
        nested = _func(
            "def f(x):\n"
            "    if x:\n"
            "        if x:\n"
            "            if x:\n"
            "                pass\n"
        )
        # 3 flat ifs = 1+1+1 = 3; nested = 1 + 2 + 3 = 6 (deeper reads worse)
        assert cognitive_complexity(flat) == 3
        assert cognitive_complexity(nested) == 6


def _hunk(path, start, added):
    body = "@@ -1 +%d @@\n" % start + "\n".join("+" + ln for ln in added)
    return DiffHunk(
        file_path=path, new_start=start,
        new_lines=frozenset(range(start, start + len(added))), body=body,
    )


class TestScanComplexity:
    _TANGLED = (
        "def tangled(x):\n"
        + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(20))
        + "    return -1\n"
    )

    def test_over_cap_function_flagged_and_diff_anchored(self):
        # a changed line inside the tangled function
        hunks = (_hunk("services/x.py", 3, ["        return 3"]),)
        out = scan_complexity(
            hunks, {"services/x.py": self._TANGLED},
            cyclomatic_cap=15, cognitive_cap=25,
        )
        assert len(out) == 1
        f = out[0]
        assert f.rule_name == "high-complexity"
        assert f.severity == "medium"          # advisory, never blocks alone
        assert f.effort == "heavy-lift"
        assert "tangled" in f.message
        assert f.line == 3                     # anchored on the changed line

    def test_untouched_function_not_scanned(self):
        # change a line OUTSIDE the tangled function (line 999 -> no overlap)
        hunks = (_hunk("services/x.py", 999, ["+noise"]),)
        out = scan_complexity(hunks, {"services/x.py": self._TANGLED})
        assert out == ()

    def test_simple_function_under_cap_not_flagged(self):
        src = "def ok(x):\n    if x:\n        return 1\n    return 0\n"
        hunks = (_hunk("services/y.py", 2, ["        return 1"]),)
        assert scan_complexity(hunks, {"services/y.py": src}) == ()

    def test_non_python_skipped(self):
        hunks = (_hunk("web/app.ts", 1, ["const x = 1"]),)
        assert scan_complexity(hunks, {"web/app.ts": "const x=1;"}) == ()

    def test_unparseable_source_skipped(self):
        hunks = (_hunk("services/z.py", 1, ["def broken("]),)
        assert scan_complexity(hunks, {"services/z.py": "def broken(:\n"}) == ()

    def test_missing_full_file_content_skipped(self):
        hunks = (_hunk("services/x.py", 3, ["        return 3"]),)
        assert scan_complexity(hunks, {}) == ()   # no content -> can't measure
