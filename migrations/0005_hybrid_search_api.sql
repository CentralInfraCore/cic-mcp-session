-- ============================================================================
-- session-hybrid-search-api-001
-- ADDITIVE migration on top of output/session-postgres-schema.sql,
-- output/session-chunk-indexer-migration.sql,
-- output/session-retrieval-quality-migration.sql, and
-- output/session-vector-search-api-migration.sql.
--
-- STATUS: experimental — applied and verified against a real Postgres
-- instance (pgvector/pgvector:pg16) as part of this job. See
-- output/session-hybrid-search-api-report.md for the claim-evidence matrix.
--
-- This file does NOT replace or rewrite any of the four prior schema/
-- migration files. It is applied FIFTH, after all four, against the same
-- instance.
--
-- One new session_api function: session_api.search_context_hybrid(), the
-- FIRST function in this codebase that combines the FTS signal
-- (session_api.search_context()'s plainto_tsquery('simple', ...) /
-- ts_rank expression) and the vector signal
-- (session_api.search_context_vector()'s cosine-distance `<=>` expression)
-- into a single ranking.
--
-- ----------------------------------------------------------------------------
-- Fusion method: Reciprocal Rank Fusion (RRF), NOT a weighted sum.
-- ----------------------------------------------------------------------------
--
-- THE SCALE-MISMATCH PROBLEM (must be addressed explicitly, see input.md
-- "2." / "Forbidden Shortcuts"): ts_rank() (FTS side) and cosine similarity
-- (vector side, `1 - (embedding <=> query_embedding)`) are NOT on comparable
-- scales:
--   - ts_rank() is an unbounded, corpus/term-frequency-dependent score
--     (driven by lexeme weight and document length normalization — see
--     PostgreSQL docs, "12.3.3. Ranking Search Results"). Its numeric range
--     has no fixed upper bound and no natural interpretation as "0..1
--     relevance" — two chunks with very different rank values are not
--     necessarily "twice as relevant", and the same rank value means
--     different things across different queries.
--   - cosine similarity (`1 - cosine_distance`) is bounded to [-1, 1] (in
--     practice usually [0, 1] for this model's normalized embeddings), and
--     IS a fixed geometric quantity — but it is on a totally different
--     numeric scale and distribution than ts_rank(): an FTS rank of, say,
--     0.06 (a typical ts_rank() value for a single-term match — see this
--     job's own "Findings" for the actual values observed) is NOT
--     comparable to a cosine similarity of 0.06 by any principled
--     conversion. There is no shared unit between "how much a term's tf/idf-
--     like weight contributes to a tsvector match" and "how close two
--     embedding vectors are in 384-dimensional space".
--
-- REJECTED ALTERNATIVE — naive weighted sum (e.g.
-- `0.5 * ts_rank + 0.5 * similarity`): explicitly rejected per input.md
-- "Forbidden Shortcuts". A weighted sum silently assumes the two scores are
-- numerically commensurate (i.e. that "0.5 points of ts_rank" and "0.5
-- points of cosine similarity" represent equivalent amounts of relevance).
-- They do not — ts_rank()'s scale shifts with corpus statistics and query
-- term count, while cosine similarity's scale is fixed by the embedding
-- model's geometry. Any single fixed weight pair (e.g. 0.5/0.5) chosen
-- today could be silently wrong tomorrow if the corpus grows or the query
-- shape changes (more/fewer terms), because ts_rank()'s typical magnitude
-- is not stable in the way cosine similarity's is. Normalizing each score
-- to [0,1] via per-query min-max scaling was also considered and rejected:
-- with only 1-2 result rows (the common case for a single small session),
-- min-max normalization degenerates (the top result is always rescaled to
-- 1.0 and the only other result to 0.0, regardless of the actual score
-- gap), which is a worse, not better, approximation than ignoring the raw
-- scores entirely.
--
-- CHOSEN METHOD — Reciprocal Rank Fusion (RRF): each side's RESULT IS FIRST
-- REDUCED TO A RANK (1st place, 2nd place, ...) within that side's own
-- result set, discarding the raw score entirely. The fused score per chunk
-- is the SUM of `1 / (k + rank)` over whichever side(s) returned that
-- chunk (a chunk missing from one side simply does not contribute that
-- side's term — it is NOT given a rank or a zero score, it is omitted from
-- that side's sum term). This sidesteps the scale-mismatch problem
-- completely: rank position is dimensionless and means the same thing
-- ("how many other results out-scored this one, on this side") regardless
-- of whether the underlying score was a ts_rank() value or a cosine
-- similarity value. k = 60, the commonly cited RRF constant from the
-- original Cormack/Clarke/Buettcher TREC fusion work (a smoothing constant
-- that flattens the score curve so rank 1 vs. rank 2 is not a huge jump
-- relative to rank 50 vs. rank 51); this job does NOT tune k against real
-- session data (see input.md "Nem cél" — k-tuning is explicitly out of
-- scope), it only fixes a documented, literature-precedented default.
--
-- IMPLEMENTATION CHOICE — pure SQL, not SQL+Python: RRF's "reduce to rank,
-- then sum 1/(k+rank)" computation is expressible entirely in standard SQL
-- via two CTEs (one per side, each producing its own ROW_NUMBER()-based
-- rank over its own ORDER BY) followed by a FULL OUTER JOIN keyed on
-- chunk_id and a COALESCE-guarded sum. No Python-side fusion step is
-- needed (unlike, say, a fusion method that needed array sorting not
-- expressible in SQL) — see input.md "3." ("ha a fuggveny nem tisztan
-- SQL-ben oldhato meg" — it IS solvable purely in SQL here, so no Python
-- helper was added for the fusion step itself; the EXISTING Python helper
-- session_store/vector_search.py:embed_query()/to_pgvector_literal() is
-- still required by CALLERS to produce the p_query_embedding parameter,
-- exactly as it already was for search_context_vector()).
-- ============================================================================

CREATE OR REPLACE FUNCTION session_api.search_context_hybrid(
    p_session_id UUID,
    p_query TEXT,
    p_query_embedding VECTOR(384),
    p_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    chunk_id BIGINT,
    turn_id BIGINT,
    text TEXT,
    fused_score DOUBLE PRECISION
) AS $$
    WITH
    -- FTS side: REUSES session_api.search_context()'s exact query
    -- expression (plainto_tsquery('simple', p_query) against
    -- session_idx.chunk_fts.tsv, ts_rank() for ordering) — not
    -- re-implemented. No LIMIT here (unlike search_context() itself):
    -- the hybrid function needs each side's FULL ranked list to compute
    -- ranks correctly before any final LIMIT is applied at the end, so
    -- truncating a side early would silently drop chunks that the OTHER
    -- side might still need ranked against.
    fts_matches AS (
        SELECT c.chunk_id, c.turn_id, c.text,
               ts_rank(f.tsv, plainto_tsquery('simple', p_query)) AS rank_score
        FROM session_core.chunks c
        JOIN session_idx.chunk_fts f ON f.chunk_id = c.chunk_id
        WHERE c.session_id = p_session_id
          AND f.tsv @@ plainto_tsquery('simple', p_query)
    ),
    fts_ranked AS (
        SELECT chunk_id, turn_id, text,
               ROW_NUMBER() OVER (ORDER BY rank_score DESC) AS rrf_rank
        FROM fts_matches
    ),
    -- Vector side: REUSES session_api.search_context_vector()'s exact
    -- query expression (cosine distance `<=>` against
    -- session_idx.chunk_embeddings.embedding, ordered ascending on the
    -- raw `<=>` expression so the planner can still match the HNSW
    -- index) — not re-implemented. Every chunk in the session has a
    -- vector-side rank (no WHERE filter beyond session_id), unlike the
    -- FTS side which only ranks chunks that satisfy the tsquery match.
    vector_matches AS (
        SELECT c.chunk_id, c.turn_id, c.text,
               (1 - (e.embedding <=> p_query_embedding))::REAL AS similarity
        FROM session_core.chunks c
        JOIN session_idx.chunk_embeddings e ON e.chunk_id = c.chunk_id
        WHERE c.session_id = p_session_id
    ),
    vector_ranked AS (
        SELECT chunk_id, turn_id, text,
               ROW_NUMBER() OVER (ORDER BY similarity DESC) AS rrf_rank
        FROM vector_matches
    ),
    -- RRF fusion: FULL OUTER JOIN on chunk_id so a chunk present on only
    -- one side still contributes that side's 1/(k+rank) term; a chunk
    -- absent from a side contributes 0 for that side (COALESCE), NOT a
    -- synthetic worst-case rank — this is what makes RRF tolerant of one
    -- side returning zero matches (e.g. an FTS query with no lexical hits
    -- at all still produces a fully ranked hybrid result from the vector
    -- side alone).
    fused AS (
        SELECT
            COALESCE(f.chunk_id, v.chunk_id) AS chunk_id,
            COALESCE(f.turn_id, v.turn_id) AS turn_id,
            COALESCE(f.text, v.text) AS text,
            COALESCE(1.0 / (60 + f.rrf_rank), 0.0)
            + COALESCE(1.0 / (60 + v.rrf_rank), 0.0) AS fused_score
        FROM fts_ranked f
        FULL OUTER JOIN vector_ranked v ON v.chunk_id = f.chunk_id
    )
    SELECT chunk_id, turn_id, text, fused_score
    FROM fused
    ORDER BY fused_score DESC
    LIMIT p_limit;
$$ LANGUAGE sql STABLE;

COMMENT ON FUNCTION session_api.search_context_hybrid(UUID, TEXT, VECTOR(384), INTEGER) IS
    'Reciprocal Rank Fusion (RRF, k=60) over session_api.search_context()''s '
    'FTS expression (plainto_tsquery(''simple'', ...) / ts_rank()) and '
    'session_api.search_context_vector()''s cosine-distance expression '
    '(embedding <=> query_embedding), added by session-hybrid-search-api-001 '
    '(see output/session-hybrid-search-api-report.md). Neither side''s query '
    'expression is reimplemented here — both CTEs reuse the existing '
    'functions'' SQL bodies verbatim. RRF was chosen over a naive weighted '
    'sum of ts_rank and cosine similarity because the two scores are not on '
    'comparable scales (ts_rank is an unbounded, corpus-dependent score; '
    'cosine similarity is a fixed geometric quantity in [-1,1]) — RRF '
    'sidesteps this by fusing RANK POSITIONS, not raw scores, so no '
    'cross-scale weighting decision is needed. k=60 is the commonly cited '
    'literature default (Cormack/Clarke/Buettcher), NOT tuned against real '
    'session data — k-tuning is explicitly out of scope for this job (see '
    'input.md "Nem cél"). A chunk missing from one side (e.g. zero lexical '
    'matches) contributes 0 for that side via COALESCE rather than a '
    'synthetic worst-rank penalty, so the hybrid function degrades '
    'gracefully to a single-side ranking when one side has no matches.';


-- ============================================================================
-- Rollback note (no migration framework wired in for this job, same as the
-- four prior schema/migration files). To roll back this migration on a
-- scratch instance:
-- ============================================================================
--
-- DROP FUNCTION IF EXISTS session_api.search_context_hybrid(UUID, TEXT, VECTOR(384), INTEGER);
