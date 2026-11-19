"""The sandbox every reviewer analyzer subprocess runs in (#1254, #1576).

An analyzer is a parser, never a runner: it reads the changed files and
reports what it sees. Four properties keep it that way, and every analyzer
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
4. A KERNEL BOUNDARY. The runtime image routes every engine through Landlock
   and seccomp before it sees any PR input. The executable can read its input
   and runtime files, write to private scratch, and make no network connections.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass

log = logging.getLogger("grug.code_reviewer.analyzer_sandbox")

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


# The reason an analyzer skip carries when the kernel boundary cannot start.
SANDBOX_UNAVAILABLE = "sandbox unavailable"

# What the launcher prints (and exits 2 with) when it cannot install a kernel
# restriction. Landlock calls are named landlock_*; the other two are the
# no-new-privs prctl and the seccomp filter load.
_LAUNCHER_FAILURES = ("landlock_", "PR_SET_NO_NEW_PRIVS", "could not install analyzer seccomp")


class SandboxUnavailable(AnalyzerSkip):
    """The launcher could not install Landlock or seccomp on this kernel.

    Fail closed on purpose (#1576): the analyzer is NOT run without the
    boundary. It is an `AnalyzerSkip`, so the runner records a skip, but the
    reason names the sandbox instead of a bare "error"."""


def _launcher_failure(proc: subprocess.CompletedProcess[str]) -> str | None:
    """The launcher's own failure line, or None when the engine ran (or failed
    for its own reasons, which are not the sandbox's)."""
    if proc.returncode != 2:
        return None
    for line in (proc.stderr or "").splitlines():
        if line.startswith(_LAUNCHER_FAILURES):
            return line.strip()[:200]
    return None


def _launcher_path() -> str | None:
    launcher = os.getenv("GRUG_ANALYZER_SANDBOX")
    if not launcher and os.getenv("KUBERNETES_SERVICE_HOST"):
        launcher = "/usr/local/bin/grug-analyzer-sandbox"
    return launcher or None


@dataclass(frozen=True)
class SandboxState:
    """Whether the kernel boundary works on this host.

    `required` is False where no launcher is configured (source test runs):
    nothing to probe, and `available` is then True."""
    required: bool
    available: bool
    detail: str = ""


_state_lock = threading.Lock()
_state: SandboxState | None = None


def probe_sandbox() -> SandboxState:
    """Start the launcher on a harmless binary and report whether it worked.

    Never raises. A kernel property does not change while the process lives,
    so callers use `sandbox_state()`, which caches this."""
    launcher = _launcher_path()
    if not launcher:
        return SandboxState(required=False, available=True, detail="no launcher configured")
    probe_bin = shutil.which("true") or sys.executable
    try:
        with tempfile.TemporaryDirectory(prefix="grug-sandbox-probe-") as tmp:
            scan, scratch = os.path.join(tmp, "scan"), os.path.join(tmp, "scratch")
            os.makedirs(scan)
            os.makedirs(scratch)
            args = [] if probe_bin != sys.executable else ["-c", "pass"]
            proc = subprocess.run(
                [launcher, scan, scratch, "-", os.path.abspath(probe_bin), *args],
                env={"PATH": os.environ.get("PATH", _DEFAULT_PATH)},
                stdin=subprocess.DEVNULL, close_fds=True, capture_output=True,
                text=True, check=False, timeout=15,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return SandboxState(True, False, f"launcher did not run: {type(exc).__name__}")
    if proc.returncode == 0:
        return SandboxState(required=True, available=True, detail="ok")
    detail = _launcher_failure(proc) or (
        f"probe exited {proc.returncode}: " + (proc.stderr or "").strip()[:150])
    return SandboxState(required=True, available=False, detail=detail)


def sandbox_state(*, refresh: bool = False) -> SandboxState:
    """The cached probe result. The first call logs one line: INFO when the
    boundary works, ERROR when it cannot start (every analyzer will then be
    skipped, so this must not go unnoticed)."""
    global _state
    with _state_lock:
        if _state is None or refresh:
            _state = probe_sandbox()
            if _state.required and not _state.available:
                log.error(
                    "analyzer_sandbox_unavailable: EVERY analyzer (ruff, opengrep) "
                    "will be skipped on this host until the kernel supports the "
                    "sandbox",
                    extra={"detail": _state.detail},
                )
            elif _state.required:
                log.info("analyzer_sandbox_ready")
        return _state


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


def run_analyzer(
    argv: list[str], *, scan_root: str, env: dict[str, str], timeout: float,
    rules_dir: str | None = None, cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an engine, requiring the OS launcher in the image and in Kubernetes."""
    launcher = _launcher_path()
    if not launcher:
        return subprocess.run(
            argv, cwd=cwd, env=env, timeout=timeout, capture_output=True,
            text=True, check=False,
        )

    engine = shutil.which(argv[0], path=env["PATH"])
    if not engine:
        raise FileNotFoundError(argv[0])
    with tempfile.TemporaryDirectory(prefix="grug-analyzer-scratch-") as scratch:
        child_env = dict(env, HOME=scratch, XDG_CACHE_HOME=scratch, TMPDIR=scratch)
        command = [
            launcher, scan_root, scratch, rules_dir or "-", os.path.abspath(engine), *argv[1:],
        ]
        proc = subprocess.run(
            command, cwd=cwd, env=child_env, timeout=timeout,
            stdin=subprocess.DEVNULL, close_fds=True, capture_output=True,
            text=True, check=False,
        )
    failure = _launcher_failure(proc)
    if failure:
        log.warning("analyzer_sandbox_launch_failed", extra={"detail": failure})
        raise SandboxUnavailable(SANDBOX_UNAVAILABLE)
    return proc


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
