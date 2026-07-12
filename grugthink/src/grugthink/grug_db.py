#!/usr/bin/env python3
"""Grug's Memory Database - SQLite + FAISS
Manages Grug's long-term memory using a relational database for facts
and a vector index for semantic search.
"""

import contextlib
import logging
import os
import sqlite3
import threading

from .grug_structured_logger import get_logger

# Lazy import globals - only loaded when actually needed
_np = None
_faiss = None
_SentenceTransformer = None


def _get_numpy():
    """Lazy import numpy only when needed."""
    global _np
    if _np is None:
        try:
            import numpy as np

            _np = np
        except ImportError:
            _np = False
    return _np if _np is not False else None


def _get_faiss():
    """Lazy import faiss only when needed."""
    global _faiss
    if _faiss is None:
        try:
            import faiss

            _faiss = faiss
        except ImportError:
            _faiss = False
    return _faiss if _faiss is not False else None


def _get_sentence_transformer():
    """Lazy import SentenceTransformer only when needed."""
    global _SentenceTransformer
    if _SentenceTransformer is None:
        try:
            from sentence_transformers import SentenceTransformer

            _SentenceTransformer = SentenceTransformer
        except ImportError:
            _SentenceTransformer = False
    return _SentenceTransformer if _SentenceTransformer is not False else None


log = get_logger(__name__)


def make_server_manager(base_db_path, model_name="all-MiniLM-L6-v2", load_embedder: bool = True):
    """Return the memory backend selected by environment.

    When a Postgres DSN is configured (GRUGTHINK_DATABASE_URL / GRUG_DATABASE_URL)
    Grug's memory lives in pgvector -- one row per fact, no node-pinned PVC and no
    SQLite+FAISS to keep in sync. Otherwise fall back to the local SQLite+FAISS
    store (dev and tests without a database). Both expose the same public API, so
    callers construct through this factory and never branch on the backend.
    """
    from .pgvector_store import PgVectorServerManager, pgvector_enabled

    if pgvector_enabled():
        return PgVectorServerManager(base_db_path, model_name=model_name, load_embedder=load_embedder)
    return GrugServerManager(base_db_path, model_name=model_name, load_embedder=load_embedder)


# String interning cache for performance (common server IDs, personalities, etc.)
_string_cache = {}


def _intern_string(s):
    """Intern frequently used strings to reduce memory and improve comparison speed."""
    if s in _string_cache:
        return _string_cache[s]
    _string_cache[s] = s
    return s


def _get_model_cache_dir():
    """Get the directory where sentence-transformer models should be cached."""
    # Use standard cache directories based on the environment
    if os.environ.get("XDG_CACHE_HOME"):
        cache_root = os.environ["XDG_CACHE_HOME"]
    elif os.environ.get("HOME"):
        cache_root = os.path.join(os.environ["HOME"], ".cache")
    else:
        # Fallback to current directory/.cache for environments without HOME
        cache_root = os.path.join(os.getcwd(), ".cache")

    return os.path.join(cache_root, "grugthink", "sentence-transformers")


def download_model(model_name="all-MiniLM-L6-v2"):
    """Pre-download a SentenceTransformer model for offline use."""
    SentenceTransformer = _get_sentence_transformer()
    if SentenceTransformer is None:
        log.warning("SentenceTransformer not available, cannot download model")
        return False

    try:
        cache_dir = _get_model_cache_dir()
        model_path = os.path.join(cache_dir, model_name)

        if os.path.exists(model_path):
            log.info("Model already cached", extra={"model": model_name, "path": model_path})
            return True

        log.info("Downloading model", extra={"model": model_name, "cache_dir": cache_dir})
        os.makedirs(cache_dir, exist_ok=True)

        # Download the model
        SentenceTransformer(model_name, cache_folder=cache_dir)
        log.info("Model downloaded successfully", extra={"model": model_name, "path": model_path})
        return True

    except Exception as e:
        log.error("Failed to download model", extra={"model": model_name, "error": str(e)})
        return False


