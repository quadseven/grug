"""Tests for the pgvector memory backend.

Two tiers:
- Logic tests (always run): dimension guard, zero-vector rejection, backend
  selection by env, factory dispatch.
- Store tests (skip without GRUGTHINK_TEST_DATABASE_URL): real add/search/
  get_all/delete against a pgvector Postgres, using a deterministic fake
  embedder so cosine ordering is asserted, not hoped for. CI points the env at
  a `pgvector/pgvector:pg18` service.
"""

import os

import numpy as np
import pytest

from src.grugthink import grug_db, pgvector_store
from src.grugthink.pgvector_store import EMBED_DIM, _to_vector

TEST_DSN = os.environ.get("GRUGTHINK_TEST_DATABASE_URL", "")
_needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason="GRUGTHINK_TEST_DATABASE_URL unset - pgvector store tests require a real pgvector Postgres",
)


class FakeEmbedder:
    """Deterministic embedder: one-hot vectors keyed by keyword.

    Text sharing a keyword embeds to the same basis vector (cosine distance 0),
    so a query's nearest neighbour is predictable and orderings are testable.
    """

    KEYS = ["ugga", "bork", "grog", "og", "cave", "mammoth"]

    def encode(self, texts):
        out = []
        for t in texts:
            low = t.lower()
            slot = next((i for i, k in enumerate(self.KEYS) if k in low), len(self.KEYS))
            v = np.zeros(EMBED_DIM, dtype=np.float32)
            v[slot % EMBED_DIM] = 1.0
            out.append(v)
        return np.array(out, dtype=np.float32)


# --------------------------------------------------------------------------- #
# Logic tests (no database)
# --------------------------------------------------------------------------- #


def test_to_vector_accepts_correct_dimension():
    emb = np.ones((1, EMBED_DIM), dtype=np.float32)
    vec = _to_vector(emb)
    assert vec is not None and len(vec) == EMBED_DIM


def test_to_vector_rejects_wrong_dimension():
    emb = np.ones((1, EMBED_DIM + 1), dtype=np.float32)
    with pytest.raises(ValueError):
        _to_vector(emb)


def test_to_vector_zero_vector_is_none():
    """A zero vector (encode's failure sentinel) must not be stored as real."""
    emb = np.zeros((1, EMBED_DIM), dtype=np.float32)
    assert _to_vector(emb) is None


def test_to_vector_none_is_none():
    assert _to_vector(None) is None


def test_pgvector_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("GRUGTHINK_DATABASE_URL", raising=False)
    monkeypatch.delenv("GRUG_DATABASE_URL", raising=False)
    assert pgvector_store.pgvector_enabled() is False
    monkeypatch.setenv("GRUG_DATABASE_URL", "postgresql://x/y")
    assert pgvector_store.pgvector_enabled() is True


def test_factory_selects_sqlite_without_dsn(monkeypatch, tmp_path):
    monkeypatch.delenv("GRUGTHINK_DATABASE_URL", raising=False)
    monkeypatch.delenv("GRUG_DATABASE_URL", raising=False)
    mgr = grug_db.make_server_manager(str(tmp_path / "facts.db"), load_embedder=False)
    assert isinstance(mgr, grug_db.GrugServerManager)


def test_factory_selects_pgvector_with_dsn(monkeypatch, tmp_path):
    monkeypatch.setenv("GRUGTHINK_DATABASE_URL", "postgresql://x/y")
    mgr = grug_db.make_server_manager(str(tmp_path / "facts.db"), load_embedder=False)
    assert isinstance(mgr, pgvector_store.PgVectorServerManager)


