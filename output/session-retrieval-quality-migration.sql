-- ============================================================================
-- session-retrieval-quality-001
-- ADDITIVE migration on top of output/session-postgres-schema.sql AND
-- output/session-chunk-indexer-migration.sql.
--
-- STATUS: experimental — applied and verified against a real Postgres
-- instance (pgvector/pgvector:pg16) as part of this job. See
-- output/session-retrieval-quality-report.md for the claim-evidence matrix.
--
-- This file does NOT replace or rewrite the two prior schema files. It is
-- applied THIRD, after both, against the same instance, using
-- CREATE OR REPLACE FUNCTION (the functions are defined LANGUAGE sql/plpgsql
-- so this is a metadata-only redefinition, no table rewrite, no data loss).
--
-- Two fixes, both proven necessary by actual function-call output against a
-- real fixture (see report "Claim-Evidence Matrix" for the exact quoted
-- before/after psql output):
--
--   1. session_api.search_context(): plainto_tsquery('english', p_query) ->
--      plainto_tsquery('simple', p_query). Proven broken even for an EXACT
--      English word ("deployment" stems to "deploy" on the 'english' query
--      side but chunk_indexer's to_tsvector('simple', ...) stores the
--      literal "deployment" token — never stemmed — so the two sides never
--      match), and broken for the stemming-sensitive case the job set out
--      to test ("run" vs. "running"). 'simple' was chosen over making
--      chunk_indexer stem with 'english' because the session corpus is
--      explicitly bilingual (Hungarian + English, per CLAUDE.md's
--      convention and chunk_indexer.py's own to_tsvector('simple', ...)
--      comment) — running the 'english' tsvector/tsquery config against
--      Hungarian content actively corrupts tokens (e.g. to_tsvector(
--      'english', 'futás közben') yields the lexeme 'futá', dropping the
--      's', because the English stemmer mis-applies suffix-stripping rules
--      to a word it was never designed for). 'simple' has no stemming on
--      either side for any language, so it is the only config that is
--      uniformly correct (if exact-match-only) across the mixed corpus.
--      This is an accepted experimental-status tradeoff: stemmed retrieval
--      quality for English-only content is intentionally NOT pursued here
--      (see report "Risks" / "Rejected / Out Of Scope") — a language-aware
--      per-chunk tsvector config would need to be its own job.
--
--   2. session_api.session_status(): the pending_jobs subquery matched
--      ONLY via payload->>'event_id' IN (session_raw.envelopes.event_id),
--      which the 'project_envelope' outbox payload has but the 'index_turn'
--      outbox payload (session_id/turn_seq only, no event_id — see
--      session_core.enqueue_chunk_indexing_job() in
--      output/session-chunk-indexer-migration.sql) does NOT. Proven to
--      undercount: a real pending index_turn outbox row produced
--      pending_jobs = 0. Fixed by joining session_jobs.outbox directly via
--      job_type-aware source_id resolution instead of the event_id-only
--      payload lookup, so it works correctly for BOTH outbox job_types
--      currently in use (and any future job_type whose source_table is
--      session_raw.envelopes or session_core.turns, since both are resolved
--      to the same session_id via an explicit UNION rather than a single
--      payload-shape assumption).
-- ============================================================================


-- ============================================================================
-- 1. search_context(): align the tsquery side with chunk_indexer's tsvector
--    side. Both 'simple' now — see rationale above.
-- ============================================================================

CREATE OR REPLACE FUNCTION session_api.search_context(
    p_session_id UUID,
    p_query TEXT,
    p_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    chunk_id BIGINT,
    turn_id BIGINT,
    text TEXT,
    rank REAL
) AS $$
    SELECT c.chunk_id, c.turn_id, c.text,
           ts_rank(f.tsv, plainto_tsquery('simple', p_query)) AS rank
    FROM session_core.chunks c
    JOIN session_idx.chunk_fts f ON f.chunk_id = c.chunk_id
    WHERE c.session_id = p_session_id
      AND f.tsv @@ plainto_tsquery('simple', p_query)
    ORDER BY rank DESC
    LIMIT p_limit;
