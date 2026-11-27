import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench import replay_inputs  # noqa: E402


def _git(root, *args):
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Review Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def local_case(tmp_path):
    root = tmp_path / "example"
    root.mkdir()
    _git(root, "init", "--quiet")
    (root / "setting.py").write_text("ENABLED = False\n\nTIMEOUT = 5\n", encoding="utf-8")
    _git(root, "add", "setting.py")
    _git(root, "commit", "--quiet", "-m", "Initial setting")
    fork = _git(root, "rev-parse", "HEAD")

    (root / "setting.py").write_text("ENABLED = True\n\nTIMEOUT = 5\n", encoding="utf-8")
    _git(root, "add", "setting.py")
    _git(root, "commit", "--quiet", "-m", "Topic edit")
    head = _git(root, "rev-parse", "HEAD")

    _git(root, "checkout", "--quiet", "--detach", fork)
    (root / "base_only.py").write_text("LABEL = 'base'\n", encoding="utf-8")
    _git(root, "add", "base_only.py")
    _git(root, "commit", "--quiet", "-m", "Advance base")
    base = _git(root, "rev-parse", "HEAD")
    return {
        "id": "example-1",
        "repo": "quadseven/example",
        "pr": 1,
        "kind": "bug",
        "defects": [{"answer": "never sent to the reviewer"}],
        "base_sha": base,
        "head_sha": head,
        "size": {"additions": 1, "deletions": 1, "files": 1},
    }


def _pin_corpus(corpus_dir, monkeypatch, cases):
    payload = (json.dumps({"version": 1, "cases": cases}) + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    (corpus_dir / "cases.json").write_bytes(payload)
    (corpus_dir / "corpus.sha256").write_text(
        f"{digest}  cases.json\n", encoding="ascii"
    )
    monkeypatch.setattr(replay_inputs, "CORPUS_SHA256", digest)


def test_local_blank_line_formatting_cannot_change_the_replay_input(tmp_path, monkeypatch, local_case):
    _pin_corpus(tmp_path, monkeypatch, [local_case])
    [first] = replay_inputs.load_snapshots(tmp_path, tmp_path)
    _git(first.root, "config", "diff.suppressBlankEmpty", "true")
    [second] = replay_inputs.load_snapshots(tmp_path, tmp_path)
    assert second.diff == first.diff
    assert second.diff_sha256 == first.diff_sha256


def test_reconstructs_the_topic_diff_from_the_frozen_merge_base(
    tmp_path, monkeypatch, local_case
):
    _pin_corpus(tmp_path, monkeypatch, [local_case])

    [snapshot] = replay_inputs.load_snapshots(tmp_path, tmp_path)

    assert snapshot.head_sha == local_case["head_sha"]
    assert snapshot.base_sha == local_case["base_sha"]
    assert snapshot.root == tmp_path / "example"
    assert snapshot.diff.startswith("diff --git a/setting.py b/setting.py\n")
    assert "-ENABLED = False\n+ENABLED = True\n" in snapshot.diff
    assert "base_only.py" not in snapshot.diff
    assert (
        snapshot.diff_sha256
        == hashlib.sha256(snapshot.diff.encode("utf-8")).hexdigest()
    )
    assert set(asdict(snapshot)) == {
        "case_id",
        "repo",
        "pr",
        "root",
        "base_sha",
        "head_sha",
        "diff",
        "diff_sha256",
    }


def test_patch_is_independent_of_worktree_and_diff_configuration(
    tmp_path, monkeypatch, local_case
):
    _pin_corpus(tmp_path, monkeypatch, [local_case])
    [before] = replay_inputs.load_snapshots(tmp_path, tmp_path)
    root = tmp_path / "example"
    _git(root, "config", "diff.external", "false")
    _git(root, "config", "diff.noPrefix", "true")
    _git(root, "config", "core.abbrev", "5")
    (root / "setting.py").write_text("ENABLED = None\n", encoding="utf-8")
    (root / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")

    [after] = replay_inputs.load_snapshots(tmp_path, tmp_path)

    assert after == before


@pytest.mark.parametrize("tamper", ["sidecar", "manifest", "both"])
def test_refuses_any_change_to_the_pinned_corpus(
    tmp_path, monkeypatch, local_case, tamper
):
    _pin_corpus(tmp_path, monkeypatch, [local_case])
    manifest = tmp_path / "cases.json"
    sidecar = tmp_path / "corpus.sha256"
    if tamper in {"manifest", "both"}:
        manifest.write_bytes(manifest.read_bytes() + b" ")
    if tamper in {"sidecar", "both"}:
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        filename = "other.json" if tamper == "sidecar" else "cases.json"
        sidecar.write_text(f"{digest}  {filename}\n", encoding="ascii")

    with pytest.raises(ValueError, match="pinned public snapshot"):
        replay_inputs.load_snapshots(tmp_path, tmp_path)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"repo": "another-owner/example"}, "outside the public repository owner"),
        ({"repo": "quadseven/../example"}, "outside the public repository owner"),
        ({"head_sha": "HEAD"}, "invalid Git object ID"),
        ({"head_sha": "0" * 40}, "pinned local Git snapshot"),
        ({"id": "example-2"}, "invalid pinned corpus case ID"),
        (
            {"size": {"additions": True, "deletions": 1, "files": 1}},
            "invalid expected diff size",
        ),
        ({"size": {"additions": 2, "deletions": 1, "files": 1}}, "diff size differs"),
    ],
)
def test_refuses_inconsistent_case_metadata(
    tmp_path, monkeypatch, local_case, change, error
):
    _pin_corpus(tmp_path, monkeypatch, [{**local_case, **change}])

    with pytest.raises(ValueError, match=error):
        replay_inputs.load_snapshots(tmp_path, tmp_path)


def test_refuses_duplicate_or_empty_corpus(tmp_path, monkeypatch, local_case):
    _pin_corpus(tmp_path, monkeypatch, [local_case, local_case])
    with pytest.raises(ValueError, match="duplicate case IDs"):
        replay_inputs.load_snapshots(tmp_path, tmp_path)

    _pin_corpus(tmp_path, monkeypatch, [])
    with pytest.raises(ValueError, match="empty pinned corpus"):
        replay_inputs.load_snapshots(tmp_path, tmp_path)
