"""Tests for the Postgres config backend (grug#1190).

When GRUGTHINK_DATABASE_URL / GRUG_DATABASE_URL is set, the bot configuration
document lives in the ``grugthink_config`` table instead of a YAML file on the
``grugthink-data`` PVC - which is what lets the k8s deployment drop the
node-pinned local-path volume without breaking dashboard writes.

Two tiers, mirroring test_pgvector_store.py:
- Logic/routing tests (always run): backend selection by env, loader dispatch,
  seed-on-first-boot, manager watcher behavior. The pg_store I/O functions are
  stubbed so no database is needed.
- Store tests (skip without GRUGTHINK_TEST_DATABASE_URL): real save/load
  round-trip against a Postgres, like CI's pgvector service job.
"""

import os

import pytest
import yaml

from src.grugthink.config import loader, manager, pg_store

TEST_DSN = os.environ.get("GRUGTHINK_TEST_DATABASE_URL", "")
_needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason="GRUGTHINK_TEST_DATABASE_URL unset - pg config store tests require a real Postgres",
)


@pytest.fixture
def pg_env(monkeypatch):
    """Pretend a Postgres DSN is configured (backend selection only)."""
    monkeypatch.delenv("GRUG_DATABASE_URL", raising=False)
    monkeypatch.setenv("GRUGTHINK_DATABASE_URL", "postgresql://example.invalid:5432/grugthink")
    assert pg_store.pg_config_enabled()


@pytest.fixture
def no_pg_env(monkeypatch):
    """No DSN configured: the file backend must be used unchanged."""
    monkeypatch.delenv("GRUGTHINK_DATABASE_URL", raising=False)
    monkeypatch.delenv("GRUG_DATABASE_URL", raising=False)
    assert not pg_store.pg_config_enabled()


# --------------------------------------------------------------------------- #
# Routing: with a DSN set, the loader must never touch the filesystem.
# --------------------------------------------------------------------------- #


def test_load_config_routes_to_postgres_when_dsn_set(pg_env, monkeypatch):
    calls = []
    sentinel = {"environment": {"FROM": "pg"}}

    def fake_load_or_seed(seed_path, templates):
        calls.append((seed_path, templates))
        return sentinel

    monkeypatch.setattr(pg_store, "load_or_seed", fake_load_or_seed)
    # A path that does not exist: the file backend would return {} for this.
    assert loader.load_config("/nonexistent/grugthink_config.yaml", {"t": 1}) is sentinel
    assert calls and calls[0][0] == "/nonexistent/grugthink_config.yaml"


def test_save_config_routes_to_postgres_when_dsn_set(pg_env, monkeypatch, tmp_path):
    saved = []

    def fake_save(data):
        saved.append(data)

    monkeypatch.setattr(pg_store, "save_config", fake_save)
    target = tmp_path / "grugthink_config.yaml"
    loader.save_config(str(target), {"environment": {"A": "b"}})
    assert saved == [{"environment": {"A": "b"}}]
    assert not target.exists(), "file backend must not write when Postgres is selected"


def test_file_backend_unchanged_without_dsn(no_pg_env, tmp_path):
    target = tmp_path / "grugthink_config.yaml"
    target.write_text(yaml.safe_dump({"environment": {"FILE": "yes"}}))
    assert loader.load_config(str(target)) == {"environment": {"FILE": "yes"}}

    other = tmp_path / "out.yaml"
    loader.save_config(str(other), {"environment": {"WROTE": "file"}})
    assert yaml.safe_load(other.read_text()) == {"environment": {"WROTE": "file"}}


def test_manager_skips_file_watcher_in_pg_mode(pg_env, monkeypatch):
    monkeypatch.setattr(pg_store, "load_or_seed", lambda seed_path, templates: {"environment": {}})
    mgr = manager.ConfigManager("grugthink_config.yaml")
    try:
        assert mgr._pg_config is True
        assert mgr.observer is None, "no file watcher when config lives in Postgres"
        assert mgr.config_data.get("environment") == {}
    finally:
        mgr.stop()


def test_manager_watches_file_without_dsn(no_pg_env, tmp_path, monkeypatch):
    cfg = tmp_path / "grugthink_config.yaml"
    cfg.write_text(yaml.safe_dump({"environment": {}}))
    monkeypatch.chdir(tmp_path)
    mgr = manager.ConfigManager("grugthink_config.yaml")
    try:
        assert mgr._pg_config is False
    finally:
        mgr.stop()


# --------------------------------------------------------------------------- #
# Seed-on-first-boot: empty table -> seed file (PVC-era migration) -> default.
# --------------------------------------------------------------------------- #


def test_load_or_seed_prefers_existing_row(monkeypatch):
    existing = {"environment": {"LIVE": "1"}}
    monkeypatch.setattr(pg_store, "load_config", lambda: existing)
    saved = []
    monkeypatch.setattr(pg_store, "save_config", saved.append)
    assert pg_store.load_or_seed("/nonexistent.yaml", {}) == existing
    assert saved == [], "must not overwrite an existing config row"


def test_load_or_seed_migrates_from_seed_file(monkeypatch, tmp_path):
    monkeypatch.setattr(pg_store, "load_config", lambda: {})
    saved = []
    monkeypatch.setattr(pg_store, "save_config", saved.append)
    seed = tmp_path / "grugthink_config.yaml"
    seed.write_text(yaml.safe_dump({"environment": {"MIGRATED": "yes"}}))
    result = pg_store.load_or_seed(str(seed), {})
    assert result == {"environment": {"MIGRATED": "yes"}}
    assert saved == [result], "seed file content must be persisted to Postgres"


def test_load_or_seed_creates_default_without_seed_file(monkeypatch):
    monkeypatch.setattr(pg_store, "load_config", lambda: {})
    saved = []
    monkeypatch.setattr(pg_store, "save_config", saved.append)
    result = pg_store.load_or_seed("/nonexistent.yaml", {})
    assert result["version"] == "2.0"
    assert result["environment"]["GRUGBOT_VARIANT"] == "prod"
    assert saved == [result]


def test_build_default_config_matches_file_default():
    via_build = loader.build_default_config({})
    assert via_build["version"] == "2.0"
    assert via_build["api_keys"]["discord"] == {"tokens": []}


# --------------------------------------------------------------------------- #
# Live Postgres round-trip (CI only).
# --------------------------------------------------------------------------- #


@_needs_db
def test_pg_config_roundtrip(monkeypatch):
    monkeypatch.delenv("GRUG_DATABASE_URL", raising=False)
    monkeypatch.setenv("GRUGTHINK_DATABASE_URL", TEST_DSN)
    pg_store.reset_pool_for_tests()
    try:
        doc = {"environment": {"ROUNDTRIP": "ok"}, "version": "2.0"}
        pg_store.save_config(doc)
        assert pg_store.load_config() == doc

        # load_or_seed must not clobber the row it just wrote.
        assert pg_store.load_or_seed("/nonexistent.yaml", {}) == doc
    finally:
        pg_store.reset_pool_for_tests()
