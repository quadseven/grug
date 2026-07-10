"""Database layer tests with FAISS mocking."""

import importlib.util
import pickle
import sys
import types

import numpy as np
import pytest

# Provide a minimal faiss stub if faiss is unavailable
if "faiss" not in sys.modules:
    fake_faiss = types.ModuleType("faiss")
    # This is the important part to make the mock compatible with transformers' import checks.
    fake_faiss.__spec__ = importlib.util.spec_from_loader("faiss", loader=None)

    class IndexFlatL2:
        def __init__(self, dim):
            self.dim = dim
            self.vectors = np.empty((0, dim), dtype=np.float32)

        def add(self, vecs):
            self.vectors = np.vstack([self.vectors, vecs]).astype(np.float32)

        def reset(self):
            self.vectors = np.empty((0, self.dim), dtype=np.float32)

        def search(self, queries, k):
            if len(self.vectors) == 0:
                dists = np.zeros((len(queries), k), dtype=np.float32)
                idx = -np.ones((len(queries), k), dtype=np.int64)
                return dists, idx
            dists = np.linalg.norm(self.vectors[None, :, :] - queries[:, None, :], axis=2)
            idx = np.argsort(dists, axis=1)[:, :k]
            dist = np.take_along_axis(dists, idx, axis=1)
            return dist.astype(np.float32), idx.astype(np.int64)

    class IndexIDMap:
        def __init__(self, index):
            self.index = index
            self.ids = np.array([], dtype=np.int64)

        @property
        def ntotal(self):
            return len(self.ids)

        def add_with_ids(self, embeddings, ids):
            self.index.add(embeddings)
            self.ids = np.concatenate([self.ids, ids])

        def search(self, queries, k):
            dist, idx = self.index.search(queries, k)
            mapped = np.full_like(idx, -1)
            for r, row in enumerate(idx):
                for c, val in enumerate(row):
                    if 0 <= val < len(self.ids):
                        mapped[r, c] = self.ids[val]
            return dist, mapped

        def reset(self):
            self.index.reset()
            self.ids = np.array([], dtype=np.int64)

    def write_index(index, path):
        with open(path, "wb") as f:
            pickle.dump(index, f)

    def read_index(path):
        with open(path, "rb") as f:
            return pickle.load(f)

    fake_faiss.IndexFlatL2 = IndexFlatL2
    fake_faiss.IndexIDMap = IndexIDMap
    fake_faiss.write_index = write_index
    fake_faiss.read_index = read_index
    sys.modules["faiss"] = fake_faiss

from src.grugthink.grug_db import GrugDB


@pytest.fixture(scope="function")
def db_instance(tmp_path):
    # Setup: Create a temporary directory for the database files
    test_db_dir = tmp_path / "grug_test_db"
    test_db_dir.mkdir()
    test_db_path = str(test_db_dir / "test_grug_lore.db")
    test_server_id = "test_server"

    db = GrugDB(test_db_path, server_id=test_server_id)
    yield db

    # Teardown: Close the database and remove the test files
    db.close()
    # tmp_path fixture handles cleanup of the directory


def test_add_fact(db_instance):
    assert db_instance.add_fact("Grug like big rock.")
    assert not db_instance.add_fact("Grug like big rock.")  # Test adding duplicate
    facts = db_instance.get_all_facts()
    assert "Grug like big rock." in facts
    assert len(facts) == 1


def test_add_fact_rollback_on_index_failure(db_instance, monkeypatch):
    """Ensure DB insert is rolled back if indexing fails."""
    # This test only applies when semantic search is available
    if not (
        hasattr(db_instance, "embedder")
        and db_instance.embedder is not None
        and hasattr(db_instance, "index")
        and db_instance.index is not None
    ):
        pytest.skip("Semantic search not available, rollback test not applicable")

    def fail(*args, **kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(db_instance.index, "add_with_ids", fail)

    assert not db_instance.add_fact("Bad fact")
    assert "Bad fact" not in db_instance.get_all_facts()


def test_search_facts(db_instance):
    db_instance.add_fact("Grug hunt mammoth.")
    db_instance.add_fact("Ugga make good fire.")
    db_instance.add_fact("Bork find shiny stone.")
    db_instance.add_fact("Grug think sky is blue.")

    results = db_instance.search_facts("what grug hunt?", k=1)

    # If semantic search is available, we should get results
    # If not available (CI environment), search returns empty list
    if hasattr(db_instance, "embedder") and db_instance.embedder is not None:
        assert "Grug hunt mammoth." in results
        assert len(results) == 1
    else:
        # In CI environment without sentence-transformers, search is disabled
        assert results == []

    if hasattr(db_instance, "embedder") and db_instance.embedder is not None:
        results = db_instance.search_facts("who make fire?", k=1)
        assert "Ugga make good fire." in results

        results = db_instance.search_facts("what bork find?", k=1)
        assert "Bork find shiny stone." in results

        results = db_instance.search_facts("color of sky?", k=1)
        assert "Grug think sky is blue." in results


def test_get_all_facts(db_instance):
    db_instance.add_fact("Fact one.")
    db_instance.add_fact("Fact two.")
    facts = db_instance.get_all_facts()
    assert len(facts) == 2
    assert "Fact one." in facts
    assert "Fact two." in facts


def test_rebuild_index(db_instance):
    db_instance.add_fact("Fact for rebuild.")
    initial_ntotal = db_instance.index.ntotal
    db_instance.rebuild_index()
    assert db_instance.index.ntotal == initial_ntotal


def test_db_close(db_instance, tmp_path):
    # The fixture handles closing, but we can test if it doesn't raise an error
    # when called explicitly (though it's usually called once in teardown)
    db_instance.close()
    # Re-initializing to ensure it can be opened again after close
    new_db = GrugDB(str(tmp_path / "test_grug_lore_new.db"))
    assert new_db is not None
    new_db.close()


def test_invalid_db_path():
    # Create a temporary file, then try to create a database path that would require
    # creating a directory inside this file (which should fail)
    import tempfile

    with tempfile.NamedTemporaryFile() as f:
        invalid_path = f.name + "/subdir/db.sqlite"
        with pytest.raises(Exception):
            GrugDB(invalid_path)
