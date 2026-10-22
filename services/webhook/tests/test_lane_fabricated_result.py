"""Tests for the fabricated-result checks (#1368).

Recorded shapes (anonymized):

- a lane PR added a benchmark row and a sentence "Pass rates were 47.0%
  energy, 43.0% protein, 40.0% carb, 20.0% fat" to a README whose table
  states 30 rows, with no raw output; 47.0% and 43.0% of 30 are not whole
  counts;
- a lane PR added a model comparison verdict whose note says the run used
  stand-in responses from `--simulate`, and an agreement of 7 of 8 (87.5%);
- a lane PR added an audit doc dated on a day nothing ran, saying two
  handsets were audited, with digests and no command output;
- a lane PR set `done_percent = min(len(sessions), 100)`.
"""

from __future__ import annotations

import time

from personas.code_reviewer import linked_issue
from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.fabricated_result import (
    MAX_ADDED_CHARS,
    MAX_RATE_CHECKS,
    RULE,
    scan_fabricated_results,
)
from personas.code_reviewer.lane_work import lane_work_findings

_FOOTER = "\n---\nLoom ticket example.bench-44-r2, lane spark, model m.\nRefs #44\n"


def _added_file(path: str, lines: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{ln}\n" for ln in lines)
    )


def _edited_file(path: str, context: list[str], added: list[str], header: str = "") -> str:
    """A hunk that keeps `context` and appends `added` after it."""
    body = "".join(f" {ln}\n" for ln in context) + "".join(f"+{ln}\n" for ln in added)
    return (
        f"diff --git a/{path} b/{path}\nindex 2222222..3333333 100644\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -10,{len(context)} +10,{len(context) + len(added)} @@ {header}\n" + body
    )


def _scan(diff: str):
    return scan_fabricated_results(file_changes(diff), parse_diff(diff))


_TABLE = [
    "| model | energy | protein | carb | fat | pass |",
    "| --- | --- | --- | --- | --- | --- |",
    "| model-a | 26.9% | 32.9% | 30.4% | 50.6% | ~35% |",
]


def _benchmark_diff(sentence: str) -> str:
    return _edited_file(
        "suites/nutrition/README.md", _TABLE,
        ["| model-b | 28.4% | 30.9% | 29.1% | 48.7% | 34% |", "", sentence],
        header="All runs 2026-05-08, benchmark v2, 30 rows unless noted.",
    )


# --- impossible rate ----------------------------------------------------------


def test_the_recorded_benchmark_row_is_a_high_finding_on_the_rate_line():
    sentence = "Pass rates for model-b were 47.0% energy, 43.0% protein, 40.0% carb, 20.0% fat."
    findings = _scan(_benchmark_diff(sentence))
    assert len(findings) == 1, findings
    f = findings[0]
    assert (f.rule_name, f.severity, f.file) == (RULE, "high", "suites/nutrition/README.md")
    assert f.line == 15  # the sentence: new_start 10, three context lines, three added
    assert "47.0%, 43.0%" in f.message and "40.0%" not in f.message
    assert "(30)" in f.message


def test_rates_a_whole_count_gives_are_fine():
    # 14/30 = 46.67 and 13/30 = 43.33: rounded or truncated, both possible.
    for sentence in (
        "Pass rates were 46.7% energy, 43.3% protein, 40.0% carb, 20.0% fat.",
        "Pass rates were 46.6% energy, 43.3% protein.",
    ):
        assert _scan(_benchmark_diff(sentence)) == (), sentence


def test_a_percentage_that_is_not_a_rate_or_has_no_stated_size_is_fine():
    # MAPE and other means are not counts out of N.
    assert _scan(_benchmark_diff("Mean error for model-b was 47.0% energy.")) == ()
    no_size = _edited_file("docs/results.md", ["Results:"], ["Pass rate: 47.0%."])
    assert _scan(no_size) == ()


def test_a_size_stated_any_way_is_used_and_any_stated_size_can_make_it_possible():
    # "n": 7 in data: 3/7 = 42.86, so 42.9% is fine and 43.0% is not.
    data = _added_file("results/scoreboard.json", [
        '{"model": "m", "n": 7, "detail": "pass rate 43.0%"}',
    ])
    assert len(_scan(data)) == 1
    ok = data.replace("43.0%", "42.9%")
    assert _scan(ok) == ()
    # Two stated sizes: 47.0% is impossible for 30 but 47/100 is whole.
    both = _edited_file("docs/bench.md", ["30 rows on the slice; 100 rows on the full set."],
                        ["Pass rate 47.0%."])
    assert _scan(both) == ()


def test_test_files_and_code_are_not_read_for_rates():
    test = _added_file("tests/test_bench.py", ["# 30 rows", "# pass rate 47.0%"])
    code = _added_file("bench/report.py", ["# 30 rows", "# pass rate 47.0%"])
    assert _scan(test + code) == ()


# --- simulated result ---------------------------------------------------------


_VERDICT = [
    "# Judge model comparison",
    "",
    "> NOTE: this run used deterministic stand-in responses (`--simulate`) because",
    "> the live gateway is not reachable from this environment.",
    "",
    "## Results",
    "- agreement: 7 of 8 (87.5%)",
    "",
    "## Verdict",
    "- recommendation: do not switch",
]


def test_a_verdict_from_the_simulate_path_is_a_high_finding_on_the_note():
    findings = _scan(_added_file("scripts/emitter/comparison_verdict.md", _VERDICT))
    assert len(findings) == 1, findings
    f = findings[0]
    assert (f.rule_name, f.severity, f.line) == (RULE, "high", 3)
    assert "simulated or stubbed path" in f.message


