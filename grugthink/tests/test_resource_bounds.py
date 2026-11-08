"""grug#1686: SQLite connections close on error, caches stay bounded, and the
memories list resolves each server name once."""

import asyncio
import contextlib
import os
import sqlite3
import types

import pytest

# prompts reads the bot config at import time, during collection.
os.environ.setdefault("DISCORD_TOKEN", "fake_token")

from src.grugthink import grug_db  # noqa: E402
from src.grugthink import personality_engine as pe  # noqa: E402
from src.grugthink.bot import prompts  # noqa: E402


class _FakeConn:
    def __init__(self, fail_on):
        self.fail_on = fail_on
        self.closed = False

    def cursor(self):
        conn = self

        class _Cur:
            def execute(self, sql, *args):
                if conn.fail_on in sql:
                    raise sqlite3.OperationalError("boom")

            def fetchall(self):
                return []

        return _Cur()

    def commit(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("method", "fail_on"),
    [("_init_db", "CREATE TABLE"), ("_save_personality", "INSERT"), ("_load_all_personalities", "SELECT")],
)
def test_personality_connection_closes_when_a_statement_raises(tmp_path, monkeypatch, method, fail_on):
    engine = pe.PersonalityEngine(db_path=str(tmp_path / "p.db"))
    state = engine.get_personality("srv-1")
    opened = []

    def _connect(*_a, **_kw):
        conn = _FakeConn(fail_on)
        opened.append(conn)
        return conn

    monkeypatch.setattr(pe.sqlite3, "connect", _connect)
    # _init_db re-raises after logging; the other two log and carry on.
    with contextlib.suppress(sqlite3.OperationalError):
        if method == "_save_personality":
            engine._save_personality(state)
        else:
            getattr(engine, method)()
    assert opened, "no connection was opened"
    assert all(c.closed for c in opened)


def test_string_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(grug_db, "_string_cache", {})
    for i in range(grug_db._STRING_CACHE_MAX + 500):
        assert grug_db._intern_string(f"s-{i}") == f"s-{i}"
    assert len(grug_db._string_cache) <= grug_db._STRING_CACHE_MAX


def test_cooldowns_drop_expired_entries(monkeypatch):
    monkeypatch.setattr(prompts, "user_cooldowns", {})
    clock = {"t": 1000.0}
    monkeypatch.setattr(prompts.time, "time", lambda: clock["t"])
    bound = prompts._COOLDOWN_PRUNE_AT
    for i in range(bound * 3):
        clock["t"] += prompts.COOLDOWN_SECONDS  # every earlier entry is now expired
        assert prompts.is_rate_limited(i, bot_id="b") is False
        assert len(prompts.user_cooldowns) <= bound
    # A user inside the window is still limited.
    assert prompts.is_rate_limited(bound * 3 - 1, bot_id="b") is True


def test_memories_list_resolves_each_server_name_once(monkeypatch):
    from src.grugthink.api.routers import memories

    calls = []

    async def _name(bot, srv_id):
        calls.append(srv_id)
        return f"name-{srv_id}"

    monkeypatch.setattr(memories, "_get_server_name", _name)
    dbs = {
        "s1": types.SimpleNamespace(get_all_facts=lambda: [f"fact {i}" for i in range(50)]),
        "s2": types.SimpleNamespace(get_all_facts=lambda: ["one", "two"]),
    }
    bot = types.SimpleNamespace(server_manager=types.SimpleNamespace(server_dbs=dbs))
    manager = types.SimpleNamespace(bots={"b1": bot})
    out = asyncio.run(memories.get_bot_memories("b1", server_id=None, search=None, limit=1000, bot_manager=manager))
    assert sorted(calls) == ["s1", "s2"]
    assert len(out["memories"]) == 52
    assert {m["server_name"] for m in out["memories"]} == {"name-s1", "name-s2"}
