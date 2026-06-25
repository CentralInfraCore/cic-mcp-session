-- ============================================================================
-- session-chunk-indexer-001
-- ADDITIVE migration on top of output/session-postgres-schema.sql.
--
-- STATUS: experimental — applied and verified against a real Postgres
-- instance (pgvector/pgvector:pg16) as part of this job. See
-- output/session-chunk-indexer-report.md for the claim-evidence matrix.
--
-- This file does NOT replace or rewrite session-postgres-schema.sql. It is
-- applied SECOND, after the existing schema, against the same instance.
--
-- Two changes:
--   1. session_idx.chunk_embeddings.embedding: VECTOR(1536) placeholder ->
--      VECTOR(384), matching the actual output dimension of the chosen
--      local embedding model (paraphrase-multilingual-MiniLM-L12-v2),
--      queried via a real model.encode() call, not assumed. See report
--      "Decisions Proposed" for the measurement and the ALTER vs.
--      recreate-table tradeoff discussion.
--   2. A new outbox job_type ('index_turn'), enqueued by an additive
--      AFTER INSERT trigger on session_core.turns, following the exact
--      pattern of trg_session_raw_envelopes_enqueue /
--      session_raw.enqueue_projection_job() in session-postgres-schema.sql.
-- ============================================================================


-- ============================================================================
-- 1. Embedding dimension correction (1536 placeholder -> 384, the real
--    output dimension of paraphrase-multilingual-MiniLM-L12-v2).
--
--    Why ALTER COLUMN ... TYPE and not a table recreate: at the time this
--    migration runs, session_idx.chunk_embeddings is guaranteed empty (no
--    worker has ever written to it — this is the first job that does), so
--    there is no data-loss/cast concern. pgvector's vector type supports a
--    straight ALTER COLUMN ... TYPE VECTOR(n) change; since the table is
--    empty, no USING expression/cast is needed and the change is effectively
--    a metadata-only rewrite. A full DROP+CREATE TABLE was considered and
--    rejected because it would also require re-creating the
--    idx_session_idx_chunk_embeddings_hnsw index and the FK to
--    session_core.chunks for no additional safety benefit over ALTER
--    COLUMN, given the table is empty.
-- ============================================================================

ALTER TABLE session_idx.chunk_embeddings
    ALTER COLUMN embedding TYPE VECTOR(384);

COMMENT ON COLUMN session_idx.chunk_embeddings.embedding IS
    'Embedding vector, dimension 384 (corrected from the original VECTOR(1536) '
    'placeholder by session-chunk-indexer-001 — see output/session-chunk-indexer-report.md '
    '"Decisions Proposed"). Dimension matches the actual model.encode() output of '
    'paraphrase-multilingual-MiniLM-L12-v2, queried directly, not assumed from '
    'documentation. embedding_model column records which model produced a given row, '
    'so a future model swap with a different dimension can be detected/migrated '
    'explicitly rather than silently mismatching.';


-- ============================================================================
-- 2. New outbox job_type: 'index_turn', enqueued by an AFTER INSERT trigger
--    on session_core.turns. Mirrors session_raw.enqueue_projection_job() /
--    trg_session_raw_envelopes_enqueue exactly: trigger ONLY performs a
--    synchronous, in-DB INSERT into session_jobs.outbox — NO network call,
--    NO LLM call, NO HTTP request. The actual chunking/FTS/embedding work
--    happens in the external session_store.chunk_indexer worker (this job),
--    which polls/consumes this outbox row, exactly like turn_projector does
--    for 'project_envelope'.
-- ============================================================================

CREATE OR REPLACE FUNCTION session_core.enqueue_chunk_indexing_job()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO session_jobs.outbox (job_type, source_table, source_id, payload)
    VALUES ('index_turn', 'session_core.turns', NEW.turn_id,
            jsonb_build_object('session_id', NEW.session_id, 'turn_seq', NEW.turn_seq));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_session_core_turns_enqueue_index
    AFTER INSERT ON session_core.turns
    FOR EACH ROW
    EXECUTE FUNCTION session_core.enqueue_chunk_indexing_job();


-- ============================================================================
-- Rollback note (no migration framework wired in for this job, same as
-- session-postgres-schema.sql — see CLAUDE.md "Nem cél" of the prior jobs).
-- To roll back this migration on a scratch instance:
-- ============================================================================
--
-- DROP TRIGGER IF EXISTS trg_session_core_turns_enqueue_index ON session_core.turns;
-- DROP FUNCTION IF EXISTS session_core.enqueue_chunk_indexing_job();
-- -- Reverting the column type requires chunk_embeddings to be empty again
-- -- (or an explicit USING cast plan), since this is a narrowing type change:
-- ALTER TABLE session_idx.chunk_embeddings ALTER COLUMN embedding TYPE VECTOR(1536);
