"""The read-only sandbox every Elder analyzer subprocess runs in (#1254).

An analyzer is a parser, never a runner: it reads the changed files and
reports what it sees. Three properties keep it that way, and every analyzer
(ruff in lint.py, the engines in analyzers.py, the scan in sast.py) goes through this module to
get them:

1. NO CHECKOUT. The analyzer sees only a temp copy of the changed files'
   text, written by grug. There is no build file, no package manifest, no
   hook and no script of the repo's on disk for it to run, and grug never
   points an analyzer at a config the PR controls. `without_engine_config`
   holds that for a tree that takes every changed file: an engine loads these
   names from what it scans, so a PR's own copy never reaches the disk.
2. A READ-ONLY TREE. `read_only_tree` drops write permission on the copied
   files and directories for the length of the run, so an analyzer (or a
   fix mode switched on by mistake) cannot rewrite what it scans.
3. A SCRUBBED ENVIRONMENT. `sandbox_env` passes an allowlist, not the pod's
   environment: the GitHub App key, the database URL and every provider
   token stay out of the child process entirely.
"""
from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator

# Only what a statically linked or bundled-interpreter binary needs to start.
# PATH finds the binary; the locale keys stop opengrep's bundled Python from
# falling back to ASCII (see services/Dockerfile).
_PASSTHROUGH = ("PATH",)
_FIXED = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONUTF8": "1"}
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"

_READ_ONLY_FILE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_READ_ONLY_DIR = _READ_ONLY_FILE | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
_WRITABLE_FILE = _READ_ONLY_FILE | stat.S_IWUSR
_WRITABLE_DIR = _READ_ONLY_DIR | stat.S_IWUSR


# Files opengrep reads as settings from the tree it scans, at any depth.
_ENGINE_CONFIG_NAMES = frozenset({".semgrepignore", ".opengrepignore", ".gitignore"})


def without_engine_config(files: dict[str, str]) -> dict[str, str]:
    """The changed files minus any the engine would obey instead of scan."""
    return {path: text for path, text in files.items()
            if os.path.basename(path) not in _ENGINE_CONFIG_NAMES}


class AnalyzerSkip(Exception):
    """An analyzer could not produce a result; the message is the reason
    (missing, timeout, error, unparseable, ...). The runner records it as a
    skip and the review goes on."""


def sandbox_env(home: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The whole environment of an analyzer subprocess.

    `home` is the engine's HOME. An engine that writes there (opengrep keeps
    a cache) must get one OUTSIDE the scanned tree. `extra` is for grug-chosen values only, such as
    XDG_CACHE_HOME; never pass anything read from the repo under review."""
    env = {key: os.environ[key] for key in _PASSTHROUGH if os.environ.get(key)}
    env.setdefault("PATH", _DEFAULT_PATH)
    env.update(_FIXED)
    env["HOME"] = home
    env.update(extra or {})
    return env


def _chmod_tree(root: str, file_mode: int, dir_mode: int) -> None:
    os.chmod(root, dir_mode)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), dir_mode)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), file_mode)


@contextlib.contextmanager
def read_only_tree(root: str) -> Iterator[None]:
    """Make `root` and everything under it read-only while the body runs,
    then make it writable again so the temp directory can be removed."""
    _chmod_tree(root, _READ_ONLY_FILE, _READ_ONLY_DIR)
    try:
        yield
    finally:
        _chmod_tree(root, _WRITABLE_FILE, _WRITABLE_DIR)
