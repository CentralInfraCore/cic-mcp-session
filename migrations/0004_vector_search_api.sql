-- ============================================================================
-- session-vector-search-api-001
-- ADDITIVE migration on top of output/session-postgres-schema.sql,
-- output/session-chunk-indexer-migration.sql, and
-- output/session-retrieval-quality-migration.sql.
--
-- STATUS: experimental — applied and verified against a real Postgres
-- instance (pgvector/pgvector:pg16) as part of this job. See
-- output/session-vector-search-api-report.md for the claim-evidence matrix.
--
-- This file does NOT replace or rewrite any of the three prior schema/
-- migration files. It is applied FOURTH, after all three, against the same
-- instance.
--
-- One change: a single new session_api function,
-- session_api.search_context_vector(), the FIRST function in this codebase
-- that ever queries session_idx.chunk_embeddings. It accepts a READY
-- VECTOR(384) parameter (NOT text) — the text->vector conversion happens in
-- Python (session_store/vector_search.py:embed_query(), which reuses
-- session_store/chunk_indexer.py:embed_texts(); see that module's docstring
-- for the full rationale: no local embedding model load/call is possible
-- from plpgsql/sql).
--
-- Cosine distance is computed via pgvector's `<=>` operator against
-- session_idx.chunk_embeddings.embedding, ordered ascending (smaller
-- distance = more similar), which is exactly the operator
-- idx_session_idx_chunk_embeddings_hnsw (USING hnsw (embedding
-- vector_cosine_ops), output/session-postgres-schema.sql:255-256) was built
-- to accelerate — vector_cosine_ops indexes the `<=>` operator specifically,
-- per pgvector's documented operator/opclass pairing. The `similarity`
-- output column is `1 - cosine_distance` (cosine similarity, higher =
-- better), so callers see a "higher is better" score consistent with
-- search_context()'s existing `rank` column convention, while the ORDER BY
-- itself still sorts on the raw `<=>` distance expression (NOT on the
-- derived `similarity` alias) — this is deliberate: ordering directly on
-- the `<=>` expression is what allows the planner to recognize the HNSW
-- index's native distance-ordering scan; wrapping it in 1-distance before
-- the ORDER BY would obscure that operator from the planner's index-scan
-- matching. See report "EXPLAIN" section for whether the planner actually
-- chooses the HNSW index scan at this fixture's row count, or falls back to
-- a sequential scan (and why that is an accepted, documented outcome at
-- small N, not a bug).
-- ============================================================================

CREATE OR REPLACE FUNCTION session_api.search_context_vector(
    p_session_id UUID,
    p_query_embedding VECTOR(384),
    p_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    chunk_id BIGINT,
    turn_id BIGINT,
    text TEXT,
    similarity REAL
) AS $$
    SELECT c.chunk_id, c.turn_id, c.text,
           (1 - (e.embedding <=> p_query_embedding))::REAL AS similarity
    FROM session_core.chunks c
    JOIN session_idx.chunk_embeddings e ON e.chunk_id = c.chunk_id
    WHERE c.session_id = p_session_id
    ORDER BY e.embedding <=> p_query_embedding
    LIMIT p_limit;
$$ LANGUAGE sql STABLE;

COMMENT ON FUNCTION session_api.search_context_vector(UUID, VECTOR(384), INTEGER) IS
    'Cosine-similarity vector search over session_idx.chunk_embeddings, added '
    'by session-vector-search-api-001 — the FIRST function in this codebase '
    'that ever queries chunk_embeddings (see '
    'output/session-vector-search-api-report.md). Takes a READY VECTOR(384) '
    'parameter, NOT text — callers must produce the query embedding '
    'themselves via session_store/vector_search.py:embed_query(), which '
    'reuses session_store/chunk_indexer.py:embed_texts() (same local '
    'sentence-transformers model, NOT a new model load). Orders by the raw '
    '`<=>` cosine-distance expression (not the derived `similarity` column) '
    'so the planner can match it against '
    'idx_session_idx_chunk_embeddings_hnsw (USING hnsw (embedding '
    'vector_cosine_ops)) when row counts make an index scan worthwhile — '
    'see report "EXPLAIN" section for whether that happens at this job''s '
    'fixture size. Does NOT filter/blend with FTS rank (session_api.'
    'search_context''s `simple`-config text search) — hybrid FTS+vector '
    'ranking is explicitly out of scope for this job, see input.md "Nem '
    'cél".';


-- ============================================================================
-- Rollback note (no migration framework wired in for this job, same as the
-- three prior schema/migration files). To roll back this migration on a
-- scratch instance:
-- ============================================================================
--
-- DROP FUNCTION IF EXISTS session_api.search_context_vector(UUID, VECTOR(384), INTEGER);
