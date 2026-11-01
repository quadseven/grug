"""grug#1574: a pull request's own files cannot steer or escape the analyzers.

The inventory is docs/reviewer-attack-surface.md. These tests hold its claims
for the two engines grug runs on PR files.

The stand-in engine below is deliberately the worst case: it obeys every config
file it can see and acts on every secret in its environment by connecting to a
local tripwire. So a test here passes only if grug gave it nothing to obey and
nothing to steal, whatever a real engine's config format can or cannot do. The
real engines then run against the same hostile tree, where CI has them.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import llm_client as lc
import pytest
from llm_client import Hunk, review_diff
from personas.code_reviewer import analyzers as an
from personas.code_reviewer import dispatch, sast
from personas.code_reviewer.analyzers import RepoAnalyzerConfig, run_analyzers
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.lint import ruff_hits
from personas.code_reviewer.sast import scan_opengrep
from review_evidence import assemble_evidence

_CANARY = "canary-value-from-the-pod-environment"


def _hunk(path: str) -> DiffHunk:
    return DiffHunk(
        file_path=path, new_start=1, new_lines=frozenset({1}),
        body="@@ -0,0 +1 @@\n+x = 1\n",
    )


def _hostile_tree(port: int, sentinel: Path) -> dict[str, str]:
    """The changed files of a PR that tries every config route at once. Each
    config names the tripwire, and each runnable file would leave a sentinel."""
    url = f"http://127.0.0.1:{port}/"
    runs = f"import pathlib; pathlib.Path({str(sentinel)!r}).touch()\n"
    return {
        "app.py": "x = 1\n",
        "app.js": "const x = 1;\n",
        "conftest.py": runs,
        "setup.py": runs,
        ".semgrepignore": f"# {url}\n*\n",
        ".opengrepignore": f"# {url}\n*\n",
        ".gitignore": f"# {url}\n*\n",
        "ruff.toml": f'# {url}\n[lint]\nignore = ["ALL"]\n',
        ".ruff.toml": f'# {url}\n[lint]\nignore = ["ALL"]\n',
        "pyproject.toml": f'# {url}\n[tool.ruff.lint]\nignore = ["ALL"]\n',
        ".pre-commit-config.yaml": f"# {url}\nrepos: []\n",
        "Makefile": f"all:\n\tcurl {url}\n",
        "nested/.semgrepignore": f"# {url}\n*\n",
        "nested/ruff.toml": f'# {url}\n[lint]\nignore = ["ALL"]\n',
        "nested/mod.py": "y = 2\n",
    }


# The configs each engine really would load from a directory it scans.
_OBEYED = {
    "opengrep": {".semgrepignore", ".opengrepignore", ".gitignore"},
    "ruff": {"ruff.toml", ".ruff.toml", "pyproject.toml"},
}
_ENGINE = """#!{python}
import json, os, re, socket, sys

NAMES = {names!r}
EVIDENCE = {evidence!r}
CANARY = {canary!r}
PORT = {port!r}


def phone(reason, seen):
    seen.append(reason)
    with socket.create_connection(("127.0.0.1", PORT), timeout=2):
        pass


seen = []
root = os.path.abspath(sys.argv[-1])
for key, value in os.environ.items():
    if CANARY in value:
        phone("env:" + key, seen)
isolated = "--isolated" in sys.argv
for dirpath, _dirs, files in os.walk(root):
    for name in files:
        if name in NAMES:
            phone("config:" + name, seen)
parent = os.path.dirname(root)
while not isolated and len(parent) > 1:
    for name in NAMES:
        if os.path.isfile(os.path.join(parent, name)):
            phone("ancestor:" + name, seen)
    parent = os.path.dirname(parent)
writable = []
for dirpath, _dirs, files in os.walk(root):
    for name in files:
        if os.access(os.path.join(dirpath, name), os.W_OK):
            writable.append(name)
with open(EVIDENCE, "a") as fh:
    json.dump({{"seen": seen, "writable": writable, "env": sorted(os.environ)}}, fh)
    fh.write("\\n")