def test_get_server_db_loads_embedder_without_deadlock(monkeypatch):
    """get_server_db() holds self.lock and calls _get_embedder(); the two must
    use different locks or the thread self-deadlocks (froze the loop in prod).

    Runs in a worker thread with a join timeout so a regression HANGS the test
    (detected) rather than blocking the whole suite.
    """
    import threading

    class _StubEmbedder:
        def __init__(self, *a, **k):
            pass

        def test_connection(self):
            return True

    monkeypatch.setenv("USE_OLLAMA_EMBEDDINGS", "true")
    monkeypatch.setenv("OLLAMA_URLS", "http://gateway:11434")
    monkeypatch.setattr("src.grugthink.embedders.OllamaEmbedder", _StubEmbedder)

    mgr = pgvector_store.PgVectorServerManager("/data/deadlock-check/facts.db", load_embedder=True)

    result = {}

    def _call():
        db = mgr.get_server_db("srv")
        result["ok"] = db is not None and mgr._embedder is not None

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "get_server_db deadlocked (embedder lock == manager lock)"
    assert result.get("ok") is True


# --------------------------------------------------------------------------- #
# Store tests (real pgvector Postgres)
# --------------------------------------------------------------------------- #


@pytest.fixture
def pg_env(monkeypatch):
    """Point the store at the test DSN and reset the module-global pool."""
    monkeypatch.setenv("GRUGTHINK_DATABASE_URL", TEST_DSN)
    # Force a clean pool/bootstrap for the test process.
    pgvector_store._pool = None
    pgvector_store._bootstrapped = False
    yield
    try:
        if pgvector_store._pool is not None:
            pgvector_store._pool.close()
    finally:
        pgvector_store._pool = None
        pgvector_store._bootstrapped = False


def _cleanup(namespace):
    with pgvector_store.get_pool().connection() as conn:
        conn.execute(f"DELETE FROM {pgvector_store._TABLE} WHERE namespace = %s", (namespace,))
        conn.commit()


@_needs_db
def test_add_search_get_delete_roundtrip(pg_env):
    ns = "/data/test-bot/facts.db#roundtrip"
    mgr = pgvector_store.PgVectorServerManager(ns, load_embedder=False)
    # inject deterministic embedder (load_embedder=False skips gateway)
    mgr._embedder = FakeEmbedder()
    mgr._embedder_loaded = True
    db = mgr.get_server_db("server1")
    try:
        assert db.add_fact("Ugga is Grug wife") is True
        assert db.add_fact("Bork is Grug son") is True
        # duplicate rejected
        assert db.add_fact("Ugga is Grug wife") is False

        allf = db.get_all_facts()
        assert set(allf) == {"Ugga is Grug wife", "Bork is Grug son"}

        # semantic search: query keyed to "ugga" returns the Ugga fact first
        hits = db.search_facts("tell me about ugga", k=2)
        assert hits and hits[0] == "Ugga is Grug wife"

        assert db.delete_fact("Ugga is Grug wife") is True
        assert db.delete_fact("Ugga is Grug wife") is False
        assert db.get_all_facts() == ["Bork is Grug son"]
    finally:
        _cleanup(ns)


@_needs_db
def test_scopes_are_isolated(pg_env):
    ns = "/data/test-bot/facts.db#isolation"
    mgr = pgvector_store.PgVectorServerManager(ns, load_embedder=False)
    mgr._embedder = FakeEmbedder()
    mgr._embedder_loaded = True
    s1 = mgr.get_server_db("serverA")
    s2 = mgr.get_server_db("serverB")
    try:
        s1.add_fact("cave near big river")
        assert s1.get_all_facts() == ["cave near big river"]
        # a different server_id sees nothing from serverA
        assert s2.get_all_facts() == []
        assert s2.search_facts("cave", k=5) == []
    finally:
        _cleanup(ns)


@_needs_db
def test_namespaces_are_isolated(pg_env):
    ns_a = "/data/bot-a/facts.db#nsiso"
    ns_b = "/data/bot-b/facts.db#nsiso"
    ma = pgvector_store.PgVectorServerManager(ns_a, load_embedder=False)
    mb = pgvector_store.PgVectorServerManager(ns_b, load_embedder=False)
    ma._embedder = mb._embedder = FakeEmbedder()
    ma._embedder_loaded = mb._embedder_loaded = True
    da = ma.get_server_db("s")
    db_ = mb.get_server_db("s")
    try:
        da.add_fact("mammoth good meat")
        assert da.get_all_facts() == ["mammoth good meat"]
        assert db_.get_all_facts() == []  # different bot namespace
    finally:
        _cleanup(ns_a)
        _cleanup(ns_b)
