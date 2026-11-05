"""Personality writes go to a writable directory, and the config is saved only
after the file write succeeds (#1633).

The k8s deployment runs with readOnlyRootFilesystem, so the image's own
personalities/ directory cannot be written; the data volume can.
"""

import os

import pytest

from src.grugthink.config import personalities
from src.grugthink.config.manager import ConfigManager


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRUGTHINK_CONFIG_PATH", str(tmp_path / "grugthink_config.yaml"))
    monkeypatch.delenv("GRUGTHINK_PERSONALITIES_DIR", raising=False)
    monkeypatch.setenv("GRUGBOT_DATA_DIR", str(tmp_path / "data"))
    # No file watcher: a hot reload racing the test's own writes makes it flaky.
    monkeypatch.setattr("src.grugthink.config.watcher.create_observer_and_handler", lambda _cm: (None, None))
    monkeypatch.setattr("src.grugthink.config.watcher.start_watching", lambda _cm: None)
    monkeypatch.setattr("src.grugthink.config.watcher.stop_watching", lambda _cm: None)
    cm = ConfigManager()
    yield cm
    cm.stop()


def test_writable_dir_defaults_under_data_dir(manager, tmp_path):
    assert personalities.writable_personalities_dir() == os.path.join(str(tmp_path / "data"), "personalities")


def test_explicit_dir_wins(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("GRUGTHINK_PERSONALITIES_DIR", str(tmp_path / "elsewhere"))
    assert personalities.writable_personalities_dir() == str(tmp_path / "elsewhere")


def test_add_writes_under_data_dir_and_saves_config(manager, tmp_path):
    assert manager.add_personality("caveman", {"name": "Caveman"})
    assert (tmp_path / "data" / "personalities" / "caveman.yaml").is_file()
    assert manager.get_personality("caveman") == {"name": "Caveman"}
    # The shipped (read-only in production) directory is not written.
    assert not (tmp_path / "personalities" / "caveman.yaml").exists()


def test_add_failed_write_leaves_config_unchanged(manager, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("GRUGTHINK_PERSONALITIES_DIR", str(blocker))

    assert manager.add_personality("caveman", {"name": "Caveman"}) is False
    assert manager.get_personality("caveman") is None


def test_update_failed_write_leaves_config_unchanged(manager, tmp_path, monkeypatch):
    assert manager.add_personality("caveman", {"name": "Caveman"})
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("GRUGTHINK_PERSONALITIES_DIR", str(blocker))

    assert manager.update_personality("caveman", {"name": "Changed"}) is False
    assert manager.get_personality("caveman") == {"name": "Caveman"}


def test_written_personality_loads_back(manager, tmp_path):
    (tmp_path / "personalities").mkdir()
    (tmp_path / "personalities" / "shipped.yaml").write_text("name: Shipped\n")
    assert manager.add_personality("custom", {"name": "Custom"})

    loaded = personalities.load_external_personalities()
    assert loaded["shipped"] == {"name": "Shipped"}
    assert loaded["custom"] == {"name": "Custom"}
