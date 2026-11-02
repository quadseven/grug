"""Tests for the deterministic complexity source (#532)."""

from __future__ import annotations

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.complexity import (
    cognitive_complexity,
    cyclomatic_complexity,
    scan_complexity,
    scan_complexity_full,
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
    body = f"@@ -1 +{start} @@\n" + "\n".join("+" + ln for ln in added)
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
        assert f.severity == "medium"
        assert f.suggestion
        assert f.effort == "quick-win"
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


# --- regression gate: publish what THIS PR did, not pre-existing debt --------

def _tangled(extra: int = 0) -> str:
    body = "\n".join(f"    if x{i}: pass" for i in range(18 + extra))
    return f"def handler(x):\n{body}\n    return 1\n"


def _one_hunk() -> tuple[DiffHunk, ...]:
    return (DiffHunk(file_path="a.py", new_start=1, new_lines=frozenset(),
                     body="@@ -1 +1 @@\n+    if x0: pass\n"),)


def test_touching_a_tangled_function_without_worsening_it_is_silent():
    """The measured failure. PR #766 was told `poller_handler.handler` was
    cyclomatic 30 / cognitive 53 - EXACTLY main's baseline, which the PR had
    not touched (#767). An adversarial audit put this rule at 85 of the last
    120 findings, most replies saying "pre-existing"."""
    out = scan_complexity(_one_hunk(), {"a.py": _tangled()},
                          base_contents={"a.py": _tangled()})
    assert out == ()


def test_improving_a_tangled_function_is_silent():
    """Still over cap, but better than base. Nagging someone for reducing
    complexity is the fastest way to teach them to ignore the rule."""
    out = scan_complexity(_one_hunk(), {"a.py": _tangled()},
                          base_contents={"a.py": _tangled(10)})
    assert out == ()


def test_second_push_shrinking_function_under_cap_uses_head_source():
    """A Living Hunt delta must measure the fetched second-push head."""
    first_push = _tangled()
    second_push = "def handler(x):\n    return 1\n"
    # The delta has new-side line numbers from push 2. Supplying the old file
    # here reproduces the stale-source failure; dispatch must supply second_push.
    second_push_hunk = (
        DiffHunk(
            file_path="a.py", new_start=1, new_lines=frozenset({2}),
            body="@@ -1,19 +1,2 @@\n def handler(x):\n+    return 1\n",
        ),
    )
    assert scan_complexity(
        second_push_hunk, {"a.py": second_push},
        base_contents={"a.py": first_push},
    ) == ()


def test_second_push_over_cap_finding_uses_head_score_and_anchor():
    first_push = _tangled()
    second_push = _tangled(6)
    hunks = (_hunk("a.py", 8, ["    if x6: pass"]),)
    scan = scan_complexity_full(
        hunks, {"a.py": second_push}, base_contents={"a.py": first_push},
    )
    assert len(scan.findings) == 1
    finding = scan.findings[0]
    assert finding.line == 8
    assert "cyclomatic 25 (cap 15)" in finding.message
    assert "moved it 19->25 cyclomatic" in finding.message


def test_existing_function_worsening_is_summary_advice_with_the_delta():
    """An existing function made worse is advice in the summary, not a
    thread: measured on Loom lane PRs, this rule was 220 of about 1,000
    findings, most of them threads on long-lived functions a PR touched
    lightly, each costing a fix round. The delta stays in the text."""
    out = scan_complexity(_one_hunk(), {"a.py": _tangled(6)},
                          base_contents={"a.py": _tangled()})
    assert len(out) == 1
    assert "moved it" in out[0].message      # states what THIS PR did
    assert "->" in out[0].message
    assert out[0].severity == "low"
    assert out[0].suggestion is None
    from personas.code_reviewer.persona import CodeReviewEvaluation
    from personas.code_reviewer.dispatch import build_review_result, _summary_markdown
    evaluation = CodeReviewEvaluation(findings=out, conclusion="success")
    result, posted = build_review_result(evaluation, head_sha="a" * 40, event="COMMENT")
    assert result is None
    assert posted == ()
    assert "Maintainability notes" in _summary_markdown(evaluation)[1]


def test_existing_function_grown_18_to_20_is_advisory_not_a_thread():
    """The measured shape: a PR moved an existing function 18->20 against a
    cap of 15. Before, ending five over the cap opened a medium thread."""
    base = _tangled(-1)                      # 17 branches: cyclomatic 18
    head = _tangled(1)                       # 19 branches: cyclomatic 20
    out = scan_complexity(_one_hunk(), {"a.py": head},
                          cyclomatic_cap=15, cognitive_cap=100,
                          base_contents={"a.py": base})
    assert len(out) == 1
    assert "cyclomatic 20 (cap 15)" in out[0].message
    assert "moved it 18->20 cyclomatic" in out[0].message
    assert (out[0].severity, out[0].suggestion) == ("low", None)
    from personas.code_reviewer.persona import CodeReviewEvaluation
    from personas.code_reviewer.dispatch import build_review_result
    evaluation = CodeReviewEvaluation(findings=out, conclusion="success")
    result, posted = build_review_result(evaluation, head_sha="a" * 40, event="COMMENT")
    assert result is None and posted == ()


def test_new_function_over_cap_by_the_margin_is_a_medium_thread():
    out = scan_complexity(_one_hunk(), {"a.py": _tangled(1)},
                          cyclomatic_cap=15, cognitive_cap=100,
                          base_contents={"a.py": "def other():\n    pass\n"})
    assert len(out) == 1
    assert "New function" in out[0].message
    assert out[0].severity == "medium"
    from personas.code_reviewer.persona import CodeReviewEvaluation
    from personas.code_reviewer.dispatch import build_review_result
    evaluation = CodeReviewEvaluation(findings=out, conclusion="success")
    result, posted = build_review_result(evaluation, head_sha="a" * 40, event="COMMENT")
    assert result is not None
    assert len(result.comments) == 1
    assert posted[0].severity == "medium"
    assert "Extract the new branching" in posted[0].suggestion


def test_one_or_two_point_rise_is_summary_advice_without_inline_suggestion():
    base = _tangled()
    for rise in (1, 2):
        scan = scan_complexity_full(
            _one_hunk(), {"a.py": _tangled(rise)},
            cyclomatic_cap=19, cognitive_cap=100,
            base_contents={"a.py": base},
        )
        out = scan.findings
        assert len(out) == 1
        assert scan.suppressed == ()
        assert out[0].suggestion is None
        assert out[0].severity == "low"
        from personas.code_reviewer.persona import CodeReviewEvaluation
        from personas.code_reviewer.dispatch import build_review_result, _summary_markdown
        evaluation = CodeReviewEvaluation(findings=out, conclusion="success")
        result, posted = build_review_result(evaluation, head_sha="a" * 40, event="COMMENT")
        assert result is None
        assert posted == ()
        assert "Maintainability notes" in _summary_markdown(evaluation)[1]


def test_severity_and_split_turn_on_exactly_at_the_inline_margin():
    """For a new function, ending four points over the cap is low with no
    split, summary only; five points over meets the margin: medium with a
    split. Pins the boundary so the low arm cannot quietly drift back."""
    base = "def other():\n    pass\n"
    below, at = (
        scan_complexity(_one_hunk(), {"a.py": _tangled(extra)},
                        cyclomatic_cap=15, cognitive_cap=100,
                        base_contents={"a.py": base})
        for extra in (0, 1)
    )
    assert [(f.severity, f.suggestion is None) for f in below] == [("low", True)]
    assert [(f.severity, f.suggestion is None) for f in at] == [("medium", False)]


def test_new_function_over_cap_is_reported():
    out = scan_complexity(_one_hunk(), {"a.py": _tangled()},
                          base_contents={"a.py": "def other():\n    pass\n"})
    assert len(out) == 1
    assert "New function" in out[0].message


def test_absent_base_preserves_absolute_behaviour():
    """Backward compatible: safe to merge before dispatch is wired, and a
    base-fetch failure degrades to the old behaviour rather than going
    silent - silence on a fetch error would hide real regressions."""
    out = scan_complexity(_one_hunk(), {"a.py": _tangled()})
    assert len(out) == 1


def test_duplicate_function_names_compare_against_the_worst_base():
    """Two `handler`s in one file (a method on two classes). Comparing the
    tangled head against the GENTLER twin would manufacture a fake
    regression, so the base keeps the worst score."""
    base = _tangled(8) + "\nclass B:\n    def handler(self):\n        return 1\n"
    out = scan_complexity(_one_hunk(), {"a.py": _tangled()}, base_contents={"a.py": base})
    assert out == ()


def test_unparseable_base_degrades_to_absolute_not_silence():
    """A base that does not parse must not be read as 'no complexity there',
    which would turn every touched function into a fake new-function finding
    or, worse, silently swallow a real one."""
    out = scan_complexity(_one_hunk(), {"a.py": _tangled()},
                          base_contents={"a.py": "def broken(:\n"})
    assert len(out) == 1


# --- suppression visibility (#781): the gate must be countable, not just quiet --

def test_scan_complexity_matches_the_findings_half_of_the_full_scan():
    """scan_complexity is a thin view over scan_complexity_full - same
    findings, every time, for every case above."""
    hunks, contents = _one_hunk(), {"a.py": _tangled(6)}
    base = {"a.py": _tangled()}
    assert scan_complexity(hunks, contents, base_contents=base) == (
        scan_complexity_full(hunks, contents, base_contents=base).findings
    )


def test_not_worsened_over_cap_function_is_counted_suppressed():
    scan = scan_complexity_full(_one_hunk(), {"a.py": _tangled()},
                                base_contents={"a.py": _tangled()})
    assert scan.findings == ()
    assert len(scan.suppressed) == 1
    s = scan.suppressed[0]
    assert s.file == "a.py"
    assert s.function == "handler"
    assert s.cyclomatic == s.base_cyclomatic
    assert s.cognitive == s.base_cognitive


def test_improved_over_cap_function_is_counted_suppressed():
    scan = scan_complexity_full(_one_hunk(), {"a.py": _tangled()},
                                base_contents={"a.py": _tangled(10)})
    assert scan.findings == ()
    assert len(scan.suppressed) == 1
    assert scan.suppressed[0].cyclomatic < scan.suppressed[0].base_cyclomatic


def test_material_worsening_reports_and_suppresses_nothing():
    scan = scan_complexity_full(_one_hunk(), {"a.py": _tangled(6)},
                                base_contents={"a.py": _tangled()})
    assert len(scan.findings) == 1
    assert scan.suppressed == ()


def test_no_base_never_populates_suppressed():
    """Without a base, _is_regression always returns True (fall back to
    absolute behaviour), so nothing can land in `suppressed` - there is no
    base score to compare against and call a non-regression."""
    scan = scan_complexity_full(_one_hunk(), {"a.py": _tangled()})
    assert len(scan.findings) == 1
    assert scan.suppressed == ()


def test_under_cap_function_is_neither_reported_nor_suppressed():
    src = "def ok(x):\n    if x:\n        return 1\n    return 0\n"
    hunks = (_hunk("services/y.py", 2, ["        return 1"]),)
    scan = scan_complexity_full(hunks, {"services/y.py": src},
                                base_contents={"services/y.py": src})
    assert scan.findings == ()
    assert scan.suppressed == ()


# --- per-repo caps (grug#1041) ---------------------------------------------

def test_repo_caps_come_from_the_repo_config():
    from personas.code_reviewer import dispatch as cr_dispatch
    from unittest.mock import patch

    cfg = {"complexity_cyclomatic_cap": 30, "complexity_cognitive_cap": None}
    with patch("adapters.install_store.get_repo_config", return_value=cfg):
        assert cr_dispatch._repo_complexity_caps(7, 42) == (30, None)


def test_repo_caps_fall_back_to_env_default_when_the_config_read_raises():
    """A config-store hiccup must not abort the review: (None, None) makes the
    scanner use its env default."""
    from personas.code_reviewer import dispatch as cr_dispatch
    from unittest.mock import patch

    with patch("adapters.install_store.get_repo_config", side_effect=RuntimeError("db")):
        assert cr_dispatch._repo_complexity_caps(7, 42) == (None, None)


def test_a_repo_specific_cap_changes_which_functions_report():
    cfg_loose = {"complexity_cyclomatic_cap": 500, "complexity_cognitive_cap": 500}
    cfg_tight = {"complexity_cyclomatic_cap": 2, "complexity_cognitive_cap": 500}
    from personas.code_reviewer import dispatch as cr_dispatch
    from unittest.mock import patch

    hunks, contents = _one_hunk(), {"a.py": _tangled()}
    results = []
    for cfg in (cfg_loose, cfg_tight):
        with patch("adapters.install_store.get_repo_config", return_value=cfg):
            cyclo, cog = cr_dispatch._repo_complexity_caps(7, 42)
        results.append(
            scan_complexity_full(hunks, contents, cyclomatic_cap=cyclo,
                                 cognitive_cap=cog).findings
        )
    assert results[0] == ()
    assert len(results[1]) == 1


class TestCarriedRowRescoredAtHead:
    """A carried high-complexity row is judged against the head's own source."""

    @staticmethod
    def _record(message: str):
        import review_session as rs

        return rs.FindingRecord(
            finding_id="f1", persona="elder", path="a.py", line=2,
            rule="high-complexity", category="maintainability",
            severity="medium", confidence=None, message=message, evidence="",
            source="llm", state="open", state_sha="aaa", first_seen_sha="aaa",
            last_seen_sha="aaa", history=(),
        )

    @staticmethod
    def _body(branches: int) -> str:
        return "def big(x):\n" + "".join(f"    if x == {i}:\n        pass\n" for i in range(branches))

    def _anchor(self, source: str, message: str):
        from personas.code_reviewer import hunt_link

        anchors = hunt_link.anchors_for(
            (self._record(message),), {"a.py": source}, frozenset({"a.py"}), True,
        )
        return anchors["f1"]

    def test_function_shrunk_under_the_cap_is_changed(self):
        msg = "Function `big` too tangled -- cyclomatic 16 (cap 15). Grug say: split."
        assert self._anchor(self._body(1), msg) == "changed"

    def test_function_removed_is_changed(self):
        msg = "Function `big` too tangled -- cyclomatic 16 (cap 15). Grug say: split."
        assert self._anchor("def other():\n    return 1\n", msg) == "changed"

    def test_function_with_a_different_score_is_changed(self):
        msg = "Function `big` too tangled -- cyclomatic 16 (cap 15). Grug say: split."
        assert self._anchor(self._body(17), msg) == "changed"

    def test_function_still_at_the_recorded_score_stays_unchanged(self):
        msg = "Function `big` too tangled -- cyclomatic 16 (cap 15). Grug say: split."
        assert self._anchor(self._body(15), msg) == "unchanged"
