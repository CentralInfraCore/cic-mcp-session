-- ============================================================================
-- session-audit-migration-wiring-001
-- Wires session-data-protection-001's session_audit.raw_reads into the
-- numbered, checksum-enforced migration runner (session_store/migrate.py).
--
-- WHY THIS EXISTS: the DDL below already lived in
-- output/session-data-protection-migration.sql, but was never copied into
-- migrations/ as a numbered file. A database provisioned via the canonical
-- run_migrations() path therefore lacked session_audit.raw_reads, and
-- session_store/raw_read_audit.py:log_and_read_raw_envelopes() would fail with
-- `relation "session_audit.raw_reads" does not exist`. test_data_protection.py
-- masked this by applying the schema out-of-band. This migration closes that
-- gap so the runner-only provisioning path produces a working raw_reads.
--
-- NUMBERING — APPEND-ONLY (0008, NOT inserted before 0007): migrations are
-- immutable once applied and checksum-enforced (migrate.py raises
-- ChecksumMismatchError on a changed already-applied file). 0007
-- (session-raw-retention-purge-001) is already merged + applied, so it must
-- NOT be renumbered. raw_reads and raw_purges are independent (each only needs
-- `CREATE SCHEMA IF NOT EXISTS session_audit`), so the relative order of 0007
-- and 0008 is functionally irrelevant; append-only is the only safe choice.
-- The session_audit schema is (re)created here with IF NOT EXISTS so this
-- migration is self-contained and idempotent whether or not 0007 ran first.
--
-- The output/ original (output/session-data-protection-migration.sql) is kept
-- as the documentation mirror, per the session-schema-migration-tooling-001
-- convention.
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
