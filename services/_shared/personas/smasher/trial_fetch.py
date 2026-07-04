"""Trial init phase 1: fetch the repo tarball (#469, ADR-0013). WEBHOOK-ONLY.

Runs as the `fetch` init container of the Trial Job — the ONLY container that
holds the short-lived `contents:read` token. Downloads the repo tarball at the
head SHA via the GitHub API (no git binary, no on-disk token — a tarball has no
`.git/config` to persist the credential) and extracts it into the workspace.

The token is used for exactly ONE request and never written to disk. The next
init phase (`trial_deps`) and the test phase run with NO token.
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
from pathlib import Path

import httpx

log = logging.getLogger("grug.smasher.trial_fetch")

_GH_API = "https://api.github.com"


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("GRUG_TRIAL_TOKEN", "")
    tarball_path = os.getenv("GRUG_TRIAL_TARBALL_PATH", "")
    workspace = Path(os.getenv("GRUG_TRIAL_WORKSPACE", "/workspace"))
    if not token or not tarball_path:
        log.error("trial_fetch_missing_config")
        return 1

    repo_dir = workspace / "repo"
    try:
        resp = httpx.get(
            f"{_GH_API}{tarball_path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            follow_redirects=True,
            timeout=60,
        )
        resp.raise_for_status()
        _extract_tarball(resp.content, repo_dir)
    except (httpx.HTTPError, tarfile.TarError, OSError) as e:
        log.error("trial_fetch_failed", extra={"kind": type(e).__name__})
        return 1
    log.info("trial_fetch_done", extra={"dir": str(repo_dir)})
    return 0


def _extract_tarball(content: bytes, dest: Path) -> None:
    """Extract the GitHub tarball into `dest`, stripping the single
    `<owner>-<repo>-<sha>/` top-level directory GitHub wraps everything in.
    Path-traversal-safe: members escaping `dest` are skipped."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
        members = tar.getmembers()
        root = members[0].name.split("/")[0] if members else ""
        prefix = root + "/"
        for member in members:
            name = member.name
            if name == root:
                continue
            if name.startswith(prefix):
                name = name[len(prefix):]
            if not name:
                continue
            target = (dest / name).resolve()
            # Refuse any member that would land outside dest (tar traversal).
            if not str(target).startswith(str(dest.resolve()) + os.sep):
                log.warning("trial_fetch_skip_unsafe_member", extra={"member": member.name})
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                if extracted is not None:
                    target.write_bytes(extracted.read())
            # Symlinks / devices / hardlinks are intentionally NOT extracted
            # (a symlink is another traversal vector; the tests don't need them).


if __name__ == "__main__":
    raise SystemExit(main())
