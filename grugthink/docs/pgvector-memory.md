# Grug memory on Postgres + pgvector

## Why

Grug's long-term memory was a per-server SQLite file plus a per-server FAISS
index, both living on a node-pinned local-path PVC. That has three problems:

1. **Node pinning.** local-path binds the data to one worker; the pod cannot be
   rescheduled without losing (or stranding) its brain.
2. **Two stores to keep in sync.** Every write had to insert a SQLite row *and*
   add the vector to the FAISS index inside one transaction; a delete had to
   rebuild the whole index. Drift between the two silently corrupts recall.
3. **A fragile native dependency.** FAISS needs `libgomp` at runtime and only
   ships aarch64 wheels from 1.8.0; a missing OpenMP or a wrong wheel disables
   vector search with no error the user ever sees.

We already run a CloudNativePG Postgres (the same cluster grug's persona/install
store uses, `postgres-rw.databases.svc.cluster.local`, PG 18, pgvector 0.8.2
available) and the macchina project already stores recipe/nutrient embeddings in
it with pgvector. Grug's memory should use the same store the same way.

## What

Replace SQLite+FAISS with a single pgvector-backed table. One row per fact holds
both the text and its embedding, so there is exactly one store and no sync step.

- Table `grugthink_facts (id, namespace, server_id, content, embedding vector(768), created_at)`.
- `namespace` = the bot's base db path (stable per bot instance) so multiple bots
  and multiple Discord servers stay isolated exactly as the separate files did.
- Retrieval is `ORDER BY embedding <=> $q LIMIT k` (cosine distance), the same
  operator and 768-dim `nomic-embed-text:v1.5` model macchina uses.
- Embeddings come from the owned spark-gateway `OllamaEmbedder` (unchanged, and
  already the only embedding path in the light image).

## Public API is unchanged

`GrugServerManager(base_db_path, load_embedder).get_server_db(server_id)` returns
an object exposing `add_fact`, `search_facts`, `get_all_facts`, `delete_fact`,
`close`. Callers (bot RAG in `bot/prompts.py`, `bot/lore.py`, the memories API
router) do not change. The backend is selected at construction time:

- If `GRUGTHINK_DATABASE_URL` (or `GRUG_DATABASE_URL`) is set -> pgvector backend.
- Otherwise -> the legacy SQLite+FAISS backend (dev/tests without a Postgres).

## Invariants (mirrors macchina's NO FAKE DATA posture)

- `EMBED_DIM = 768` is the single source of truth the DDL and every insert read;
  a vector of any other length is rejected, never stored.
- A fact whose embedding could not be produced is inserted with `embedding NULL`
  and is excluded from semantic search (`WHERE embedding IS NOT NULL`) rather
  than stored as a zero vector that would poison every cosine distance.
- Schema bootstrap (`CREATE EXTENSION IF NOT EXISTS vector` + `CREATE TABLE IF
  NOT EXISTS` + indexes) is idempotent and runs once per process.

## Migration

Grug's existing facts are read out via the memories API before cutover and
re-inserted after the pgvector backend is live (they re-embed on insert). No
boot-time SQLite reader is added, keeping the two backends decoupled.

## Rollout

1. Ship the backend behind the env selector (this PR); CI runs the grugthink
   suite against a `pgvector/pgvector:pg18` service so the vector path is tested.
2. Wire `GRUGTHINK_DATABASE_URL` into the deployment from the existing
   `grug-secrets` (`GRUG_DATABASE_URL`).
3. Re-seed the family facts, verify semantic recall.
4. Follow-up: drop the local-path PVC and the FAISS/`libgomp` dependency.
