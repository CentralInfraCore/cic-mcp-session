-- ============================================================================
-- session-postgres-storage-design-001
-- PostgreSQL schema DRAFT for cic-mcp-session
--
-- STATUS: experimental / DESIGN DRAFT — NOT executed against a live Postgres
-- instance. No migration framework wired in. See
-- output/session-postgres-storage-design.md for rationale, claim-evidence
-- matrix and limitations.
--
-- Source of truth for session_raw column mapping:
--   output/session-ingress-envelope.schema.yaml (SessionIngressEnvelope)
--
-- Five schemas (architecture.md "Schema szeparacio"):
--   session_raw    - SessionIngressEnvelope 1:1 storage, raw provider payload
--   session_core   - sessions/turns/chunks/source_refs/manifests (projected)
--   session_idx    - FTS, vector refs (pgvector/HNSW), ranking features
--   session_jobs   - outbox/projection jobs, retry/dead-letter state
--   session_api    - stable SQL functions called by the MCP server
-- ============================================================================

-- ============================================================================
-- Extensions (required for vector + trigram FTS support)
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- trigram index support for fuzzy/substring search
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector, for session_idx embedding columns


-- ============================================================================
-- SCHEMA: session_raw
-- 1:1 mapping of SessionIngressEnvelope. Every required envelope field is
-- either a typed column or a JSONB sub-object. idempotency_key carries the
-- UNIQUE constraint that implements the dedup mechanism described in the
-- envelope schema ("a session_raw ingest MUST treat idempotency_key as a
-- unique constraint").
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS session_raw;

-- Enum-constrained fields are modeled as Postgres ENUM types rather than
-- CHECK(... IN (...)) so that invalid values are rejected at the type level,
-- mirroring the envelope's "ENUM, not free text" intent for trust/source.kind.
CREATE TYPE session_raw.trust_level AS ENUM ('session_local', 'session_derived');
CREATE TYPE session_raw.source_kind AS ENUM ('hook', 'importer', 'manual', 'api');
CREATE TYPE session_raw.payload_encoding AS ENUM ('json', 'text', 'base64');

CREATE TABLE session_raw.envelopes (
    -- internal surrogate key; NOT the envelope identity (event_id is)
    id                      BIGSERIAL PRIMARY KEY,

    -- top-level discriminators (envelope.apiVersion, envelope.kind)
    api_version             TEXT NOT NULL CHECK (api_version = 'cic.session/v1'),
    kind                    TEXT NOT NULL CHECK (kind = 'SessionIngressEnvelope'),

    -- event identity (envelope.event_id)
    event_id                UUID NOT NULL,

    -- provider identity (envelope.provider, envelope.provider_session_id,
    -- envelope.provider_event_name)
    provider                TEXT NOT NULL CHECK (length(provider) >= 1),
    provider_session_id     TEXT NOT NULL CHECK (length(provider_session_id) >= 1),
    provider_event_name     TEXT,  -- optional but recommended in the envelope schema

    -- source object (envelope.source.kind, envelope.source.collector)
    source_kind             session_raw.source_kind NOT NULL,
    source_collector        TEXT NOT NULL CHECK (length(source_collector) >= 1),

    -- timestamps (envelope.occurred_at, envelope.ingested_at)
    occurred_at             TIMESTAMPTZ NOT NULL,
    ingested_at             TIMESTAMPTZ NOT NULL,

    -- payload (envelope.payload, envelope.payload_encoding, envelope.raw_payload_hash)
    payload                 JSONB NOT NULL,
    payload_encoding        session_raw.payload_encoding NOT NULL DEFAULT 'json',
    raw_payload_hash        TEXT NOT NULL
        CHECK (raw_payload_hash ~ '^sha256:[a-f0-9]{64}$'),

    -- trust fields (envelope.trust, envelope.canonical, envelope.interpreted)
    -- canonical/interpreted are pinned to false at the column-default AND
    -- CHECK level, mirroring the envelope's JSON Schema const:false — this
    -- table must never accept canonical=true or interpreted=true rows.
    trust                   session_raw.trust_level NOT NULL,
    canonical                BOOLEAN NOT NULL DEFAULT false
        CHECK (canonical = false),
    interpreted              BOOLEAN NOT NULL DEFAULT false
        CHECK (interpreted = false),

    -- idempotency (envelope.idempotency_key) — UNIQUE constraint is the
    -- dedup mechanism required by the envelope schema and by the
    -- Definition Of Done for this job.
    idempotency_key          TEXT NOT NULL
        CHECK (idempotency_key ~ '^sha256:[a-f0-9]{64}$'),

    -- optional contextual fields (envelope.workstream, envelope.schema_notes)
    workstream               TEXT,
    schema_notes              TEXT,

    -- bookkeeping (NOT part of the envelope; storage-layer metadata)
    stored_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT envelopes_idempotency_key_unique UNIQUE (idempotency_key)
);

COMMENT ON TABLE session_raw.envelopes IS
    'Raw, unmodified storage of SessionIngressEnvelope instances. No semantic '
    'interpretation happens on this table or any trigger attached to it — '
    'see session_jobs.outbox for the projection mechanism.';

-- event_id is NOT globally unique by envelope-schema design (retries may
-- reuse a different event_id for the same logical event), so it is indexed
-- but not constrained UNIQUE.
CREATE INDEX idx_session_raw_envelopes_event_id
    ON session_raw.envelopes (event_id);

CREATE INDEX idx_session_raw_envelopes_provider_session
    ON session_raw.envelopes (provider, provider_session_id);

CREATE INDEX idx_session_raw_envelopes_occurred_at
    ON session_raw.envelopes (occurred_at);

-- GIN index for ad-hoc JSONB payload queries (worker-side projection reads,
-- not part of the hot ingest write path).
CREATE INDEX idx_session_raw_envelopes_payload_gin
    ON session_raw.envelopes USING GIN (payload jsonb_path_ops);


-- ============================================================================
-- SCHEMA: session_core
-- Projected, processed state. NEVER raw. Populated asynchronously by
-- session_jobs workers reading session_raw.envelopes, never by a trigger
-- that calls out to an LLM/HTTP service.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS session_core;

CREATE TABLE session_core.sessions (
    session_id              UUID NOT NULL PRIMARY KEY DEFAULT gen_random_uuid(),
    provider                TEXT NOT NULL,
    provider_session_id     TEXT NOT NULL,
    started_at               TIMESTAMPTZ NOT NULL,
    last_seen_at              TIMESTAMPTZ NOT NULL,
    trust                    session_raw.trust_level NOT NULL,
    -- session_core projections are derived/interpreted state, distinct from
    -- the ingress envelope's pinned interpreted=false; this column lives in
    -- session_core, never mutates the session_raw row.
    status                   TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed', 'archived')),
    metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT sessions_provider_session_unique UNIQUE (provider, provider_session_id)
);

CREATE TABLE session_core.turns (
    turn_id                  BIGSERIAL PRIMARY KEY,
    session_id                UUID NOT NULL REFERENCES session_core.sessions (session_id)
                                   ON DELETE CASCADE,
    source_envelope_id         BIGINT REFERENCES session_raw.envelopes (id),
    occurred_at                TIMESTAMPTZ NOT NULL,
    role                       TEXT NOT NULL,            -- e.g. user/assistant/tool/system
    turn_seq                   INTEGER NOT NULL,         -- monotonic order within session
    content                    JSONB NOT NULL,

    CONSTRAINT turns_session_seq_unique UNIQUE (session_id, turn_seq)
);

CREATE TABLE session_core.chunks (
    chunk_id                  BIGSERIAL PRIMARY KEY,
    turn_id                    BIGINT NOT NULL REFERENCES session_core.turns (turn_id)
                                   ON DELETE CASCADE,
    session_id                 UUID NOT NULL REFERENCES session_core.sessions (session_id)
                                   ON DELETE CASCADE,
    chunk_seq                   INTEGER NOT NULL,
    text                         TEXT NOT NULL,
    token_count                  INTEGER,

    CONSTRAINT chunks_turn_seq_unique UNIQUE (turn_id, chunk_seq)
);

CREATE TABLE session_core.source_refs (
    source_ref_id              BIGSERIAL PRIMARY KEY,
    chunk_id                    BIGINT NOT NULL REFERENCES session_core.chunks (chunk_id)
                                     ON DELETE CASCADE,
    ref_kind                     TEXT NOT NULL,           -- e.g. file, url, tool_call
    ref_value                    TEXT NOT NULL,
    content_hash                  TEXT
);

CREATE TABLE session_core.manifests (
    session_id                  UUID NOT NULL REFERENCES session_core.sessions (session_id)
                                     ON DELETE CASCADE,
    manifest_version              INTEGER NOT NULL,
    generated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary                          JSONB NOT NULL,

    PRIMARY KEY (session_id, manifest_version)
);


-- ============================================================================
-- SCHEMA: session_idx
-- FTS, vector refs (pgvector/HNSW), ranking features. Read/query-optimized
-- side of the system; never written by a trigger that calls external
-- services — embedding vectors arrive via session_jobs workers.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS session_idx;

CREATE TABLE session_idx.chunk_fts (
    chunk_id                    BIGINT PRIMARY KEY REFERENCES session_core.chunks (chunk_id)
                                     ON DELETE CASCADE,
    tsv                            TSVECTOR NOT NULL
);

-- Embedding dimensionality is a placeholder (1536, matching common
-- text-embedding models) — pinning the actual model/dimension is an open
-- decision for the worker job that implements embedding generation
-- (see session-chunk-indexer-001 in execution-phases.md Phase 3).
CREATE TABLE session_idx.chunk_embeddings (
    chunk_id                     BIGINT PRIMARY KEY REFERENCES session_core.chunks (chunk_id)
                                      ON DELETE CASCADE,
    embedding_model                 TEXT NOT NULL,
    embedding                        VECTOR(1536) NOT NULL,
    generated_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE session_idx.ranking_features (
    chunk_id                      BIGINT PRIMARY KEY REFERENCES session_core.chunks (chunk_id)
                                       ON DELETE CASCADE,
    recency_score                    DOUBLE PRECISION,
    importance_score                  DOUBLE PRECISION,
    feature_vector                      JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Index strategy (Definition Of Done: "index-stratégia konkrét CREATE INDEX
-- parancsokkal, session_id, metadata, FTS, vector").

-- session_id lookups (joined through session_core.chunks -> session_core.turns)
CREATE INDEX idx_session_core_turns_session_id
    ON session_core.turns (session_id);

CREATE INDEX idx_session_core_chunks_session_id
    ON session_core.chunks (session_id);

-- metadata lookups
CREATE INDEX idx_session_core_sessions_metadata_gin
    ON session_core.sessions USING GIN (metadata jsonb_path_ops);

-- full-text search
CREATE INDEX idx_session_idx_chunk_fts_tsv
    ON session_idx.chunk_fts USING GIN (tsv);

-- vector similarity search (HNSW, per architecture.md "session_idx: FTS,
-- vector refs (pgvector/HNSW)")
CREATE INDEX idx_session_idx_chunk_embeddings_hnsw
    ON session_idx.chunk_embeddings USING hnsw (embedding vector_cosine_ops);


-- ============================================================================
-- SCHEMA: session_jobs
-- Outbox/projection jobs, retry/dead-letter state. This is the mechanism
-- that moves data from session_raw into session_core/session_idx. Trigger
-- writes ONLY enqueue an outbox row (cheap, synchronous, no external call);
-- the actual projection/embedding work happens in an external worker
-- process that polls/consumes this table.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS session_jobs;

CREATE TYPE session_jobs.job_status AS ENUM (
    'pending', 'in_progress', 'done', 'failed', 'dead_letter'
);

CREATE TABLE session_jobs.outbox (
    job_id                       BIGSERIAL PRIMARY KEY,
    job_type                       TEXT NOT NULL,    -- e.g. 'project_turn', 'embed_chunk'
    source_table                    TEXT NOT NULL,   -- e.g. 'session_raw.envelopes'
    source_id                        BIGINT NOT NULL, -- envelopes.id or chunks.chunk_id etc.
    payload                            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                              session_jobs.job_status NOT NULL DEFAULT 'pending',
    attempts                             INTEGER NOT NULL DEFAULT 0,
    max_attempts                          INTEGER NOT NULL DEFAULT 5,
    last_error                             TEXT,
    created_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                               TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by                                TEXT,     -- worker identity, for crash-safe claiming
    locked_at                                 TIMESTAMPTZ
);

CREATE INDEX idx_session_jobs_outbox_status_created
    ON session_jobs.outbox (status, created_at)
    WHERE status IN ('pending', 'failed');

-- ----------------------------------------------------------------------------
-- Trigger boundary: this trigger ONLY inserts a row into session_jobs.outbox.
-- It performs NO network call, NO LLM call, NO HTTP request — purely a
-- synchronous, in-DB INSERT. This is the explicit trigger/outbox boundary
-- required by the job's forbidden_shortcuts ("trigger calls external
-- LLM/HTTP — TILOS").
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION session_raw.enqueue_projection_job()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO session_jobs.outbox (job_type, source_table, source_id, payload)
    VALUES ('project_envelope', 'session_raw.envelopes', NEW.id,
            jsonb_build_object('event_id', NEW.event_id, 'provider', NEW.provider));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_session_raw_envelopes_enqueue
    AFTER INSERT ON session_raw.envelopes
    FOR EACH ROW
    EXECUTE FUNCTION session_raw.enqueue_projection_job();


-- ============================================================================
-- SCHEMA: session_api
-- Stable SQL functions called by the MCP server. The server never queries
-- session_core/session_idx tables directly (architecture.md: "Az MCP
-- szerver ne tablakat turkaljon. Stabil API fuggvenyeket hivjon").
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS session_api;

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
           ts_rank(f.tsv, plainto_tsquery('english', p_query)) AS rank
    FROM session_core.chunks c
    JOIN session_idx.chunk_fts f ON f.chunk_id = c.chunk_id
    WHERE c.session_id = p_session_id
      AND f.tsv @@ plainto_tsquery('english', p_query)
    ORDER BY rank DESC
    LIMIT p_limit;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION session_api.get_timeline(
    p_session_id UUID,
    p_limit INTEGER DEFAULT 100
)
RETURNS TABLE (
    turn_id BIGINT,
    occurred_at TIMESTAMPTZ,
    role TEXT,
    turn_seq INTEGER
) AS $$
    SELECT t.turn_id, t.occurred_at, t.role, t.turn_seq
    FROM session_core.turns t
    WHERE t.session_id = p_session_id
    ORDER BY t.turn_seq ASC
    LIMIT p_limit;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION session_api.get_context_pack(
    p_session_id UUID,
    p_max_chunks INTEGER DEFAULT 50
)
RETURNS TABLE (
    chunk_id BIGINT,
    turn_seq INTEGER,
    text TEXT
) AS $$
    SELECT c.chunk_id, t.turn_seq, c.text
    FROM session_core.chunks c
    JOIN session_core.turns t ON t.turn_id = c.turn_id
    WHERE c.session_id = p_session_id
    ORDER BY t.turn_seq ASC, c.chunk_seq ASC
    LIMIT p_max_chunks;
$$ LANGUAGE sql STABLE;

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
           (SELECT count(*) FROM session_jobs.outbox o
              WHERE o.status IN ('pending', 'failed')
                AND o.payload->>'event_id' IN (
                    SELECT e.event_id::text FROM session_raw.envelopes e
                    WHERE e.provider_session_id = s.provider_session_id
                )) AS pending_jobs
    FROM session_core.sessions s
    WHERE s.session_id = p_session_id;
$$ LANGUAGE sql STABLE;


-- ============================================================================
-- Rollback / drop note (no migration framework wired in for this DESIGN
-- job — see "Nem cél"). To tear down a manually-applied instance of this
-- draft in a scratch environment:
-- ============================================================================
--
-- DROP SCHEMA session_api CASCADE;
-- DROP SCHEMA session_jobs CASCADE;
-- DROP SCHEMA session_idx CASCADE;
-- DROP SCHEMA session_core CASCADE;
-- DROP SCHEMA session_raw CASCADE;
