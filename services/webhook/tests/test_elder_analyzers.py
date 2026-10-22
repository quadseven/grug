"""Tests for Elder's inline OSS analyzer set (#1254).

Seams: which engines run (`configured_engines`), how each one is matched to
files, bounded and degraded (`run_analyzers`), how opengrep output becomes an
attributed marking (`map_opengrep_results`), how analyzer findings fold into
the model's own (`merge_analyzer_findings`), and the read-only sandbox every
engine runs in. The real-engine checks for the rule pack itself live in
test_elder_analyzer_rules.py.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import personas.code_reviewer.analyzers as an
import personas.code_reviewer.lint as lint_mod
from llm_client import Backend, FindingOrigin
from personas.code_reviewer.analyzer_sandbox import (
    AnalyzerSkip, read_only_tree, sandbox_env,
)
from personas.code_reviewer.analyzers import (
    AnalyzerSpec, RepoAnalyzerConfig, configured_engines, map_opengrep_results,
    merge_analyzer_findings, run_analyzers,
)
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding


def _hunk(path: str, lines: int = 3) -> DiffHunk:
    body = f"@@ -0,0 +1,{lines} @@\n" + "".join(f"+l{i}\n" for i in range(lines))
    return DiffHunk(file_path=path, new_start=1, new_lines=frozenset(), body=body)


def _finding(path="a.js", line=1, rule="r", message="m", origins=()):
    return Finding(file=path, line=line, severity="medium", rule_name=rule,
                   message=message, suggestion=None, origins=origins)


_MODEL = (FindingOrigin(backend=Backend.CAVE, model="m"),)


# --- configured_engines: a bounded, opt-in set ----------------------------

def test_no_engines_by_default():
    assert configured_engines("", "") == ()


def test_named_engines_run_in_order_and_unknown_names_are_ignored():
    assert configured_engines("opengrep, Ruff,pyflakes-from-the-internet", "") == (
        "opengrep", "ruff",
    )


def test_legacy_lint_switch_still_enables_ruff_once():
    assert configured_engines("", "ruff") == ("ruff",)
    assert configured_engines("ruff,opengrep", "ruff") == ("ruff", "opengrep")


def test_engine_set_is_capped(monkeypatch):
    fake = {f"e{i}": an.ANALYZERS["ruff"] for i in range(6)}
    monkeypatch.setattr(an, "ANALYZERS", fake)
    assert len(configured_engines(",".join(fake), "")) == an._MAX_ANALYZERS


def test_engines_read_from_environment(monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_ANALYZERS", "opengrep")
    monkeypatch.delenv("GRUG_LINT_EVIDENCE", raising=False)
    assert configured_engines() == ("opengrep",)


# --- run_analyzers: language matching, bounds, graceful skips -------------

def _spec(engine, exts, run, timeout_s=30.0):
    return AnalyzerSpec(engine, exts, "sh", run, timeout_s)


@pytest.fixture
def which_all(monkeypatch):
    monkeypatch.setattr(an.shutil, "which", lambda b: f"/usr/bin/{b}")


def test_each_engine_sees_only_files_in_its_languages(monkeypatch, which_all):
    seen: dict[str, set[str]] = {}

    def record(name):
        def run(ctx):
            seen[name] = set(ctx.files)
            return ()
        return run

    monkeypatch.setattr(an, "ANALYZERS", {
        "py": _spec("py", (".py",), record("py")),
        "js": _spec("js", (".js", ".tsx"), record("js")),
        "go": _spec("go", (".go",), record("go")),
    })
    files = {"a.py": "x", "b.tsx": "y", "c.js": "z", "README.md": "w"}
    run = run_analyzers((), files, ("py", "js", "go"))
    assert seen == {"py": {"a.py"}, "js": {"b.tsx", "c.js"}}
    # An engine with nothing to read neither ran nor degraded.
    assert run.ran == ("py", "js") and run.skipped == ()


def test_missing_binary_degrades_to_skip(monkeypatch):
    monkeypatch.setattr(an.shutil, "which", lambda b: None)
    monkeypatch.setattr(an, "ANALYZERS", {"js": _spec("js", (".js",), lambda c: 1 / 0)})
    run = run_analyzers((), {"a.js": "x"}, ("js",))
    assert run.findings == () and run.skipped == (("js", "missing"),)


@pytest.mark.parametrize("exc, reason", [
    (subprocess.TimeoutExpired(["x"], 1), "timeout"),
    (AnalyzerSkip("unparseable"), "unparseable"),
    (RuntimeError("boom"), "error"),
])
def test_failing_engine_is_a_skip_and_later_engines_still_run(
    monkeypatch, which_all, exc, reason,
):
    def boom(ctx):
        raise exc

    good = _finding("b.py", 1, "good")
    monkeypatch.setattr(an, "ANALYZERS", {
        "bad": _spec("bad", (".js",), boom),
        "ok": _spec("ok", (".py",), lambda ctx: (good,)),
    })
    run = run_analyzers((), {"a.js": "x", "b.py": "y"}, ("bad", "ok"))
    assert run.skipped == (("bad", reason),)
    assert run.ran == ("ok",) and run.findings == (good,)


def test_engines_share_one_time_budget(monkeypatch, which_all):
    now = [0.0]
    timeouts: list[float] = []

    def slow(ctx):
        timeouts.append(ctx.timeout_s)
        now[0] += 80.0
        return ()

    monkeypatch.setattr(an, "ANALYZERS", {
        "a": _spec("a", (".py",), slow, timeout_s=60.0),
        "b": _spec("b", (".py",), slow, timeout_s=60.0),
        "c": _spec("c", (".py",), slow, timeout_s=60.0),
    })
    run = run_analyzers((), {"x.py": ""}, ("a", "b", "c"),
                        budget_s=100.0, clock=lambda: now[0])
    # a gets its own cap; b gets what is left of the budget; c never starts.
    assert timeouts == [60.0, 20.0]
    assert run.ran == ("a", "b") and run.skipped == (("c", "budget"),)


def test_findings_per_engine_are_capped(monkeypatch, which_all):
    many = tuple(_finding("a.py", i, "r") for i in range(1, 50))
    monkeypatch.setattr(an, "ANALYZERS", {"a": _spec("a", (".py",), lambda c: many)})
    run = run_analyzers((), {"a.py": ""}, ("a",))
    assert len(run.findings) == an._MAX_FINDINGS_PER_ENGINE


def test_ruff_engine_runs_even_when_the_legacy_switch_is_off(monkeypatch, which_all):
    monkeypatch.setattr(lint_mod, "_ENABLED", False)
    calls = []

    def fake_run(argv, **kw):
        calls.append(kw)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    with patch("subprocess.run", side_effect=fake_run):
        run = run_analyzers((_hunk("a.py"),), {"a.py": "x = 1\n"}, ("ruff",))
    assert run.ran == ("ruff",) and len(calls) == 1
    assert calls[0]["timeout"] <= lint_mod._TIMEOUT_S


def test_ruff_error_is_a_recorded_skip_not_a_clean_run(monkeypatch, which_all):
    proc = subprocess.CompletedProcess(["ruff"], 2, "", "bad config")
    with patch("subprocess.run", return_value=proc):
        run = run_analyzers((_hunk("a.py"),), {"a.py": "x = 1\n"}, ("ruff",))
    assert run.ran == () and run.skipped == (("ruff", "error"),)


def test_scan_ruff_best_effort_callers_still_get_empty_on_error():
    proc = subprocess.CompletedProcess(["ruff"], 2, "", "bad config")
    with patch("subprocess.run", return_value=proc):
        assert lint_mod.scan_ruff((_hunk("a.py"),), {"a.py": "x\n"}, enabled=True) == ()
        with pytest.raises(AnalyzerSkip, match="error"):
            lint_mod.scan_ruff((_hunk("a.py"),), {"a.py": "x\n"},
                               enabled=True, strict=True)


# --- opengrep output -> attributed, diff-anchored markings -----------------

def _og(path, line, rule="elder-js-dynamic-eval", severity="WARNING", msg="Dynamic code."):
    return {"path": path, "start": {"line": line}, "check_id": rule,
            "extra": {"message": msg, "severity": severity}}


def test_opengrep_results_are_anchored_to_added_lines():
    data = {"results": [_og("./src/a.js", 2), _og("src/a.js", 9), _og("b.js", 1)]}
    found = map_opengrep_results(data, {"src/a.js": {1, 2}})
    assert [(f.file, f.line) for f in found] == [("src/a.js", 2)]


def test_opengrep_marking_names_engine_and_rule():
    (f,) = map_opengrep_results({"results": [_og("a.js", 1)]}, {"a.js": {1}})
    assert f.rule_name == "elder-js-dynamic-eval"
    assert f.message.startswith("opengrep `elder-js-dynamic-eval`: Dynamic code.")
    assert "nosemgrep: elder-js-dynamic-eval" in f.message


def test_opengrep_markings_are_advisory_only():
    data = {"results": [_og("a.js", 1, severity="ERROR"),
                        _og("a.js", 2, rule="x.y", severity="INFO")]}
    found = map_opengrep_results(data, {"a.js": {1, 2}})
    assert [f.severity for f in found] == ["medium", "low"]
    # A dotted engine id still round-trips through the grug-rule marker charset.
    assert found[1].rule_name == "x-y"


def test_malformed_opengrep_rows_are_skipped():
    data = {"results": [None, {"path": "a.js"}, _og("a.js", "1"), _og("a.js", 1)]}
    assert len(map_opengrep_results(data, {"a.js": {1}})) == 1
    assert map_opengrep_results("nope", {}) == ()


# --- dedup against the model's own findings -------------------------------

def test_analyzer_finding_on_a_model_finding_line_folds_into_it():
    model = _finding("a.js", 4, "xss", "Model sees XSS.", origins=_MODEL)
    measured = _finding("a.js", 4, "elder-js-unescaped-html-sink",
                        "opengrep `elder-js-unescaped-html-sink`: Unescaped HTML sink.")
    existing, extra = merge_analyzer_findings((model,), (measured,))
    assert extra == ()
    (merged,) = existing
    assert merged.rule_name == "xss" and merged.origins == _MODEL
    assert merged.message.endswith(
        "Also measured by opengrep `elder-js-unescaped-html-sink`.")


def test_analyzer_finding_elsewhere_is_added():
    model = _finding("a.js", 4, "xss", origins=_MODEL)
    measured = _finding("a.js", 5, "elder-js-dynamic-eval", "opengrep `x`: y")
    existing, extra = merge_analyzer_findings((model,), (measured,))
    assert existing == (model,) and extra == (measured,)


def test_deterministic_findings_do_not_absorb_analyzer_findings():
    complexity = _finding("a.py", 3, "high-complexity")  # no model origins
    ruff = _finding("a.py", 3, "lint-bug", "ruff `B006`: mutable default")
    existing, extra = merge_analyzer_findings((complexity,), (ruff,))
    assert existing == (complexity,) and extra == (ruff,)


def test_analyzer_duplicates_collapse():
    f = _finding("a.py", 3, "lint-bug", "ruff `B006`: x")
    assert merge_analyzer_findings((), (f, f))[1] == (f,)


# --- the read-only sandbox -------------------------------------------------

def test_sandbox_env_carries_no_pod_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "not-for-children")
    monkeypatch.setenv("GRUG_DATABASE_URL", "postgresql://x")
    env = sandbox_env(str(tmp_path))
    assert "GITHUB_APP_PRIVATE_KEY" not in env and "GRUG_DATABASE_URL" not in env
    assert env["HOME"] == str(tmp_path) and env["PATH"]


def test_read_only_tree_blocks_writes_and_restores(tmp_path):
    (tmp_path / "pkg").mkdir()
    target = tmp_path / "pkg" / "a.py"
    target.write_text("x = 1\n")
    with read_only_tree(str(tmp_path)):
        assert not os.stat(target).st_mode & stat.S_IWUSR
        assert not os.stat(tmp_path / "pkg").st_mode & stat.S_IWUSR
    target.write_text("y = 2\n")  # writable again so cleanup works


def test_ruff_runs_in_the_sandbox(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "not-for-children")
    seen = {}

    def fake_run(argv, **kw):
        scanned = argv[-1]
        seen["env"] = kw["env"]
        seen["writable"] = [
            os.stat(os.path.join(d, n)).st_mode & stat.S_IWUSR
            for d, _, names in os.walk(scanned) for n in names
        ]
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    with patch("subprocess.run", side_effect=fake_run):
        lint_mod.scan_ruff((_hunk("a.py"),), {"a.py": "x\n"}, enabled=True)
    assert "GITHUB_APP_PRIVATE_KEY" not in seen["env"]
    assert seen["writable"] == [0]


def test_opengrep_runs_read_only_with_grug_rules_and_the_repo_ignore_file(
    monkeypatch, tmp_path, which_all,
):
    rules = tmp_path / "rules"
    rules.mkdir()
    monkeypatch.setattr(an, "_ELDER_RULES_DIR", str(rules))
    monkeypatch.delenv("GRUG_SAST_CACHE_DIR", raising=False)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "not-for-children")
    seen = {}

    def fake_run(argv, **kw):
        root = kw["cwd"]
        seen["argv"], seen["env"] = argv, kw["env"]
        seen["ignore"] = Path(root, ".semgrepignore").read_text()
        seen["files"] = sorted(
            os.path.relpath(os.path.join(d, n), root)
            for d, _, names in os.walk(root) for n in names
        )
        seen["writable"] = os.stat(Path(root, "src", "a.js")).st_mode & stat.S_IWUSR
        seen["home_outside"] = not kw["env"]["HOME"].startswith(root)
        return subprocess.CompletedProcess(argv, 0, '{"results": [' + (
            '{"path": "src/a.js", "start": {"line": 1}, "check_id": "elder-js-dynamic-eval",'
            ' "extra": {"message": "Dynamic.", "severity": "WARNING"}}]}'), "")

    with patch("subprocess.run", side_effect=fake_run):
        run = run_analyzers(
            (_hunk("src/a.js"),), {"src/a.js": "eval(x)\n", "b.py": "x\n"},
            ("opengrep",), RepoAnalyzerConfig(semgrepignore="vendor/\n"),
        )
    assert seen["argv"][:4] == ["opengrep", "scan", "--config", str(rules)]
    assert seen["files"] == [".semgrepignore", "src/a.js"]
    assert seen["ignore"] == "vendor/\n"
    assert seen["writable"] == 0 and seen["home_outside"]
    assert "GITHUB_APP_PRIVATE_KEY" not in seen["env"]
    assert run.ran == ("opengrep",) and [f.line for f in run.findings] == [1]


def test_opengrep_nonzero_exit_is_a_skip(monkeypatch, tmp_path, which_all):
    monkeypatch.setattr(an, "_ELDER_RULES_DIR", str(tmp_path))
    proc = subprocess.CompletedProcess(["opengrep"], 2, "", "invalid config")
    with patch("subprocess.run", return_value=proc):
        run = run_analyzers((_hunk("a.ts"),), {"a.ts": "x"}, ("opengrep",))
    assert run.skipped == (("opengrep", "error"),)


def test_opengrep_without_rules_is_a_skip(monkeypatch, tmp_path, which_all):
    monkeypatch.setattr(an, "_ELDER_RULES_DIR", str(tmp_path / "gone"))
    run = run_analyzers((_hunk("a.ts"),), {"a.ts": "x"}, ("opengrep",))
    assert run.skipped == (("opengrep", "rules-missing"),)


# --- dispatch wiring --------------------------------------------------------

def test_dispatch_folds_analyzer_findings_and_reports_skips(monkeypatch):
    from personas.code_reviewer import dispatch

    model = _finding("a.js", 1, "xss", "Model.", origins=_MODEL)
    evaluation = CodeReviewEvaluation(findings=(model,), conclusion="success")
    measured = (_finding("a.js", 1, "elder-js-unescaped-html-sink", "opengrep `s`: x"),
                _finding("a.js", 2, "elder-js-dynamic-eval", "opengrep `e`: y"))
    monkeypatch.setattr(dispatch, "run_analyzers", lambda *a, **k: an.AnalyzerRun(
        findings=measured, ran=("opengrep",), skipped=(("ruff", "missing"),)))
    skipped: list = []
    out = dispatch._merge_lint_evidence(
        evaluation, (), {}, engines=("ruff", "opengrep"), skipped=skipped)
    assert [f.rule_name for f in out.findings] == ["xss", "elder-js-dynamic-eval"]
    assert "Also measured by opengrep `s`." in out.findings[0].message
    assert skipped == [("ruff", "missing")]
    assert "ruff (missing)" in dispatch._analyzer_skip_note(skipped)
    assert dispatch._analyzer_skip_note([]) == ""


def test_dispatch_with_no_engines_changes_nothing(monkeypatch):
    from personas.code_reviewer import dispatch

    evaluation = CodeReviewEvaluation(findings=(), conclusion="success")
    monkeypatch.setattr(dispatch, "run_analyzers", lambda *a, **k: 1 / 0)
    assert dispatch._merge_lint_evidence(evaluation, (), {}, engines=()) is evaluation


def test_repo_semgrepignore_is_read_at_the_base(monkeypatch):
    from personas.code_reviewer import dispatch

    calls = []

    def fake_fetch(token, owner, repo, paths, ref):
        calls.append((paths, ref))
        return {".semgrepignore": "dist/\n"}

    monkeypatch.setattr(dispatch, "fetch_file_contents", fake_fetch)
    monkeypatch.setattr(dispatch, "with_install_token_retry", lambda i, fn: fn("t"))
    assert dispatch._load_repo_semgrepignore(1, "o", "r", "basesha") == "dist/\n"
    assert calls == [((".semgrepignore",), "basesha")]
    assert dispatch._load_repo_semgrepignore(1, "o", "r", "") is None


# --- production wiring --------------------------------------------------------

_K8S = Path(__file__).resolve().parents[3] / "k8s"


@pytest.mark.parametrize("manifest", ["webhook-deployment.yaml", "consumer-deployment.yaml"])
def test_both_review_workloads_configure_the_analyzer_set(manifest):
    docs = [d for d in yaml.safe_load_all((_K8S / manifest).read_text()) if d]
    (deploy,) = [d for d in docs if d.get("kind") == "Deployment"]
    (container,) = deploy["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in container.get("env", [])}
    assert configured_engines(env.get("GRUG_ELDER_ANALYZERS", ""), "") == (
        "ruff", "opengrep")
