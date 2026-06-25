-- ============================================================================
-- session-source-refs-api-001
-- ADDITIVE migration on top of output/session-postgres-schema.sql,
-- output/session-chunk-indexer-migration.sql,
-- output/session-retrieval-quality-migration.sql,
-- output/session-vector-search-api-migration.sql, and
-- output/session-hybrid-search-api-migration.sql.
--
-- STATUS: experimental — applied and verified against a real Postgres
-- instance (pgvector/pgvector:pg16) as part of this job. See
-- output/session-source-refs-api-report.md for the claim-evidence matrix.
--
-- This file does NOT replace or rewrite any of the five prior schema/
-- migration files. It is applied SIXTH, after all five, against the same
-- instance.
--
-- One new session_api function: session_api.get_source_refs(), the FIRST
-- function in this codebase that reads session_core.source_refs (populated
-- by session_store/chunk_indexer.py:extract_source_refs(), job
-- session-chunk-indexer-001 — ref_kind in {'tool_call', 'file', 'url'}).
--
-- Until now NOTHING read source_refs through a session_api function — the
-- MCP server architecture rule (architecture.md: "Az MCP szerver ne
-- tablakat turkaljon. Stabil API fuggvenyeket hivjon") requires this to go
-- through session_api, never a direct session_core.source_refs query. This
-- migration closes that gap for the source_refs table specifically.
--
-- session_core.source_refs has NO direct session_id column (see
-- session-postgres-schema.sql:180-187) — it only has chunk_id (FK to
-- session_core.chunks). session_core.chunks DOES carry session_id directly
-- (session-postgres-schema.sql:167-178, populated by chunk_indexer.py), so
-- the session-scoping join in this function goes source_refs -> chunks
-- (not chunks -> turns -> sessions), one join hop, matching the same
-- session_id denormalization session_api.search_context() already relies
-- on (session-postgres-schema.sql:339-341).
-- ============================================================================

CREATE OR REPLACE FUNCTION session_api.get_source_refs(
    p_session_id UUID,
    p_ref_kind TEXT DEFAULT NULL,
    p_limit INTEGER DEFAULT 100
)
RETURNS TABLE (
    source_ref_id BIGINT,
    chunk_id BIGINT,
    turn_id BIGINT,
    ref_kind TEXT,
    ref_value TEXT,
    content_hash TEXT
) AS $$
    SELECT r.source_ref_id, r.chunk_id, c.turn_id, r.ref_kind, r.ref_value,
           r.content_hash
    FROM session_core.source_refs r
    JOIN session_core.chunks c ON c.chunk_id = r.chunk_id
    WHERE c.session_id = p_session_id
      AND (p_ref_kind IS NULL OR r.ref_kind = p_ref_kind)
    ORDER BY r.source_ref_id ASC
    LIMIT p_limit;
$$ LANGUAGE sql STABLE;

COMMENT ON FUNCTION session_api.get_source_refs(UUID, TEXT, INTEGER) IS
    'Returns provenance references (tool_call/file/url) for a session, '
    'scoped via source_refs.chunk_id -> chunks.session_id (source_refs has '
    'no direct session_id column). p_ref_kind=NULL returns all kinds; a '
    'non-NULL value filters to exactly that kind. session_id scoping is '
    'mandatory (always applied via the WHERE clause, never optional) — '
    'this is the cross-session data-leak guard required by input.md '
    '"Forbidden Shortcuts".';


-- ============================================================================
-- Rollback note (no migration framework wired in for this job, same as the
-- five prior schema/migration files). To roll back this migration on a
-- scratch instance:
-- ============================================================================
--
-- DROP FUNCTION IF EXISTS session_api.get_source_refs(UUID, TEXT, INTEGER);
