#!/usr/bin/env python3
"""Grug's long-term memory on Postgres + pgvector.

A drop-in backend for GrugDB / GrugServerManager that stores each fact and its
embedding in ONE Postgres row instead of a per-server SQLite file plus a
per-server FAISS index. See docs/pgvector-memory.md for the rationale.

Selected at construction time by grug_db when GRUGTHINK_DATABASE_URL (or
GRUG_DATABASE_URL) is set. The public methods mirror GrugDB exactly so no caller
changes:

    add_fact(text) -> bool          # False if the fact already exists
    search_facts(query, k) -> list[str]
    get_all_facts() -> list[str]
    delete_fact(content) -> bool
    close() -> None                 # no-op (pool is process-global)

Embeddings come from the owned spark-gateway OllamaEmbedder (nomic-embed-text
:v1.5, 768-dim) -- the same and only embedding path the light image ships.
"""

import os
import threading

from .grug_structured_logger import get_logger

log = get_logger(__name__)

# Single source of truth: the DDL column width and every insert/query read this.
# A vector of any other length is a bug and is never written (see _to_vector).
EMBED_DIM = int(os.getenv("GRUGTHINK_EMBED_DIM", "768"))

_TABLE = "grugthink_facts"

# Lazy, process-global connection pool. Built on first use so importing this
# module (e.g. in tests without a database) never opens a socket.
_pool = None
_pool_lock = threading.Lock()
_bootstrapped = False


def _database_url() -> str:
    """DSN for Grug's memory. Reuses the Postgres grug already runs."""
    return os.environ.get("GRUGTHINK_DATABASE_URL") or os.environ.get("GRUG_DATABASE_URL", "")


def pgvector_enabled() -> bool:
    """True when a Postgres DSN is configured (selects this backend)."""
    return bool(_database_url())


