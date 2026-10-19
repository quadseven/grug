"""The workflow-hygiene gate's `paths:` filter must reach every file the
linter reads (#1278).

check.workflow-hygiene.yml runs the public port of the fleet's hygiene
linter, pinned by SHA. The port reads three kinds of file, all under
`.github/` (its `main()`):

- `.github/workflows/*.yml` - lint_file and Rule 11,
- `.github/**/*.sh` - lint_file,
- `.github/actions/**/action.yml` - Rule 11 at the current pin, and the full
  lint_file once the port's own scope fix lands and the pin is bumped.

The filter used to list only `.github/workflows/**` and `.github/**/*.sh`, so
a PR whose whole diff was a composite action started no hygiene run at all:
not failing, not pending, no check row. The canonical gate had the same hole,
and its fix pinned the filter to the linter's scope with a test so the next
scope change could not silently reopen it. This is that test for this repo. The linter is fetched at CI time, not vendored, so its
scope is mirrored in LINTER_SCOPE below; widen both together.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "check.workflow-hygiene.yml"

# What the pinned port's main() reads, as GitHub filter globs, each with a
# synthetic path inside it so a tree that is empty today is still checked.
LINTER_SCOPE: dict[str, str] = {
    ".github/workflows/*.yml": ".github/workflows/probe.yml",
    ".github/**/*.sh": ".github/scripts/nested/probe.sh",
    ".github/actions/**/action.yml": ".github/actions/probe/action.yml",
}

# The filter before #1278. The falsification test asserts the coverage check
# rejects it, naming exactly the composite actions.
PRE_1278_FILTER = [".github/workflows/**", ".github/**/*.sh"]


def _glob_re(glob: str) -> re.Pattern[str]:
    """A GitHub `paths:` glob as a regex: `**` crosses `/`, `*` and `?` do not."""
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _pull_request_paths(text: str) -> list[str] | None:
    """`on.pull_request.paths`, or None when there is no filter (runs on every PR)."""
    doc = yaml.safe_load(text)
    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on:` key as True
    pr = (on or {}).get("pull_request") or {}
    return pr.get("paths")


def _linter_files() -> list[str]:
    """The files in this repo that the linter reads, repo-relative."""
    files: set[str] = set()
    for glob in LINTER_SCOPE:
        rx = _glob_re(glob)
        files.update(
            rel
            for p in (_ROOT / ".github").rglob("*")
            if p.is_file() and rx.match(rel := p.relative_to(_ROOT).as_posix())
        )
    return sorted(files)


def _uncovered(paths: list[str], filter_globs: list[str] | None) -> list[str]:
    if filter_globs is None:
        return []
    rxs = [_glob_re(g) for g in filter_globs]
    return [p for p in paths if not any(rx.match(p) for rx in rxs)]


def test_the_paths_filter_is_found():
    globs = _pull_request_paths(_WORKFLOW.read_text())
    assert globs, "could not read on.pull_request.paths from check.workflow-hygiene.yml"


def test_the_scope_finds_the_composite_actions():
    # A scope walk that matched nothing would make the coverage test vacuous.
    files = _linter_files()
    actions = [f for f in files if f.startswith(".github/actions/")]
    assert len(actions) >= 3, files
    assert any(f.startswith(".github/workflows/") for f in files), files


def test_every_file_the_linter_reads_can_start_the_gate():
    globs = _pull_request_paths(_WORKFLOW.read_text())
    probes = list(LINTER_SCOPE.values())
    missing = _uncovered(_linter_files() + probes, globs)
    assert missing == [], (
        "check.workflow-hygiene.yml cannot be started by a PR touching only "
        f"{missing}, which the linter reads (#1278). Add a covering glob under "
        "on.pull_request.paths, or drop the filter."
    )


def test_the_pre_1278_filter_leaves_the_composite_actions_uncovered():
    missing = _uncovered(_linter_files(), PRE_1278_FILTER)
    assert missing and all(
        m.startswith(".github/actions/") and m.endswith("/action.yml") for m in missing
    ), missing
    assert _uncovered([LINTER_SCOPE[".github/actions/**/action.yml"]], PRE_1278_FILTER)


def test_glob_semantics():
    assert _glob_re(".github/**").match(".github/actions/a/action.yml")
    assert _glob_re(".github/**/*.sh").match(".github/x.sh")
    assert _glob_re(".github/**/*.sh").match(".github/a/b/x.sh")
    assert not _glob_re(".github/workflows/*.yml").match(".github/workflows/a/b.yml")
    assert not _glob_re(".github/**").match(".githubx/a.yml")


def test_no_filter_covers_everything():
    assert _pull_request_paths("on:\n  pull_request:\n  workflow_dispatch:\n") is None
    assert _uncovered(_linter_files(), None) == []


def test_the_scanned_file_floor_is_reachable():
    # The lint step fails when the linter reports fewer files than the floor,
    # so a glob that stops matching cannot read clean. The floor must not
    # exceed what the pinned linter counts (workflows plus .sh), or the gate
    # can never pass; the message must quote the same number.
    text = _WORKFLOW.read_text()
    floors = {int(n) for n in re.findall(r'-lt (\d+) \]', text)}
    quoted = {int(n) for n in re.findall(r"expected at least (\d+) ", text)}
    assert len(floors) == 1 and floors == quoted, (floors, quoted)
    counted = len(list((_ROOT / ".github" / "workflows").glob("*.yml"))) + len(
        list((_ROOT / ".github").rglob("*.sh"))
    )
    assert floors.pop() <= counted
