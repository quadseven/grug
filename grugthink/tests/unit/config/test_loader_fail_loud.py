"""load_config never hides a broken config file as an empty one.

An empty result makes ConfigManager write a default config over the file,
so a parse or read error must raise instead (the bot list is the data at
stake: grug#1281 left the pod with bot_count 0).
"""

import os

import pytest
import yaml

from src.grugthink.config import loader, manager


def test_missing_file_is_uninitialized(tmp_path):
    assert loader.load_config(str(tmp_path / "absent.yaml")) == {}


@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n"])
def test_empty_document_is_uninitialized(tmp_path, text):
    cfg = tmp_path / "grugthink_config.yaml"
    cfg.write_text(text)
    assert loader.load_config(str(cfg)) == {}


def test_unparseable_yaml_raises(tmp_path):
    cfg = tmp_path / "grugthink_config.yaml"
    cfg.write_text("bots: [unclosed\n")
    with pytest.raises(yaml.YAMLError):
        loader.load_config(str(cfg))


def test_unparseable_json_raises(tmp_path):
    cfg = tmp_path / "grugthink_config.json"
    cfg.write_text("{not json")
    with pytest.raises(ValueError):
        loader.load_config(str(cfg))


def test_non_mapping_document_raises(tmp_path):
    cfg = tmp_path / "grugthink_config.yaml"
    cfg.write_text("- a\n- b\n")
    with pytest.raises(ValueError):
        loader.load_config(str(cfg))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_unreadable_file_raises(tmp_path):
    cfg = tmp_path / "grugthink_config.yaml"
    cfg.write_text(yaml.safe_dump({"environment": {"A": "b"}}))
    cfg.chmod(0)
    try:
        with pytest.raises(PermissionError):
            loader.load_config(str(cfg))
    finally:
        cfg.chmod(0o600)


def test_manager_never_writes_a_default_over_a_broken_file(tmp_path, monkeypatch):
    monkeypatch.setattr(manager.watcher, "start_watching", lambda _mgr: None)
    cfg = tmp_path / "grugthink_config.yaml"
    broken = "bots: [unclosed\n"
    cfg.write_text(broken)
    with pytest.raises(yaml.YAMLError):
        manager.ConfigManager(str(cfg))
    assert cfg.read_text() == broken


def test_reload_keeps_the_running_config_when_the_file_breaks(tmp_path, monkeypatch):
    monkeypatch.setattr(manager.watcher, "start_watching", lambda _mgr: None)
    cfg = tmp_path / "grugthink_config.yaml"
    cfg.write_text(yaml.safe_dump({"environment": {"KEEP": "me"}}))
    mgr = manager.ConfigManager(str(cfg))
    cfg.write_text("bots: [unclosed\n")
    mgr._reload_config()
    assert mgr.env_vars == {"KEEP": "me"}
