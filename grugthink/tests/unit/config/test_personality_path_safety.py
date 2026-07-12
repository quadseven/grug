"""Path-injection guards for personality file operations (CodeQL py/path-injection).

personality_id is user-provided via the admin API and becomes a file name under
personalities/<id>.yaml. These tests pin that a traversal id can never escape the
personalities/ directory, at both the validator and the file-write boundary.
"""

import os

import pytest

from src.grugthink.config import personalities


@pytest.mark.parametrize("good", ["grug", "big_rob", "Bot-1", "a", "a" * 64])
def test_safe_ids_accepted(good):
    assert personalities.is_safe_personality_id(good) is True
    assert personalities.personality_file_path(good) == os.path.join("personalities", f"{good}.yaml")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "..",
        "../etc/passwd",
        "a/b",
        "a\\b",
        "/etc/passwd",
        "grug/../..",
        "with space",
        "dot.yaml",
        "null\x00byte",
        "a" * 65,
    ],
)
def test_unsafe_ids_rejected(bad):
    assert personalities.is_safe_personality_id(bad) is False
    with pytest.raises(ValueError):
        personalities.personality_file_path(bad)


def test_save_refuses_traversal_and_writes_nothing_outside(tmp_path, monkeypatch):
    # Run inside a temp CWD so a successful escape would be observable on disk.
    monkeypatch.chdir(tmp_path)
    ok = personalities.save_personality_to_file("../escape", {"name": "x"})
    assert ok is False
    # Nothing written anywhere above the personalities/ dir.
    assert not (tmp_path / "escape.yaml").exists()
    assert not (tmp_path.parent / "escape.yaml").exists()


def test_save_accepts_safe_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ok = personalities.save_personality_to_file("grug", {"name": "Grug"})
    assert ok is True
    assert (tmp_path / "personalities" / "grug.yaml").exists()