class GrugDB:
    def __init__(self, db_path, model_name="all-MiniLM-L6-v2", server_id="global", load_embedder: bool = True):
        self.db_path = db_path
        self.server_id = _intern_string(str(server_id))  # Intern for memory efficiency
        self.index_path = db_path.replace(".db", f"_{self.server_id}.index")
        self.model_name = model_name
        self.embedder = None
        self.dimension = 384  # Default dimension, will be updated if embedder loads
        self.load_embedder = load_embedder

        self.conn = None
        self.index = None
        self.lock = threading.Lock()

        self._init_db()
        self._load_index()

    def _ensure_embedder_loaded(self):
        if not self.load_embedder:
            log.info("Embedder loading explicitly disabled.")
            return

        if self.embedder is None:
            with self.lock:  # Acquire lock before loading to prevent race conditions
                if self.embedder is None:  # Double-check inside lock
                    # Check if Ollama embeddings are available (preferred - saves RAM)
                    ollama_urls = os.getenv("OLLAMA_URLS")
                    use_ollama_embeddings = os.getenv("USE_OLLAMA_EMBEDDINGS", "true").lower() == "true"

                    if ollama_urls and use_ollama_embeddings:
                        log.info("Using Ollama for embeddings (GPU-accelerated, saves RAM)")
                        try:
                            from .embedders import OllamaEmbedder

                            # Use first Ollama URL if multiple are provided
                            ollama_url = ollama_urls.split(",")[0].strip()

                            # The gateway serves ONE embed model: nomic-embed-text:v1.5
                            # (768-dim). The old "all-minilm" mapping asked for a model
                            # the gateway doesn't have -> "model not found" -> semantic
                            # search silently disabled. Use the gateway's model (env-
                            # overridable), and match its dimension.
                            embedding_model = os.getenv("GRUGTHINK_EMBED_MODEL", "nomic-embed-text:v1.5")
                            dimension = int(os.getenv("GRUGTHINK_EMBED_DIM", "768"))

                            self.embedder = OllamaEmbedder(
                                ollama_url=ollama_url, model=embedding_model, dimension=dimension
                            )
                            self.dimension = dimension

                            # Test connection
                            if self.embedder.test_connection():
                                log.info(
                                    "Ollama embedder initialized successfully",
                                    extra={"url": ollama_url, "model": embedding_model, "dimension": dimension},
                                )
                                return
                            else:
                                log.warning(
                                    "Ollama embedder test failed, falling back to local SentenceTransformer",
                                    extra={"url": ollama_url},
                                )
                                self.embedder = None
                        except Exception as e:
                            log.error(
                                "Failed to initialize Ollama embedder, falling back to SentenceTransformer",
                                extra={"error": str(e)},
                            )
                            self.embedder = None

                    # Fall back to local SentenceTransformer if Ollama not available
                    if self.embedder is None:
                        log.info("Loading SentenceTransformer model...")
                        SentenceTransformer = _get_sentence_transformer()
                        if SentenceTransformer is None:
                            import sys

                            if "sentence_transformers" in sys.modules:
                                self.embedder = sys.modules["sentence_transformers"].SentenceTransformer(
                                    self.model_name
                                )
                                self.dimension = self.embedder.get_sentence_embedding_dimension()
                            else:
                                log.warning("SentenceTransformer not available, semantic search will be disabled.")
                            return

                        # Try to load from cache directory first, download if needed
                        cache_dir = self._get_model_cache_dir()
                        local_model_path = os.path.join(cache_dir, self.model_name)

                        try:
                            # Try loading from cache first
                            if os.path.exists(local_model_path):
                                log.info("Loading model from cache", extra={"cache_path": local_model_path})
                                self.embedder = SentenceTransformer(local_model_path, local_files_only=True)
                            else:
                                # Download to cache directory
                                log.info(
                                    "Downloading model to cache",
                                    extra={"model": self.model_name, "cache_dir": cache_dir},
                                )
                                os.makedirs(cache_dir, exist_ok=True)
                                self.embedder = SentenceTransformer(self.model_name, cache_folder=cache_dir)

                            self.dimension = self.embedder.get_sentence_embedding_dimension()
                        except Exception as e:
                            log.error("Failed to load SentenceTransformer model", extra={"error": str(e)})
                            log.warning("SentenceTransformer model loading failed, semantic search will be disabled.")
                            return

                    log.info("SentenceTransformer model loaded.")

    def _get_model_cache_dir(self):
        """Get the directory where models should be cached."""
        return _get_model_cache_dir()

    def _init_db(self):
        """Initialize SQLite database and create facts table."""
        try:
            # Ensure the directory for the database file exists
            db_dir = os.path.dirname(self.db_path)
            if db_dir:  # Only create directory if db_path has a directory component
                os.makedirs(db_dir, exist_ok=True)

            # Check if database file exists and is accessible
            if os.path.exists(self.db_path):
                try:
                    # Try to open existing database
                    self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
                    # Test the connection with a simple query
                    cursor = self.conn.cursor()
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                    if "disk I/O error" in str(e) or "database is locked" in str(e):
                        log.warning(f"Database corrupted or locked, attempting recovery: {e}")
                        # Close any existing connection
                        if self.conn:
                            try:
                                self.conn.close()
                            except Exception:
                                pass
                        # Try to repair or recreate the database
                        backup_path = f"{self.db_path}.backup"
                        try:
                            os.rename(self.db_path, backup_path)
                            log.info(f"Moved corrupted database to {backup_path}")
                        except OSError:
                            log.warning("Could not backup corrupted database, removing it")
                            try:
                                os.remove(self.db_path)
                            except OSError:
                                pass
                    else:
                        raise

            # Create or recreate the database connection
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            cursor = self.conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id TEXT DEFAULT 'global',
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(server_id, content)
                )
            """)
            self.conn.commit()
            log.info("Database initialized", extra={"db_path": self.db_path})
        except Exception as e:
            log.error("Error initializing database", extra={"error": str(e)})
            raise

    def _load_index(self):
        """Load FAISS index from disk or create a new one."""
        if not self.load_embedder:
            log.info("Embedder loading disabled, skipping FAISS index load")
            self.index = None
            return

        faiss = _get_faiss()
        if faiss is None:
            # Use mocked version from conftest.py
            import sys

            if "faiss" in sys.modules:
                faiss_module = sys.modules["faiss"]
                if os.path.exists(self.index_path):
                    try:
                        self.index = faiss_module.read_index(self.index_path)
                        log.info(
                            "Loaded FAISS index", extra={"index_path": self.index_path, "vectors": self.index.ntotal}
                        )
                    except Exception as e:
                        log.error("Failed to load FAISS index, creating new one", extra={"error": str(e)})
                        self._create_new_index()
                else:
                    self._create_new_index()
            else:
                # No FAISS available, create placeholder
                self.index = None
                log.warning("FAISS not available, vector search disabled")
        else:
            # Normal FAISS operation
            if os.path.exists(self.index_path):
                try:
                    self.index = faiss.read_index(self.index_path)
                    log.info("Loaded FAISS index", extra={"index_path": self.index_path, "vectors": self.index.ntotal})
                except Exception as e:
                    log.error("Failed to load FAISS index, creating new one", extra={"error": str(e)})
                    self._create_new_index()
            else:
                self._create_new_index()

    def _create_new_index(self):
        """Create a new FAISS index and build it from existing DB facts."""
        log.info("Creating new FAISS index")
        faiss = _get_faiss()
        if faiss is None:
            # Use mocked version
            import sys

            if "faiss" in sys.modules:
                faiss_module = sys.modules["faiss"]
                self.index = faiss_module.IndexIDMap(faiss_module.IndexFlatL2(self.dimension))
            else:
                self.index = None
                return
        else:
            self.index = faiss.IndexIDMap(faiss.IndexFlatL2(self.dimension))
        self.rebuild_index()

    def add_fact(self, fact_text: str) -> bool:
        """Add a new fact to the database and the FAISS index."""
        self._ensure_embedder_loaded()
        # Encoding is CPU-bound and can be done outside the lock
        if self.embedder is None:
            # No embedder available, just add to database without vector search
            embedding = None
        else:
            embedding = self.embedder.encode([fact_text])

        with self.lock:
            try:
                # Use a transaction so DB insert and index update succeed or fail together
                with self.conn:
                    cursor = self.conn.execute(
                        "INSERT INTO facts (server_id, content) VALUES (?, ?)",
                        (self.server_id, fact_text),
                    )
                    fact_id = cursor.lastrowid
                    # Add to vector index if available
                    np = _get_numpy()
                    if embedding is not None and self.index is not None and np is not None:
                        # If this raises, the transaction will be rolled back
                        self.index.add_with_ids(embedding, np.array([fact_id]))

                log.info("Added fact", extra={"fact_id": fact_id, "fact": fact_text})
                return True
            except sqlite3.IntegrityError:
                log.warning("Fact already exists", extra={"fact": fact_text})
                return False
            except Exception as e:
                log.error("Error adding fact", extra={"error": str(e)})
                return False

    def search_facts(self, query: str, k: int = 5) -> list[str]:
        """Search for relevant facts using semantic search."""
        self._ensure_embedder_loaded()
        np = _get_numpy()
        if self.index is None or self.embedder is None or np is None:
            # No vector search available, return empty results
            return []

        if self.index.ntotal == 0:
            return []

        # Encoding is CPU-bound and can be done outside the lock
        query_embedding = self.embedder.encode([query])

        with self.lock:
            try:
                distances, indices = self.index.search(query_embedding, k)

                results = []
                cursor = self.conn.cursor()
                for i in indices[0]:
                    # The index is a faiss.IndexIDMap built via add_with_ids(), so
                    # returned indices are already the real SQLite fact ids.
                    cursor.execute("SELECT content FROM facts WHERE id=? AND server_id=?", (int(i), self.server_id))
                    row = cursor.fetchone()
                    if row:
                        results.append(row[0])

                log.info("Found results for query", extra={"query": query, "results": len(results)})
                return results
            except Exception as e:
                log.error("Error searching facts", extra={"error": str(e)})
                return []

    def get_all_facts(self) -> list[str]:
        """Retrieve all facts from the database."""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "SELECT content FROM facts WHERE server_id = ? ORDER BY timestamp DESC", (self.server_id,)
                )
                return [row[0] for row in cursor.fetchall()]
            except Exception as e:
                log.error("Error getting all facts", extra={"error": str(e)})
                return []

    def delete_fact(self, fact_content: str) -> bool:
        """Delete a fact from the database."""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("DELETE FROM facts WHERE server_id = ? AND content = ?", (self.server_id, fact_content))
                deleted_count = cursor.rowcount
                self.conn.commit()
            except Exception as e:
                log.error("Error deleting fact", extra={"error": str(e), "fact": fact_content})
                return False

        if deleted_count > 0:
            log.info("Deleted fact", extra={"fact": fact_content, "count": deleted_count})
            # Keep the FAISS index in sync with SQLite after a deletion (lock
            # released above since rebuild_index() acquires it itself).
            self.rebuild_index()
            return True
        else:
            log.warning("Fact not found for deletion", extra={"fact": fact_content})
            return False

    def save_index(self):
        """Save the FAISS index to disk."""
        with self.lock:
            try:
                faiss = _get_faiss()
                if faiss is None:
                    # Use mocked version
                    import sys

                    if "faiss" in sys.modules:
                        faiss_module = sys.modules["faiss"]
                        faiss_module.write_index(self.index, self.index_path)
                    # If no FAISS available, just skip saving
                else:
                    faiss.write_index(self.index, self.index_path)
                log.info("FAISS index saved", extra={"index_path": self.index_path})
            except Exception as e:
                log.error("Error saving FAISS index", extra={"error": str(e)})

    def rebuild_index(self):
        """Rebuild the entire FAISS index from the SQLite database."""
        np = _get_numpy()
        if self.index is None or self.embedder is None or np is None:
            # No vector search available, skip rebuild
            log.info("Skipping index rebuild - vector search not available")
            return

        log.info("Rebuilding FAISS index from scratch...")
        with self.lock:
            self.index.reset()
            cursor = self.conn.cursor()
            cursor.execute("SELECT id, content FROM facts WHERE server_id = ? ORDER BY id", (self.server_id,))
            all_facts_data = cursor.fetchall()

            if all_facts_data:
                np = _get_numpy()
                ids = np.array([row[0] for row in all_facts_data])
                contents = [row[1] for row in all_facts_data]
                embeddings = self.embedder.encode(contents)
                self.index.add_with_ids(embeddings, ids)

        log.info("Index rebuilt", extra={"vectors": self.index.ntotal})

    def close(self):
        """Close the database connection and save the index."""
        self.save_index()
        if self.conn:
            with self.lock:
                self.conn.close()
                log.info("Database connection closed.")


class GrugServerManager:
    """Manages separate GrugDB instances for each Discord server."""

    def __init__(self, base_db_path, model_name="all-MiniLM-L6-v2", load_embedder: bool = True):
        self.base_db_path = base_db_path
        self.model_name = model_name
        self.load_embedder = load_embedder
        self.server_dbs = {}
        self.lock = threading.Lock()
        log.info("Grug server manager initialized", extra={"base_path": base_db_path, "load_embedder": load_embedder})

    def get_server_db(self, server_id) -> GrugDB:
        """Get or create a GrugDB instance for a specific server."""
        server_id = str(server_id) if server_id else "dm"  # Handle DMs

        with self.lock:
            if server_id not in self.server_dbs:
                log.info("Creating new Grug brain for server", extra={"server_id": server_id})
                self.server_dbs[server_id] = GrugDB(self.base_db_path, self.model_name, server_id, self.load_embedder)
            return self.server_dbs[server_id]

    def close_all(self):
        """Close all server database connections."""
        with self.lock:
            for server_id, db in self.server_dbs.items():
                log.info("Closing Grug brain for server", extra={"server_id": server_id})
                db.close()
            self.server_dbs.clear()
        log.info("All Grug brains closed")

    def get_server_stats(self) -> dict:
        """Get statistics about all server databases."""
        stats = {}
        with self.lock:
            for server_id, db in self.server_dbs.items():
                try:
                    facts = db.get_all_facts()
                    stats[server_id] = {"fact_count": len(facts), "index_vectors": db.index.ntotal if db.index else 0}
                except Exception as e:
                    stats[server_id] = {"error": str(e)}
        return stats

    def migrate_global_facts_to_server(self, target_server_id: str = "global"):
        """Migrate facts without server_id to a specific server."""
        with self.lock:
            try:
                # Find facts without server_id (old format)
                with contextlib.closing(sqlite3.connect(self.base_db_path)) as migration_conn:
                    cursor = migration_conn.cursor()
                    cursor.execute("SELECT id, content FROM facts WHERE server_id IS NULL OR server_id = ''")
                    old_facts = cursor.fetchall()

                    if old_facts:
                        log.info(f"Migrating {len(old_facts)} global facts to server {target_server_id}")
                        # Update them to belong to the target server
                        cursor.execute(
                            "UPDATE facts SET server_id = ? WHERE server_id IS NULL OR server_id = ''",
                            (target_server_id,),
                        )
                        migration_conn.commit()
                        log.info("Migration completed successfully")
                    else:
                        log.info("No global facts found to migrate")
            except Exception as e:
                log.error("Error migrating global facts", extra={"error": str(e)})


if __name__ == "__main__":
    # Example usage and migration from grug_lore.json
    logging.basicConfig(level=logging.INFO)
    log.info("Running GrugDB standalone for migration...")

    db = GrugDB(db_path="grug_lore.db")

    # Migrate from old JSON file if it exists
    json_lore_path = "grug_lore.json"
    if os.path.exists(json_lore_path):
        log.info("Found old lore file, attempting migration", extra={"path": json_lore_path})
        import json

        try:
            with open(json_lore_path, "r") as f:
                lore_data = json.load(f)
                facts = lore_data.get("facts", [])
                migrated_count = 0
                for fact in facts:
                    if db.add_fact(fact):
                        migrated_count += 1
                log.info("Migration complete", extra={"migrated": migrated_count, "total": len(facts)})
                # Rename the old file to prevent re-migration
                os.rename(json_lore_path, json_lore_path + ".migrated")
                log.info("Renamed old lore file to avoid re-migration")
        except Exception as e:
            log.error("Error during migration", extra={"error": str(e)})

    # Test search
    print("\n--- Testing Search ---")
    test_query = "what grug think of ugga?"
    results = db.search_facts(test_query)
    print(f"Search results for: '{test_query}'")
    for res in results:
        print(f" - {res}")

    # Close connection
    db.close()
