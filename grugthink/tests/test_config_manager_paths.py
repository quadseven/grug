import tempfile
from pathlib import Path

from src.grugthink.config.manager import ConfigManager


def test_config_manager_uses_env_and_initializes_empty_file(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "grugthink_config.yaml"
        # Create zero-byte config file
        cfg_path.touch()
        assert cfg_path.exists() and cfg_path.stat().st_size == 0

        monkeypatch.setenv("GRUGTHINK_CONFIG_PATH", str(cfg_path))

        cm = ConfigManager()
        # Should have created default config
        assert cfg_path.exists() and cfg_path.stat().st_size > 0
        data = cm.get_config()
        assert isinstance(data, dict)
        assert data.get("version") == "2.0"

        # Change an env var and ensure it persists
        cm.set_env_var("TEST_KEY", "123")
        assert cm.get_env_var("TEST_KEY") == "123"
        assert cfg_path.stat().st_size > 0
