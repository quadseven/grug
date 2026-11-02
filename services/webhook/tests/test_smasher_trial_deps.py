"""Trial deps tests (#1580) - a PR's requirements file must not choose where
the prep pod downloads from. Option lines and direct references are dropped
before pip reads the file, and the index is pinned on the pip command line."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from personas.smasher import trial_deps
from personas.smasher.trial_deps import sanitize_requirements

_STEERING = (
    "requests==2.32.3\n"
    "--extra-index-url http://example.invalid/simple\n"
    "-i http://example.invalid/simple\n"
    "--index-url=http://example.invalid/simple\n"
    "-f http://example.invalid/wheels\n"
    "--find-links http://example.invalid/wheels\n"
    "--trusted-host example.invalid\n"
    "-r other.txt\n"
    "-c constraints.txt\n"
    "-e git+https://example.invalid/repo.git#egg=x\n"
    "pkg @ https://example.invalid/x.whl\n"
    "https://example.invalid/y.whl\n"
    "./local/pkg\n"
    "/abs/path/pkg.whl\n"
    "attrs>=23 ; python_version >= '3.9'\n"
)


def test_sanitize_drops_options_and_direct_references():
    out = sanitize_requirements(_STEERING)
    assert "example.invalid" not in out
    assert out.splitlines() == ["requests==2.32.3", "attrs>=23 ; python_version >= '3.9'"]


def test_sanitize_keeps_a_plain_pinned_file_unchanged():
    plain = "requests==2.32.3\nattrs>=23,<25\npytest[testing]~=8.3\n"
    assert sanitize_requirements(plain) == plain


@pytest.mark.parametrize(
    "line",
    [
        "python-dateutil==2.9.0.post0",
        "zope.interface>=6",
        "typing_extensions",
        "Django!=4.2.1,<5",
        "numpy==1.*",
        "uvicorn[standard]~=0.30",
        "pywin32>=306 ; sys_platform == 'win32'",
        "tomli ; python_version < '3.11'",
    ],
)
def test_sanitize_keeps_ordinary_requirement_lines_unchanged(line):
    assert sanitize_requirements(line + "\n") == line + "\n"


def test_sanitize_strips_comments_and_per_line_options():
    text = "# a comment\nrequests==2.32.3  # pinned\nidna==3.7 --hash=sha256:abc \\\n    --hash=sha256:def\n"
    assert sanitize_requirements(text).splitlines() == ["requests==2.32.3", "idna==3.7"]


def test_pip_argv_names_only_the_configured_index(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    req.write_text(_STEERING)
    monkeypatch.setenv("GRUG_TRIAL_INDEX_URL", "https://pypi.org/simple")
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        r_path = Path(argv[argv.index("-r") + 1])
        seen["filtered"] = r_path.read_text()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(trial_deps.subprocess, "run", fake_run)
    assert trial_deps._pip_install(req, tmp_path / "deps") is True

    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "example.invalid" not in " ".join(argv)
    assert "example.invalid" not in str(seen["filtered"])
    assert argv[argv.index("--index-url") + 1] == "https://pypi.org/simple"
    assert "--isolated" in argv
    # The PR's file is never handed to pip directly.
    assert argv[argv.index("-r") + 1] != str(req)


@pytest.mark.parametrize("text", ["", "# only comments\n", "--index-url http://example.invalid\n"])
def test_pip_not_called_when_nothing_survives(tmp_path, monkeypatch, text):
    req = tmp_path / "requirements.txt"
    req.write_text(text)

    def fake_run(argv, **kwargs):
        raise AssertionError("pip must not run with an empty requirement set")

    monkeypatch.setattr(trial_deps.subprocess, "run", fake_run)
    assert trial_deps._pip_install(req, tmp_path / "deps") is False
