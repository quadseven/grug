"""Reconstruct review inputs from the pinned public pull request corpus."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CORPUS_SHA256 = "688ec55989afe263d2b22b049173b1836a2a9e7899dbc1a5c1b1ca8fa93f8351"
_REPOSITORY = re.compile(r"quadseven/([a-z0-9][a-z0-9-]*)")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}")
_SIZE_FIELDS = ("additions", "deletions", "files")


@dataclass(frozen=True)
class Snapshot:
    case_id: str
    repo: str
    pr: int
    root: Path = field(repr=False)
    base_sha: str
    head_sha: str
    diff: str = field(repr=False)
    diff_sha256: str


@dataclass(frozen=True)
class _Case:
    case_id: str
    repo: str
    slug: str
    pr: int
    base_sha: str
    head_sha: str
    size: tuple[int, int, int]


def _parse_case(row: object) -> _Case:
    if not isinstance(row, dict):
        raise ValueError("invalid pinned corpus case")
    repo = row.get("repo")
    match = _REPOSITORY.fullmatch(repo) if isinstance(repo, str) else None
    if match is None:
        raise ValueError("pinned corpus case is outside the public repository owner")
    slug = match[1]
    pr = row.get("pr")
    case_id = row.get("id")
    if type(pr) is not int or pr < 1 or case_id != f"{slug}-{pr}":
        raise ValueError("invalid pinned corpus case ID")
    base, head = row.get("base_sha"), row.get("head_sha")
    if not all(
        isinstance(oid, str) and _OBJECT_ID.fullmatch(oid) for oid in (base, head)
    ):
        raise ValueError(f"{case_id}: invalid Git object ID")
    size = row.get("size")
    if (
        not isinstance(size, dict)
        or any(type(size.get(key)) is not int or size[key] < 0 for key in _SIZE_FIELDS)
        or size["files"] == 0
    ):
        raise ValueError(f"{case_id}: invalid expected diff size")
    return _Case(
        case_id,
        repo,
        slug,
        pr,
        base,
        head,
        (
            size["additions"],
            size["deletions"],
            size["files"],
        ),
    )


def _load_cases(corpus_dir: Path) -> list[_Case]:
    try:
        payload = (corpus_dir / "cases.json").read_bytes()
        sidecar = (corpus_dir / "corpus.sha256").read_text(encoding="ascii").split()
    except (OSError, UnicodeError):
        raise ValueError("cannot read the pinned corpus and checksum") from None
    if (
        sidecar != [CORPUS_SHA256, "cases.json"]
        or hashlib.sha256(payload).hexdigest() != CORPUS_SHA256
    ):
        raise ValueError("corpus SHA-256 does not match the pinned public snapshot")
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("invalid pinned corpus JSON") from None
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] != 1
        or not isinstance(document.get("cases"), list)
        or not document["cases"]
    ):
        raise ValueError("invalid or empty pinned corpus")
    cases = [_parse_case(row) for row in document["cases"]]
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("pinned corpus contains duplicate case IDs")
    return cases


def _git(root: Path, case: _Case, *args: str) -> bytes:
    # Use local objects and the frozen head's attributes. Ignore inherited Git
    # settings and prevent partial clones from fetching missing blobs.
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_ATTR_SOURCE": case.head_sha,
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        result = subprocess.run(
            [
                "git",
                "--no-pager",
                "--no-replace-objects",
                "-c",
                f"core.attributesFile={os.devnull}",
                "-c",
                "core.quotePath=true",
                "-c",
                "diff.suppressBlankEmpty=false",
                *args,
            ],
            cwd=root,
            env=env,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError(
            f"{case.case_id}: could not read the pinned local Git snapshot"
        ) from None
    return result.stdout


def _diff_size(stats: bytes, case_id: str) -> tuple[int, int, int]:
    additions = deletions = 0
    lines = stats.splitlines()
    for line in lines:
        counts = line.split(b"\t", 2)
        if len(counts) != 3:
            raise ValueError(f"{case_id}: invalid Git diff statistics")
        added, deleted, _ = counts
        if added == deleted == b"-":
            continue  # A binary file contributes only to the file count.
        if not added.isdigit() or not deleted.isdigit():
            raise ValueError(f"{case_id}: invalid Git diff line counts")
        additions += int(added)
        deletions += int(deleted)
    return additions, deletions, len(lines)


def _snapshot(case: _Case, repo_root: Path) -> Snapshot:
    root = repo_root / case.slug
    for oid in (case.base_sha, case.head_sha):
        actual = _git(
            root, case, "rev-parse", "--verify", "--end-of-options", f"{oid}^{{commit}}"
        )
        if actual.strip() != oid.encode("ascii"):
            raise ValueError(
                f"{case.case_id}: pinned object does not identify the exact commit"
            )
    output = _git(
        root,
        case,
        "diff",
        "--numstat",
        "--patch",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-relative",
        "--diff-algorithm=myers",
        "--indent-heuristic",
        "--find-renames=50%",
        "-l0",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--unified=3",
        "--inter-hunk-context=0",
        "--submodule=short",
        "--ignore-submodules=none",
        f"-O{os.devnull}",
        f"{case.base_sha}...{case.head_sha}",
        "--",
    )
    stats, separator, patch = output.partition(b"\n\n")
    if not separator or not patch.startswith(b"diff --git "):
        raise ValueError(f"{case.case_id}: pinned Git diff is empty")
    if _diff_size(stats, case.case_id) != case.size:
        raise ValueError(
            f"{case.case_id}: Git diff size differs from the frozen record"
        )
    try:
        diff = patch.decode("utf-8")
    except UnicodeError:
        raise ValueError(f"{case.case_id}: pinned Git diff is not UTF-8") from None
    return Snapshot(
        case.case_id,
        case.repo,
        case.pr,
        root,
        case.base_sha,
        case.head_sha,
        diff,
        hashlib.sha256(patch).hexdigest(),
    )


def load_snapshots(corpus_dir: Path, repo_root: Path) -> list[Snapshot]:
    """Load every case in order or fail before any review can run."""
    return [_snapshot(case, repo_root) for case in _load_cases(corpus_dir)]