print({empty!r})
"""


class _Bench:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tripwire = socket.socket()
        self.tripwire.bind(("127.0.0.1", 0))
        self.tripwire.listen(16)
        self.tripwire.setblocking(False)
        self.port = self.tripwire.getsockname()[1]
        self.sentinel = tmp_path / "executed"
        self.evidence = tmp_path / "evidence.jsonl"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        rules = tmp_path / "rules"
        rules.mkdir()
        monkeypatch.setattr(sast, "_RULES_DIR", str(rules))
        monkeypatch.setattr(an, "_ELDER_RULES_DIR", str(rules))
        monkeypatch.setattr(sast, "_OPENGREP_TIMEOUT_S", 30)
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        for name in ("GITHUB_APP_PRIVATE_KEY", "DATABASE_URL", "OPENROUTER_API_KEY"):
            monkeypatch.setenv(name, _CANARY)
        monkeypatch.delenv("GRUG_SAST_CACHE_DIR", raising=False)

    def stand_in(self, engine: str, empty: str) -> None:
        path = self.bin / engine
        path.write_text(_ENGINE.format(
            python=sys.executable, names=sorted(_OBEYED[engine]),
            evidence=str(self.evidence), canary=_CANARY, port=self.port, empty=empty,
        ))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def runs(self) -> list[dict]:
        return [json.loads(line) for line in self.evidence.read_text().splitlines()]

    def egress_attempts(self) -> int:
        count = 0
        while True:
            try:
                conn, _ = self.tripwire.accept()
            except BlockingIOError:
                return count
            conn.close()
            count += 1


@pytest.fixture
def bench(tmp_path, monkeypatch):
    made = _Bench(tmp_path, monkeypatch)
    yield made
    made.tripwire.close()


_OPENGREP_EMPTY = json.dumps({"results": [], "errors": [], "paths": {"scanned": []}})


def _assert_clean(bench: _Bench, runs: int) -> None:
    recorded = bench.runs()
    assert len(recorded) == runs, "the stand-in engine must actually have run"
    assert [run["seen"] for run in recorded] == [[]] * runs
    assert [run["writable"] for run in recorded] == [[]] * runs
    assert bench.egress_attempts() == 0
    assert not bench.sentinel.exists()


def test_the_security_scan_gives_a_hostile_pr_nothing_to_obey_or_steal(bench):
    bench.stand_in("opengrep", _OPENGREP_EMPTY)
    tree = _hostile_tree(bench.port, bench.sentinel)

    scan_opengrep((_hunk("app.py"),), tree)

    _assert_clean(bench, runs=1)


def test_the_analyzers_give_a_hostile_pr_nothing_to_obey_or_steal(bench):
    bench.stand_in("opengrep", _OPENGREP_EMPTY)
    bench.stand_in("ruff", "[]")
    tree = _hostile_tree(bench.port, bench.sentinel)

    hunks = (_hunk("app.py"), _hunk("app.js"))
    run = run_analyzers(hunks, tree, ("ruff", "opengrep"))

    assert run.ran == ("ruff", "opengrep"), run.skipped
    _assert_clean(bench, runs=2)


def test_the_base_ignore_list_is_the_only_one_the_engine_sees(bench, monkeypatch):
    """A PR that adds an ignore list where the base has none must not get it
    honored, and a repo's own base list still applies."""
    seen: list[str] = []
    real_run = an.subprocess.run

    def spy(argv, **kwargs):
        ignore = Path(kwargs["cwd"]) / ".semgrepignore"
        seen.append(ignore.read_text() if ignore.exists() else "")
        return real_run(argv, **kwargs)

    bench.stand_in("opengrep", _OPENGREP_EMPTY)
    tree = {"app.js": "const x = 1;\n", ".semgrepignore": "*\n"}
    monkeypatch.setattr(an.subprocess, "run", spy)
    run_analyzers((_hunk("app.js"),), tree, ("opengrep",))
    run_analyzers((_hunk("app.js"),), tree, ("opengrep",),
                  RepoAnalyzerConfig(semgrepignore="vendor/\n"))

    assert seen == ["", "vendor/\n"]


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")
def test_real_ruff_ignores_a_config_above_the_scan_directory(tmp_path, monkeypatch):
    """Ruff walks up from what it scans, so a config beside the temp directory
    would apply. The check is the real engine, on a real finding."""
    (tmp_path / "ruff.toml").write_text('[lint]\nper-file-ignores = {"*" = ["ALL"]}\n')
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    hits = ruff_hits({"a.py": "import pickle\npickle.loads(b'x')\n"})

    assert {hit.code for hit in hits} & {"S301", "S403"}


