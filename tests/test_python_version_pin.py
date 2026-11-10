"""Pin check: .python-version and services/Dockerfile agree on the Python minor."""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PYTHON_VERSION = _REPO / ".python-version"
_DOCKERFILE = _REPO / "services" / "Dockerfile"


def _pinned_minor() -> str:
    return _PYTHON_VERSION.read_text().strip()


def _dockerfile_minor() -> str:
    for line in _DOCKERFILE.read_text().splitlines():
        match = re.match(r"FROM\s+python:(\d+\.\d+)", line.strip())
        if match:
            return match.group(1)
    raise AssertionError("no 'FROM python:X.Y' line in services/Dockerfile")


def test_python_version_matches_dockerfile_minor():
    assert _pinned_minor() == _dockerfile_minor(), (
        f".python-version ({_pinned_minor()!r}) disagrees with "
        f"services/Dockerfile FROM python: ({_dockerfile_minor()!r})"
    )