def _bootstrap_schema(conn) -> None:
    """Idempotently create the extension, table and indexes.

    CREATE EXTENSION needs to run before pgvector's psycopg adapter can be
    registered on a connection, so this happens once against a raw connection
    before the pool starts handing out register_vector'd connections.
    """
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            namespace  text NOT NULL,
            server_id  text NOT NULL,
            content    text NOT NULL,
            embedding  vector({EMBED_DIM}),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (namespace, server_id, content)
        )
        """
    )
    # HNSW cosine index for ANN retrieval; scope index for get_all/delete.
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {_TABLE}_embedding_idx "
        f"ON {_TABLE} USING hnsw (embedding vector_cosine_ops)"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS {_TABLE}_scope_idx ON {_TABLE} (namespace, server_id)")
    conn.commit()


def get_pool():
    """Lazily build the connection pool and bootstrap the schema once."""
    global _pool, _bootstrapped
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool

        import psycopg
        from pgvector.psycopg import register_vector
        from psycopg_pool import ConnectionPool

        dsn = _database_url()
        if not dsn:
            raise RuntimeError("GRUGTHINK_DATABASE_URL / GRUG_DATABASE_URL is not set")

        # Bootstrap the extension + schema on a raw connection first, so the
        # pooled connections can register the vector type at checkout.
        if not _bootstrapped:
            with psycopg.connect(dsn, connect_timeout=10) as conn:
                _bootstrap_schema(conn)
            _bootstrapped = True

        def _configure(conn):
            register_vector(conn)

        _pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=int(os.getenv("GRUGTHINK_PG_POOL_MAX", "4")),
            configure=_configure,
            kwargs={"connect_timeout": 10},
            open=True,
        )
        log.info("Grug pgvector pool initialized", extra={"table": _TABLE, "dimension": EMBED_DIM})
        return _pool


def _to_vector(embedding):
    """Coerce an embedder output row to a plain list of EMBED_DIM floats.

    Rejects a wrong-dimension vector loudly (a silent dim drift would corrupt
    every cosine distance). Returns None when no usable embedding exists so the
    caller can store the fact with a NULL embedding instead of a fake vector.
    """
    if embedding is None:
        return None
    # OllamaEmbedder.encode returns a (n, dim) numpy array; take the first row.
    row = embedding[0] if hasattr(embedding, "__len__") and len(embedding) else None
    if row is None:
        return None
    vec = row.tolist() if hasattr(row, "tolist") else list(row)
    if len(vec) != EMBED_DIM:
        raise ValueError(f"embedding dimension {len(vec)} != EMBED_DIM {EMBED_DIM}")
    # A zero vector is what encode() returns on failure -- treat as "no embedding"
    # rather than storing a vector that sits equidistant from everything.
    if not any(vec):
        return None
    return vec


def _vec_literal(vec):
    """Render a vector as a pgvector text literal, e.g. "[0.1,0.2]".

    Passed with an explicit ``%s::vector`` cast so the value arrives typed as
    ``vector`` at both the INSERT and the ``<=>`` operator. A bare Python list
    would be sent as ``double precision[]`` -- which INSERT can assignment-cast
    into the column but the ``<=>`` operator cannot (no implicit cast), so search
    would fail with "operator does not exist: vector <=> double precision[]".
    """
    if vec is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgVectorGrugDB:
    """pgvector-backed store for one (namespace, server_id) scope.

    Mirrors GrugDB's public surface. The embedder is shared (constructed once by
    the manager) so every server scope of a bot reuses one gateway client.
    """

    def __init__(self, namespace: str, server_id: str, embedder):
        self.namespace = namespace
        self.server_id = str(server_id) if server_id else "dm"
        self.embedder = embedder  # may be None when load_embedder is False
        # index attribute kept for GrugServerManager.get_server_stats() parity.
        self.index = None

    def _embed(self, text: str):
        if self.embedder is None:
            return None
        try:
            return _to_vector(self.embedder.encode([text]))
        except Exception as e:  # never let an embed failure lose the fact
            log.error("Embedding failed; storing fact without vector", extra={"error": str(e)})
            return None

    def add_fact(self, fact_text: str) -> bool:
        """Insert a fact (with its embedding). False if it already exists."""
        vec = self._embed(fact_text)
        try:
            with get_pool().connection() as conn:
                cur = conn.execute(
                    f"""
                    INSERT INTO {_TABLE} (namespace, server_id, content, embedding)
                    VALUES (%s, %s, %s, %s::vector)
                    ON CONFLICT (namespace, server_id, content) DO NOTHING
                    RETURNING id
                    """,
                    (self.namespace, self.server_id, fact_text, _vec_literal(vec)),
                )
                inserted = cur.fetchone() is not None
                conn.commit()
            if inserted:
                log.info("Added fact", extra={"fact": fact_text, "has_vector": vec is not None})
            else:
                log.warning("Fact already exists", extra={"fact": fact_text})
            return inserted
        except Exception as e:
            log.error("Error adding fact", extra={"error": str(e)})
            return False

    def search_facts(self, query: str, k: int = 5) -> list[str]:
        """Semantic search: nearest facts by cosine distance."""
        vec = self._embed(query)
        if vec is None:
            return []
        try:
            with get_pool().connection() as conn:
                cur = conn.execute(
                    f"""
                    SELECT content FROM {_TABLE}
                    WHERE namespace = %s AND server_id = %s AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (self.namespace, self.server_id, _vec_literal(vec), k),
                )
                results = [row[0] for row in cur.fetchall()]
            log.info("Found results for query", extra={"query": query, "results": len(results)})
            return results
        except Exception as e:
            log.error("Error searching facts", extra={"error": str(e)})
            return []

    def get_all_facts(self) -> list[str]:
        """All facts for this scope, newest first."""
        try:
            with get_pool().connection() as conn:
                cur = conn.execute(
                    f"SELECT content FROM {_TABLE} WHERE namespace = %s AND server_id = %s "
                    f"ORDER BY created_at DESC",
                    (self.namespace, self.server_id),
                )
                return [row[0] for row in cur.fetchall()]
        except Exception as e:
            log.error("Error getting all facts", extra={"error": str(e)})
            return []

    def delete_fact(self, fact_content: str) -> bool:
        """Delete a fact by exact content. False if nothing matched."""
        try:
            with get_pool().connection() as conn:
                cur = conn.execute(
                    f"DELETE FROM {_TABLE} WHERE namespace = %s AND server_id = %s AND content = %s",
                    (self.namespace, self.server_id, fact_content),
                )
                deleted = cur.rowcount
                conn.commit()
        except Exception as e:
            log.error("Error deleting fact", extra={"error": str(e), "fact": fact_content})
            return False
        if deleted > 0:
            log.info("Deleted fact", extra={"fact": fact_content, "count": deleted})
            return True
        log.warning("Fact not found for deletion", extra={"fact": fact_content})
        return False

    # --- parity no-ops (FAISS-specific lifecycle the PG backend doesn't need) ---
    def save_index(self):
        return None

    def rebuild_index(self):
        return None

    def close(self):
        return None


