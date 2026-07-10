import os
import sys
import tempfile
from pathlib import Path

import pytest

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient

    HAS_FASTAPI = True
except Exception:  # pragma: no cover - environment without fastapi
    HAS_FASTAPI = False
    TestClient = None

if HAS_FASTAPI:
    # Stub uvicorn if missing to allow importing api_server for tests
    try:
        import uvicorn  # noqa: F401
    except Exception:  # pragma: no cover
        import types

        sys.modules["uvicorn"] = types.SimpleNamespace()

    from src.grugthink.api_server import APIServer
    from src.grugthink.config_manager import ConfigManager

    from src.grugthink.bot_manager import BotManager


def make_server(tmp_cfg: Path):
    os.environ["GRUGTHINK_CONFIG_PATH"] = str(tmp_cfg)
    cm = ConfigManager()
    # Disable OAuth for tests
    cm.set_env_var("DISABLE_OAUTH", "true")
    bm = BotManager(config_manager=cm)
    api = APIServer(bm, cm)
    return api, cm


def test_admin_settings_get_and_put():
    if not HAS_FASTAPI:
        pytest.skip("fastapi/uvicorn not installed in this environment")
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "grugthink_config.yaml"
        api, cm = make_server(cfg)
        client = TestClient(api.app)

        # GET default settings
        r = client.get("/api/admin/settings")
        assert r.status_code == 200
        data = r.json()
        assert "DISABLE_OAUTH" in data

        # Update a few keys
        updates = {
            "LOG_LEVEL": "DEBUG",
            "TRUSTED_USER_IDS": "123,456",
            "GRUGBOT_DATA_DIR": "/mnt/data",
        }
        r = client.put("/api/admin/settings", json=updates)
        assert r.status_code == 200

        # Verify persisted
        r = client.get("/api/admin/settings")
        assert r.status_code == 200
        data = r.json()
        assert data["LOG_LEVEL"] == "DEBUG"
        assert data["TRUSTED_USER_IDS"] == "123,456"
        assert data["GRUGBOT_DATA_DIR"] == "/mnt/data"
