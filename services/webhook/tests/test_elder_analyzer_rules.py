"""Tests for Elder's own analyzer rule pack (elder_analyzer_rules/, #1254).

Same two layers as test_sast_rules.py: structural checks that always run, and
real-engine checks against planted code, which run whenever opengrep is on
PATH (CI installs it, and `make webhook-test` refuses to run in CI without
it). The engine checks go through `run_analyzers`, so they also prove the
whole path end to end: read-only tree, scrubbed env, the repo's
`.semgrepignore` and `nosemgrep` comments.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

import personas.code_reviewer.analyzers as an
from personas.code_reviewer.analyzers import RepoAnalyzerConfig, run_analyzers
from personas.code_reviewer.diff_parser import DiffHunk

RULES_DIR = Path(__file__).resolve().parents[1] / "elder_analyzer_rules"
ENGINE = shutil.which("opengrep")


def _rules() -> list[dict]:
    out: list[dict] = []
    for f in sorted(RULES_DIR.glob("*.yml")):
        out.extend((yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("rules", []))
    return out


def test_rule_ids_are_unique_and_namespaced():
    ids = [r["id"] for r in _rules()]
    assert ids and len(ids) == len(set(ids))
    assert all(i.startswith("elder-") for i in ids)


def test_every_rule_reads_both_javascript_and_typescript():
    for rule in _rules():
        assert set(rule["languages"]) == {"javascript", "typescript"}, rule["id"]


def test_rule_files_are_ascii():
    for f in RULES_DIR.glob("*.yml"):
        f.read_text(encoding="utf-8").encode("ascii")


_JS = """const el = document.getElementById("x");
el.innerHTML = userInput;
el.innerHTML = "<b>static</b>";
el.insertAdjacentHTML("beforeend", html);
document.write(stuff);
eval(code);
const f = new Function("a", "return a");
setTimeout("doIt()", 10);
setTimeout(doIt, 10);
el.innerHTML = trusted; // nosemgrep: elder-js-unescaped-html-sink
"""

_TSX = """export function A(props: {html: string}) {
  const el: HTMLElement = document.body;
  el.outerHTML = props.html;
  return <div dangerouslySetInnerHTML={{__html: props.html}} />;
}
export function B() {
  return <div dangerouslySetInnerHTML={{__html: "<b>x</b>"}} />;
}
"""


def _all_added(path: str, text: str) -> DiffHunk:
    n = len(text.splitlines())
    body = f"@@ -0,0 +1,{n} @@\n" + "".join(f"+{ln}\n" for ln in text.splitlines())
    return DiffHunk(file_path=path, new_start=1, new_lines=frozenset(), body=body)


@pytest.fixture
def engine(monkeypatch, tmp_path):
    if ENGINE is None:
        pytest.skip("opengrep not on PATH")
    monkeypatch.setattr(an, "_ELDER_RULES_DIR", str(RULES_DIR))
    monkeypatch.setenv("GRUG_SAST_CACHE_DIR", str(tmp_path / "cache"))


def _run(files: dict[str, str], ignore: str | None = None):
    hunks = tuple(_all_added(p, t) for p, t in files.items())
    return run_analyzers(hunks, files, ("opengrep",),
                         RepoAnalyzerConfig(semgrepignore=ignore))


def test_planted_js_and_tsx_cases_match_and_literals_do_not(engine):
    run = _run({"web/a.js": _JS, "web/b.tsx": _TSX})
    assert run.skipped == () and run.ran == ("opengrep",)
    got = sorted((f.file, f.line, f.rule_name) for f in run.findings)
    assert got == [
        ("web/a.js", 2, "elder-js-unescaped-html-sink"),
        ("web/a.js", 4, "elder-js-unescaped-html-sink"),
        ("web/a.js", 5, "elder-js-unescaped-html-sink"),
        ("web/a.js", 6, "elder-js-dynamic-eval"),
        ("web/a.js", 7, "elder-js-dynamic-eval"),
        ("web/a.js", 8, "elder-js-dynamic-eval"),
        ("web/b.tsx", 3, "elder-js-unescaped-html-sink"),
        ("web/b.tsx", 4, "elder-js-react-dangerous-html"),
    ]
    # Line 10 carries `nosemgrep`: the repo's own suppression wins.
    assert all(f.message.startswith("opengrep `elder-js-") for f in run.findings)


def test_repo_semgrepignore_wins(engine):
    run = _run({"web/a.js": _JS, "web/b.tsx": _TSX}, ignore="web/b.tsx\n")
    assert run.ran == ("opengrep",)
    assert {f.file for f in run.findings} == {"web/a.js"}