class PgVectorServerManager:
    """Drop-in for GrugServerManager backed by pgvector.

    One shared embedder for the whole bot; per-server scopes are cheap objects
    over the shared pool. `base_db_path` becomes the isolation namespace so two
    bots writing to the same database never see each other's facts.
    """

    def __init__(self, base_db_path, model_name="nomic-embed-text:v1.5", load_embedder: bool = True):
        self.base_db_path = base_db_path
        self.namespace = str(base_db_path)
        self.model_name = model_name
        self.load_embedder = load_embedder
        self.server_dbs = {}
        self.lock = threading.Lock()
        self._embedder = None
        self._embedder_loaded = False
        log.info(
            "Grug pgvector server manager initialized",
            extra={"namespace": self.namespace, "load_embedder": load_embedder},
        )

    def _get_embedder(self):
        """Build the shared OllamaEmbedder once (same gateway path as GrugDB)."""
        if self._embedder_loaded:
            return self._embedder
        with self.lock:
            if self._embedder_loaded:
                return self._embedder
            self._embedder_loaded = True
            if not self.load_embedder:
                log.info("Embedder loading explicitly disabled.")
                return None
            ollama_urls = os.getenv("OLLAMA_URLS")
            use_ollama = os.getenv("USE_OLLAMA_EMBEDDINGS", "true").lower() == "true"
            if not (ollama_urls and use_ollama):
                log.warning("No Ollama embeddings configured; semantic search disabled")
                return None
            try:
                from .embedders import OllamaEmbedder

                url = ollama_urls.split(",")[0].strip()
                model = os.getenv("GRUGTHINK_EMBED_MODEL", "nomic-embed-text:v1.5")
                self._embedder = OllamaEmbedder(ollama_url=url, model=model, dimension=EMBED_DIM)
                if self._embedder.test_connection():
                    log.info("Ollama embedder ready for pgvector store", extra={"url": url, "model": model})
                else:
                    log.warning("Ollama embedder self-test failed; semantic search disabled", extra={"url": url})
                    self._embedder = None
            except Exception as e:
                log.error("Failed to init Ollama embedder for pgvector store", extra={"error": str(e)})
                self._embedder = None
            return self._embedder

    def get_server_db(self, server_id) -> PgVectorGrugDB:
        server_id = str(server_id) if server_id else "dm"
        with self.lock:
            if server_id not in self.server_dbs:
                log.info("Creating pgvector brain for server", extra={"server_id": server_id})
                self.server_dbs[server_id] = PgVectorGrugDB(self.namespace, server_id, self._get_embedder())
            return self.server_dbs[server_id]

    def close_all(self):
        with self.lock:
            self.server_dbs.clear()
        log.info("All pgvector brains released")

    def get_server_stats(self) -> dict:
        stats = {}
        with self.lock:
            scopes = list(self.server_dbs.items())
        for server_id, db in scopes:
            try:
                facts = db.get_all_facts()
                stats[server_id] = {"fact_count": len(facts), "index_vectors": len(facts)}
            except Exception as e:
                stats[server_id] = {"error": str(e)}
        return stats

    def migrate_global_facts_to_server(self, target_server_id: str = "global"):
        """Re-scope facts stored under the legacy 'global'/empty server id."""
        try:
            with get_pool().connection() as conn:
                cur = conn.execute(
                    f"UPDATE {_TABLE} SET server_id = %s "
                    f"WHERE namespace = %s AND (server_id IS NULL OR server_id = '')",
                    (target_server_id, self.namespace),
                )
                moved = cur.rowcount
                conn.commit()
            log.info("Migrated global facts", extra={"moved": moved, "target": target_server_id})
        except Exception as e:
            log.error("Error migrating global facts", extra={"error": str(e)})
