"""Supply-chain pins for what the runtime image and CI download (#1334).

The runtime image is the review bot itself and holds the GitHub App
credentials, so every binary it fetches at build time must be pinned to its
bytes, not just to a name:

- A GitHub release asset can be replaced after it is published, so a
  `releases/download` fetch pinned only by version is a pin on a name. Every
  such fetch, in services/Dockerfile and in any workflow, must be followed by
  `sha256sum -c` in the same RUN / run: block.
- A git tag can be moved, so the signing-helper source clone must be checked
  against a pinned 40-hex commit after the clone.
- CI installs the same opengrep the image ships (check.python.yml), so the two
  must agree on the version and the x86 digest - bump them together.

Parses the REAL files (same pattern as test_datadog_switch's Dockerfile
checks), so a cleanup that drops a check fails here, not in a review.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_DOCKERFILE = _REPO / "services" / "Dockerfile"
_WORKFLOWS = _REPO / ".github" / "workflows"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX40 = re.compile(r"[0-9a-f]{40}")


def _dockerfile_instructions(text: str) -> list[str]:
    """One string per Dockerfile instruction, continuations joined.

    Comment lines are dropped first: Docker ignores them even in the middle
    of a continued RUN, so they must not split or pad an instruction.
    """
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    joined = re.sub(r"\\\n\s*", " ", "\n".join(lines))  # join line continuations
    return [ln.strip() for ln in joined.splitlines() if ln.strip()]


def _dockerfile_args(text: str) -> dict[str, str]:
    args = {}
    for ins in _dockerfile_instructions(text):
        m = re.fullmatch(r"ARG\s+(\w+)=(\S+)", ins)
        if m:
            args[m.group(1)] = m.group(2)
    return args


def _workflow_steps() -> list[tuple[str, dict]]:
    steps = []
    for wf in sorted(_WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf.read_text()) or {}
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                steps.append((wf.name, step))
    return steps


def _release_fetch_blocks() -> list[tuple[str, str]]:
    """Every RUN / run: block that downloads a GitHub release asset."""
    blocks = [
        ("services/Dockerfile", ins)
        for ins in _dockerfile_instructions(_DOCKERFILE.read_text())
        if ins.startswith("RUN ")
    ]
    blocks += [(name, step["run"]) for name, step in _workflow_steps() if "run" in step]
    return [(where, b) for where, b in blocks if "releases/download/" in b]


def test_the_scan_finds_the_known_release_fetches():
    """Guards the guard: if the parsing broke, the rule below would pass
    vacuously over zero blocks."""
    where = {w for w, _ in _release_fetch_blocks()}
    assert "services/Dockerfile" in where
    assert "check.python.yml" in where


def test_every_release_download_is_checked_against_a_sha256():
    for where, block in _release_fetch_blocks():
        fetch = block.index("releases/download/")
        check = block.find("sha256sum -c", fetch)
        assert check != -1, (
            f"{where}: a releases/download fetch with no `sha256sum -c` after it "
            f"in the same block - a release asset can be replaced after publication"
        )
        chmod = block.find("chmod", fetch)
        install = block.find("install -m", fetch)
        made_executable = min(i for i in (chmod, install, len(block)) if i != -1)
        assert check < made_executable, (
            f"{where}: the binary is made executable before its digest is checked"
        )


def test_dockerfile_opengrep_digests_are_pinned_per_arch():
    args = _dockerfile_args(_DOCKERFILE.read_text())
    x86, arm = args.get("OPENGREP_SHA256_X86", ""), args.get("OPENGREP_SHA256_AARCH64", "")
    assert _HEX64.fullmatch(x86), f"OPENGREP_SHA256_X86 is not a sha256: {x86!r}"
    assert _HEX64.fullmatch(arm), f"OPENGREP_SHA256_AARCH64 is not a sha256: {arm!r}"
    assert x86 != arm, "the two architectures cannot share one digest"
    (fetch,) = [b for w, b in _release_fetch_blocks() if w == "services/Dockerfile"]
    # Each arm of the arch case selects its own digest.
    assert re.search(r"OG_ASSET=opengrep_manylinux_aarch64\s+OG_SHA=\"?\$\{?OPENGREP_SHA256_AARCH64", fetch)
    assert re.search(r"OG_ASSET=opengrep_manylinux_x86\s+OG_SHA=\"?\$\{?OPENGREP_SHA256_X86", fetch)


def test_ci_opengrep_matches_the_dockerfile_version_and_x86_digest():
    args = _dockerfile_args(_DOCKERFILE.read_text())
    (step,) = [
        s for name, s in _workflow_steps()
        if name == "check.python.yml" and "releases/download/" in s.get("run", "")
    ]
    env = step.get("env") or {}
    assert str(env.get("OPENGREP_VERSION")) == args["OPENGREP_VERSION"], (
        "check.python.yml installs a different opengrep than the image ships"
    )
    assert env.get("OPENGREP_SHA256_X86") == args["OPENGREP_SHA256_X86"], (
        "check.python.yml checks a different x86 digest than services/Dockerfile"
    )
    assert "opengrep_manylinux_x86" in step["run"]


def test_signing_helper_clone_is_checked_against_a_pinned_commit():
    text = _DOCKERFILE.read_text()
    commit = _dockerfile_args(text).get("SIGNING_HELPER_COMMIT", "")
    assert _HEX40.fullmatch(commit), f"SIGNING_HELPER_COMMIT is not a commit sha: {commit!r}"
    clones = [i for i in _dockerfile_instructions(text) if i.startswith("RUN ") and "git clone" in i]
    assert clones, "the signing-helper source clone is gone; update this test with it"
    for run in clones:
        assert re.search(
            r'test "\$\(git -C /src rev-parse HEAD\)" = "\$\{?SIGNING_HELPER_COMMIT\}?"', run
        ), f"a git clone without a pinned-commit check (a tag can be moved): {run[:80]}"
        assert run.index("rev-parse HEAD") < run.index("go build"), (
            "the commit must be checked before the source is built"
        )
