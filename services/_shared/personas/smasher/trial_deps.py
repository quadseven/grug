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

INDEX-PINNED (#1580): the requirements file is PR text, and that format can
name another index, a find-links page, a trusted host, a nested file, or a
direct URL. Any of those would let the PR author choose which host the prep pod
connects to. So pip never reads the PR's file: `sanitize_requirements` keeps
only plain `name[extras] specifier ; marker` lines, the result goes to a temp
file, and pip runs `--isolated` with the index from `GRUG_TRIAL_INDEX_URL`.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("grug.smasher.trial_deps")

_DEPS_DIRNAME = ".grug-deps"
_REQUIREMENTS = ("requirements.txt", "requirements-dev.txt", "requirements/dev.txt")
_DEFAULT_INDEX_URL = "https://pypi.org/simple"

# A PEP 508 name, optional extras, a version specifier built only from
# operator/version characters (no `/`, `:` or `@`, so no URL or path fits), and
# an optional environment marker.
_PLAIN_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:\s*\[[A-Za-z0-9._,\s-]*\])?"
    r"(?:\s*[<>=!~][<>=!~0-9A-Za-z.*+,\s-]*)?"
    r"(?:\s*;.*)?$"
)
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz")
_INLINE_COMMENT = re.compile(r"(^|\s)#.*$")
_PER_LINE_OPTION = re.compile(r"\s+--?[A-Za-z]")


def sanitize_requirements(text: str) -> str:
    """PURE: keep only plain named requirements from a requirements file.

    Drops every option line (`-i`, `--index-url`, `--extra-index-url`, `-f`,
    `--find-links`, `--trusted-host`, `-r`, `-c`, `-e`, and any other `-`
    line), per-line options such as `--hash`, comments, and direct references
    (`name @ url`, bare URLs, paths and archive files). Kept lines are returned
    stripped, one per line, with a trailing newline; nothing kept gives "".
    """
    kept: list[str] = []
    for raw in text.replace("\\\r\n", " ").replace("\\\n", " ").splitlines():
        line = _INLINE_COMMENT.sub("", raw).strip()
        if not line or line.startswith("-"):
            continue
        line = _PER_LINE_OPTION.split(line, maxsplit=1)[0].strip()
        name_part = line.split(";", 1)[0]
        if "://" in line or "@" in name_part:
            continue
        if name_part.strip().lower().endswith(_ARCHIVE_SUFFIXES):
            continue
        if not _PLAIN_REQUIREMENT.match(line):
            continue
        kept.append(line)
    return "".join(f"{line}\n" for line in kept)


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
    """Wheel-only install of one requirements file into `target`, from the
    configured index only. Returns True if pip exited 0. Never raises."""
    try:
        filtered = sanitize_requirements(requirements.read_text(errors="replace"))
    except OSError as e:
        log.warning("trial_deps_read_error", extra={"kind": type(e).__name__})
        return False
    if not filtered:
        log.info("trial_deps_nothing_to_install", extra={"file": requirements.name})
        return False
    index_url = os.getenv("GRUG_TRIAL_INDEX_URL", _DEFAULT_INDEX_URL)
    # The root filesystem is read-only; the workspace volume (target's parent)
    # is the writable place for the filtered copy.
    target.parent.mkdir(parents=True, exist_ok=True)
    filtered_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", prefix=".grug-req-", dir=target.parent, delete=False
        ) as fh:
            fh.write(filtered)
            filtered_path = Path(fh.name)
        proc = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--isolated",                   # ignore PIP_* env and pip config files
                "--index-url", index_url,       # the only index; the file cannot add one
                "--only-binary=:all:",          # no author build-code execution
                "--no-input", "--disable-pip-version-check",
                "--target", str(target),
                "-r", str(filtered_path),
            ],
            timeout=180,
            capture_output=True,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("trial_deps_install_error", extra={"kind": type(e).__name__})
        return False
    finally:
        if filtered_path is not None:
            filtered_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        log.info("trial_deps_install_nonzero", extra={"file": requirements.name})
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
