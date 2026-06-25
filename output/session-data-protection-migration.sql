-- ============================================================================
-- session-data-protection-001
-- ADDITIVE migration on top of output/session-postgres-schema.sql.
-- STATUS: experimental -- applied and verified against a real Postgres
-- instance, see output/session-data-protection.md for the actual
-- psql/pytest output proving an audit row appears after a real read.
--
-- One new schema, one new table: session_audit.raw_reads -- a write-once
-- audit log for READS of session_raw.envelopes (NOT a general audit log
-- for every table, see input.md "Feladat" 5: "audit-log a raw envelope
-- OLVASÁSOKHOZ" -- raw envelope reads specifically). Written by
-- session_store/raw_read_audit.py:log_and_read_raw_envelopes(), in the
-- SAME transaction as the SELECT it is logging (so a read is never
-- silently un-audited if the transaction itself fails partway).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS session_audit;

CREATE TABLE IF NOT EXISTS session_audit.raw_reads (
    read_id         BIGSERIAL PRIMARY KEY,
    reader          TEXT NOT NULL,
    read_kind       TEXT NOT NULL,
    provider        TEXT,
    provider_session_id TEXT,
    rows_returned   INTEGER NOT NULL,
    read_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE session_audit.raw_reads IS
    'One row per call to session_store.raw_read_audit.log_and_read_raw_envelopes() -- records WHO (reader) read session_raw.envelopes rows, for WHAT stated purpose (read_kind, e.g. "admin_query"/"historical_import"), scoped to which (provider, provider_session_id) if the read was scoped to one conversation, and HOW MANY rows were returned. This table is itself part of session_raw-adjacent provenance, not a general-purpose audit log for every table in this schema -- see session-data-protection-001 input.md "Feladat" 5.';

COMMENT ON COLUMN session_audit.raw_reads.reader IS
    'Free-text identity of the caller (e.g. an admin username, or "historical_import_runner") -- NOT a foreign key to any auth table, since this layer has no user/auth model of its own (see CLAUDE.md trust model).';

COMMENT ON COLUMN session_audit.raw_reads.read_kind IS
    'Free-text category of the read (e.g. "admin_query", "historical_import") -- not an enum, kept open per input.md "Feladat" 5 examples ("pl. egy admin lekérdezés vagy a historical importer").';

CREATE INDEX IF NOT EXISTS idx_session_audit_raw_reads_read_at
    ON session_audit.raw_reads (read_at);
