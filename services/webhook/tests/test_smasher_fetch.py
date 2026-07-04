"""Trial fetch tests (#469) — the tarball extractor must be path-traversal-safe
(the tarball is PR-author-controlled content)."""

from __future__ import annotations

import io
import tarfile

from personas.smasher.trial_fetch import _extract_tarball


def _make_tarball(entries: dict[str, bytes], root: str = "acme-widget-deadbeef") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # GitHub wraps everything under a single top-level dir.
        info = tarfile.TarInfo(name=root)
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
        for name, body in entries.items():
            full = f"{root}/{name}"
            info = tarfile.TarInfo(name=full)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def test_extracts_and_strips_root(tmp_path):
    tb = _make_tarball({"pkg/mod.py": b"x = 1\n", "README.md": b"hi\n"})
    dest = tmp_path / "repo"
    _extract_tarball(tb, dest)
    assert (dest / "pkg/mod.py").read_text() == "x = 1\n"
    assert (dest / "README.md").exists()
    # The GitHub root wrapper dir is stripped, not nested.
    assert not (dest / "acme-widget-deadbeef").exists()


def test_traversal_member_is_skipped(tmp_path):
    # A malicious member trying to escape via ../../ must NOT be written outside.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        root = "acme-widget-deadbeef"
        info = tarfile.TarInfo(name=root)
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
        evil_body = b"pwned\n"
        info = tarfile.TarInfo(name=f"{root}/../../escape.py")
        info.size = len(evil_body)
        tar.addfile(info, io.BytesIO(evil_body))
    dest = tmp_path / "repo"
    _extract_tarball(buf.getvalue(), dest)
    # Nothing landed outside dest.
    assert not (tmp_path / "escape.py").exists()
    assert not (tmp_path.parent / "escape.py").exists()
