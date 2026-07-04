"""Trial init phase 2: install test dependencies (#469, ADR-0013). WEBHOOK-ONLY.

Runs as the `deps` init container — with NO token and NO secrets. It needs
network (to download wheels) but there is no credential to steal here.

WHEEL-ONLY (`--only-binary=:all:`) on purpose: extracting a wheel does NOT run
author build code (no `setup.py` / PEP 517 backend execution), so even the
dependency-install step does not execute arbitrary code. A dependency published
only as an sdist is skipped; the test phase may then fail to import it, which
CONSERVATIVELY kills the affected mutants (we under-report survivors rather than
over-report). Best-effort throughout: a pip failure never fails the Job — the
worker still runs whatever imports resolve.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("grug.smasher.trial_deps")

_DEPS_DIRNAME = ".grug-deps"
_REQUIREMENTS = ("requirements.txt", "requirements-dev.txt", "requirements/dev.txt")


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    workspace = Path(os.getenv("GRUG_TRIAL_WORKSPACE", "/workspace"))
    repo_dir = workspace / "repo"
    deps_dir = workspace / _DEPS_DIRNAME
    deps_dir.mkdir(parents=True, exist_ok=True)

    installed_any = False
    for rel in _REQUIREMENTS:
        req = repo_dir / rel
        if req.is_file():
            installed_any = _pip_install(req, deps_dir) or installed_any

    if not installed_any:
        log.info("trial_deps_no_requirements_found")
    # Always exit 0 — deps are best-effort (see module docstring).
    return 0


def _pip_install(requirements: Path, target: Path) -> bool:
    """Wheel-only install of one requirements file into `target`. Returns True
    if pip exited 0. Never raises."""
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--only-binary=:all:",          # no author build-code execution
                "--no-input", "--disable-pip-version-check",
                "--target", str(target),
                "-r", str(requirements),
            ],
            timeout=180,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("trial_deps_install_error", extra={"kind": type(e).__name__})
        return False
    if proc.returncode != 0:
        log.info("trial_deps_install_nonzero", extra={"file": requirements.name})
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