def test_a_results_file_from_a_real_run_or_a_doc_about_the_flag_is_fine():
    real = [ln for ln in _VERDICT if "stand-in" not in ln and "live gateway" not in ln]
    assert _scan(_added_file("scripts/emitter/comparison_verdict.md", real)) == ()
    usage = ["# Usage", "", "Pass `--simulate` to try the harness offline (87.5% faster)."]
    assert _scan(_added_file("scripts/emitter/USAGE.md", usage)) == ()


# --- unbacked audit -----------------------------------------------------------


_AUDIT = [
    "# Fleet audit: test-key signed installs",
    "# Audit Date: 2026-10-15",
    "",
    "Both handsets were audited by pulling the installed APK from the device.",
    "",
    "| Handset | Signer | Status |",
    "|---|---|---|",
    "| Handset 1 | 0123abcd | PASS |",
]


def test_a_new_dated_audit_doc_with_no_output_is_a_high_finding():
    findings = _scan(_added_file("docs/fleet-audit-testkey-installs.md", _AUDIT))
    assert len(findings) == 1, findings
    f = findings[0]
    assert (f.rule_name, f.severity, f.line) == (RULE, "high", 2)
    assert "no command output" in f.message


def test_an_audit_with_its_output_or_a_raw_file_or_a_run_link_is_fine():
    fenced = [*_AUDIT, "", "```", "$ apksigner verify --print-certs app.apk", "0123abcd", "```"]
    assert _scan(_added_file("docs/fleet-audit.md", fenced)) == ()
    raw = _added_file("docs/fleet-audit.md", _AUDIT) + _added_file(
        "docs/audit/raw/handset-1.log", ["0123abcd"],
    )
    assert _scan(raw) == ()
    linked = [*_AUDIT, "Run: https://github.com/example/app/actions/runs/123456"]
    assert _scan(_added_file("docs/fleet-audit.md", linked)) == ()


def test_an_undated_or_unclaimed_or_edited_audit_doc_is_fine():
    undated = [ln for ln in _AUDIT if "2026" not in ln]
    assert _scan(_added_file("docs/fleet-audit.md", undated)) == ()
    plan = ["# Audit plan", "Run the audit on 2026-10-15 and record the output here."]
    assert _scan(_added_file("docs/fleet-audit.md", plan)) == ()
    edited = _edited_file("docs/fleet-audit.md", ["# Fleet audit"], _AUDIT[1:])
    assert _scan(edited) == ()


# --- percent from a count -----------------------------------------------------


def test_a_percentage_set_from_a_count_is_a_high_finding():
    code = _edited_file("services/core/view_model.py", ["    sessions = rows()"], [
        "    done_percent = min(len(sessions), 100) if sessions else None",
    ])
    findings = _scan(code)
    assert len(findings) == 1, findings
    f = findings[0]
    assert (f.rule_name, f.severity, f.line) == (RULE, "high", 11)
    assert "`done_percent`" in f.message
    js = _added_file("web/progress.ts", ["const donePct = items.length;"])
    assert len(_scan(js)) == 1


def test_a_divided_count_a_comparison_or_a_test_is_fine():
    lines = [
        "    done_percent = round(len(done) / len(total) * 100)",
        "    if done_percent == len(done):",
        "    percent_label: str = format_count(n)",
        "    total = len(items)",
    ]
    assert _scan(_added_file("services/core/view.py", lines)) == ()
    test = _added_file("tests/test_view.py", ["    expected_percent = len(rows)"])
    assert _scan(test) == ()


# --- seam, category, bounds -------------------------------------------------


def test_lane_work_findings_reports_the_fabricated_result():
    sentence = "Pass rates for model-b were 47.0% energy, 43.0% protein."
    diff = _benchmark_diff(sentence)
    findings = lane_work_findings(
        diff_text=diff, hunks=parse_diff(diff), file_contents={}, base_line_counts={},
        boxes=(), pr_title="Measure model-b on the nutrition slice",
        pr_body="## Why\nNo number yet.\n" + _FOOTER,
    )
    assert [(f.rule_name, f.severity) for f in findings] == [(RULE, "high")]


def test_the_rule_shows_under_a_named_category_not_general():
    assert linked_issue.RULE_CATEGORIES[RULE] == "requirements"


def test_the_scan_is_linear_on_a_large_diff():
    # Every rate is possible, so every line of every file is read in full.
    rates = " ".join(f"{100 * k / 30:.1f}%" for k in range(31))
    lines = [f"Pass rates {rates} on 30 rows, n={j}, {j} of 90, {j}/120." for j in range(40)]
    diff = "".join(_added_file(f"results/run-{i}.md", lines) for i in range(300))
    # Past the shared check budget the rate check is not evaluated, so an
    # impossible rate at the end draws no finding rather than a slow scan.
    diff += _benchmark_diff("Pass rates were 47.0% energy.")
    changes, hunks = file_changes(diff), parse_diff(diff)
    assert sum(len(h.body) for h in hunks) > 2_000_000
    assert 300 * 40 * 31 * 40 > MAX_RATE_CHECKS
    start = time.perf_counter()
    assert scan_fabricated_results(changes, hunks) == ()
    assert time.perf_counter() - start < 1.0


def test_a_diff_past_the_bound_is_not_evaluated():
    filler = ["x" * 1000] * (MAX_ADDED_CHARS // 1000 + 1)
    diff = _benchmark_diff("Pass rates were 47.0% energy.") + _added_file("notes.md", filler)
    assert _scan(diff) == ()
    long_line = _benchmark_diff("Pass rates were 47.0% energy." + " " * 5000)
    assert _scan(long_line) == ()