@pytest.mark.skipif(shutil.which("opengrep") is None, reason="opengrep not on PATH")
def test_real_opengrep_runs_scrubbed_and_still_finds(monkeypatch):
    """The real engine starts on the scrubbed environment and read-only tree,
    and a PR's own ignore list does not hide its finding."""
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _CANARY)
    source = "import subprocess\nsubprocess.call('x', shell=True)\n"
    files = {"app.py": source, ".semgrepignore": "*\n"}
    hunk = DiffHunk(
        file_path="app.py", new_start=1, new_lines=frozenset({1, 2}),
        body="@@ -0,0 +1,2 @@\n",
    )

    assert scan_opengrep((hunk,), files)


_HEAD, _BASE = "h" * 40, "b" * 40
_REQUEST = httpx.Request("GET", "https://api.github.com/repos/o/r/contents/f")


def test_guideline_files_steer_a_review_from_the_base_commit_only(monkeypatch):
    """The base commit answers with one rule and the PR head with another, and
    only the base rule may reach the system prompt."""
    monkeypatch.setattr("github_app_auth.get_install_token", lambda *a, **k: "t")
    monkeypatch.setattr(lc, "_guideline_cache", lc.RepoFileCache(4))
    for loader in ("_load_poolside_key", "_load_openrouter_key", "_load_opencode_go_key"):
        monkeypatch.setattr(lc, loader, lambda: "test-key")
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda _s: None)
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    refs: set[str] = set()
    systems: list[str] = []

    def get(url, params=None, headers=None, timeout=None):
        refs.add(params["ref"])
        if url.endswith("/CLAUDE.md"):
            rule = "rule-from-base" if params["ref"] == _BASE else "rule-from-head"
            return httpx.Response(200, text=rule, request=_REQUEST)
        return httpx.Response(404, request=_REQUEST)

    def post(_url, **kwargs):
        systems.append(kwargs["json"]["messages"][0]["content"])
        return httpx.Response(200, json={
            "choices": [{"message": {
                "role": "assistant", "content": json.dumps({"findings": []}),
            }}],
            "model": "test-model-id",
        })

    context = {"repo": "o/r", "installation_id": 1, "head_sha": _HEAD, "base_sha": _BASE}
    hunks = [Hunk(path="a.py", body="@@ -1 +1 @@\n+x = 1\n")]
    with patch.object(httpx, "get", side_effect=get), \
            patch.object(httpx, "post", side_effect=post):
        review_diff(assemble_evidence(hunks, pr_context=context), installation_id=1)

    assert refs == {_BASE}
    assert systems
    assert all("rule-from-base" in system for system in systems)
    assert not any("rule-from-head" in system for system in systems)


def test_repo_docs_are_read_from_the_base_commit(monkeypatch):
    refs: list[str] = []

    def fetch(_token, _owner, _repo, paths, ref):
        refs.append(ref)
        return {path: "glossary" for path in paths}

    def listing(_token, _owner, _repo, _path, ref):
        refs.append(ref)
        return []

    monkeypatch.setattr(dispatch, "fetch_file_contents", fetch)
    monkeypatch.setattr(dispatch, "_list_dir_contents", listing)

    context = dispatch._load_repo_docs("t", "o", "r", _BASE, (_hunk("app.py"),))

    assert context and "glossary" in context
    assert set(refs) == {_BASE}
