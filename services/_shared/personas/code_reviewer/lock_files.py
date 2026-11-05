"""Lock-file-only pull requests (#1649).

Lock files are dropped from the model's plate (#609), so a diff made only of
them reached the model with nothing and published a neutral "eyes clouded
(no_diff)" that never clears a blocking gate. A lock file is not harmless
data, though: a changed `resolved` URL can point an install at another
registry. So a lock-file-only diff concludes success only when all three
hold, and stays neutral otherwise:

1. every changed file's name is on `LOCK_FILE_NAMES`;
2. the head branch is in the base repository and its author is trusted
   (the caller decides with grug's existing trust checks);
3. every URL host on an added lock line is on `ALLOWED_HOSTS`, and every
   changed lock file that is not a deletion has a text hunk to scan.

Pure: no I/O.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from personas.code_reviewer.diff_parser import DiffHunk, FileChange, added_lines

LOCK_FILE_NAMES: frozenset[str] = frozenset({
    "uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json",
    "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "go.sum", "Gemfile.lock",
    ".terraform.lock.hcl", "composer.lock",
})

ALLOWED_HOSTS: frozenset[str] = frozenset({
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
    "registry.yarnpkg.com", "crates.io", "static.crates.io",
    "proxy.golang.org", "sum.golang.org", "rubygems.org",
    "registry.terraform.io", "releases.hashicorp.com", "github.com",
})

# A URL with any scheme (`https://`, `git+ssh://`, `registry+https://`), and
# git's scp-like `git@host:path` form, which has no scheme at all.
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>,()\[\]{}]+")
_SCP_RE = re.compile(r"(?<![\w.-])git@([^\s:/\"']+):")

LOCK_FILES_ONLY_TITLE = "lock files only: nothing for Elder to review"


def is_lock_file_path(path: str) -> bool:
    """True when the file name is exactly one of `LOCK_FILE_NAMES`."""
    return posixpath.basename(path) in LOCK_FILE_NAMES


def line_hosts(line: str) -> tuple[str, ...]:
    """Every URL host a line names, lower-cased, in order. A URL whose host
    cannot be read yields the URL text itself, which is never allowed."""
    out: list[str] = []
    for m in _URL_RE.finditer(line):
        try:
            host = urlsplit(m.group(0)).hostname
        except ValueError:
            host = None
        out.append(host.lower() if host else m.group(0))
    out.extend(m.group(1).lower() for m in _SCP_RE.finditer(line))
    return tuple(out)


@dataclass(frozen=True)
class LockOnlyVerdict:
    """`applies` is False when some changed file is not a lock file: the
    normal review path runs and nothing here is said. Otherwise `passed`
    says whether the diff concludes success, and `note` explains either
    outcome for the check summary."""

    applies: bool
    passed: bool = False
    paths: tuple[str, ...] = ()
    note: str = ""


def _shown(items: tuple[str, ...]) -> str:
    head = ", ".join(f"`{i.replace(chr(96), '')}`" for i in items[:10])
    return head + (f" and {len(items) - 10} more" if len(items) > 10 else "")


def judge_lock_only(
    changes: tuple[FileChange, ...], hunks: tuple[DiffHunk, ...], *,
    trusted: bool,
) -> LockOnlyVerdict:
    """Decide a lock-file-only diff. Every condition must hold to pass."""
    paths = tuple(dict.fromkeys(
        p for c in changes for p in (c.path, c.previous_path) if p
    ))
    if not paths or not all(is_lock_file_path(p) for p in paths):
        return LockOnlyVerdict(applies=False)
    lead = f"\n\nLock-file-only diff: {len(paths)} lock file(s) changed ({_shown(paths)}). "
    if not trusted:
        return LockOnlyVerdict(True, False, paths, lead + (
            "The head is not in this repository or its author is not a "
            "trusted collaborator or known App, so this stays neutral."
        ))
    with_hunks = {h.file_path for h in hunks}
    unscanned = tuple(
        c.path for c in changes if c.status != "removed" and c.path not in with_hunks
    )
    if unscanned:
        return LockOnlyVerdict(True, False, paths, lead + (
            f"Grug could not scan {_shown(unscanned)} (no text hunk), so "
            "this stays neutral."
        ))
    hosts = tuple(dict.fromkeys(
        host
        for h in hunks
        for _, text in added_lines(h)
        for host in line_hosts(text)
        if host not in ALLOWED_HOSTS
    ))
    if hosts:
        return LockOnlyVerdict(True, False, paths, lead + (
            f"Added lock lines name a host not on the registry allowlist: "
            f"{_shown(hosts)}. This stays neutral for a person to check."
        ))
    return LockOnlyVerdict(True, True, paths, lead + (
        "Every added URL points at an allowed registry host and the author "
        "is trusted. Elder does not review lock file contents, and no other "
        "file changed, so there is nothing for Elder to review."
    ))