$$ LANGUAGE sql STABLE;

COMMENT ON FUNCTION session_api.search_context(UUID, TEXT, INTEGER) IS
    'Uses plainto_tsquery(''simple'', ...) (corrected from ''english'' by '
    'session-retrieval-quality-001 — see output/session-retrieval-quality-report.md '
    '"Decisions Proposed") to match chunk_indexer''s to_tsvector(''simple'', ...) '
    'indexing config. ''simple'' chosen over making both sides ''english'' '
    'because the session corpus is bilingual (Hungarian + English) and the '
    '''english'' stemmer corrupts Hungarian tokens. Trade-off: no stemming on '
    'either language — exact-token search only, documented as an accepted '
    'experimental-status limitation, not a future-proof retrieval-quality fix.';


-- ============================================================================
-- 2. session_status(): pending_jobs computed via a job_type-aware union
--    instead of an event_id-only payload lookup, so it counts pending/
--    failed outbox rows for BOTH 'project_envelope' (session_raw.envelopes,
--    matched by provider_session_id) AND 'index_turn' (session_core.turns,
--    matched by session_id) outbox job_types correctly.
-- ============================================================================

CREATE OR REPLACE FUNCTION session_api.session_status(
    p_session_id UUID
)
RETURNS TABLE (
    session_id UUID,
    status TEXT,
    started_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    pending_jobs BIGINT
) AS $$
    SELECT s.session_id, s.status, s.started_at, s.last_seen_at,
           (
               -- 'project_envelope' outbox rows: source_id is
               -- session_raw.envelopes.id; resolve to this session via
               -- provider_session_id (matches the original lookup's intent,
               -- but joins on the envelope's own id rather than relying on
               -- a payload->>'event_id' round-trip).
               (SELECT count(*) FROM session_jobs.outbox o
                  JOIN session_raw.envelopes e ON e.id = o.source_id
                  WHERE o.job_type = 'project_envelope'
                    AND o.status IN ('pending', 'failed')
                    AND e.provider_session_id = s.provider_session_id)
               +
               -- 'index_turn' outbox rows: source_id is
               -- session_core.turns.turn_id; resolve to this session
               -- directly via session_core.turns.session_id (no event_id
               -- in this job_type's payload at all — see
               -- session_core.enqueue_chunk_indexing_job(), this is the
               -- gap proven by this job's test suite).
               (SELECT count(*) FROM session_jobs.outbox o
                  JOIN session_core.turns t ON t.turn_id = o.source_id
                  WHERE o.job_type = 'index_turn'
                    AND o.status IN ('pending', 'failed')
                    AND t.session_id = s.session_id)
           ) AS pending_jobs
    FROM session_core.sessions s
    WHERE s.session_id = p_session_id;
$$ LANGUAGE sql STABLE;

COMMENT ON FUNCTION session_api.session_status(UUID) IS
    'pending_jobs is computed as an explicit per-job_type union (corrected by '
    'session-retrieval-quality-001 — see output/session-retrieval-quality-report.md '
    '"Decisions Proposed") instead of a single payload->>''event_id'' lookup, '
    'because the ''index_turn'' outbox job_type''s payload (session_id/turn_seq '
    'only, see session_core.enqueue_chunk_indexing_job()) never contains an '
    '''event_id'' key, which made the original lookup permanently undercount '
    'pending index_turn jobs (proven: a real pending index_turn row produced '
    'pending_jobs = 0). Adding a new outbox job_type in the future requires '
    'adding a matching branch here — this is a known limitation of the '
    'job_type-aware-union approach, documented in the report "Risks".';


-- ============================================================================
-- Rollback note (no migration framework wired in for this job, same as the
-- two prior schema files). To roll back this migration on a scratch
-- instance, re-apply the ORIGINAL function bodies from
-- output/session-postgres-schema.sql (lines ~326-345 for search_context,
-- ~381-399 for session_status) via CREATE OR REPLACE FUNCTION.
-- ============================================================================
