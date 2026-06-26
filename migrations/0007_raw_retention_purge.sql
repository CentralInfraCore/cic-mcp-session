-- ============================================================================
-- session-raw-retention-purge-001
-- ADDITIVE migration on top of migrations/0001_postgres_schema.sql.
-- STATUS: experimental -- the purge entry point + this table are applied and
-- verified against a real Postgres instance, see
-- output/session-raw-retention-purge.md for the actual pytest output proving
-- old rows are deleted (by occurred_at), new rows survive, and exactly one
-- session_audit.raw_purges row is written per purge. Installing a scheduler
-- that calls the purge is a separate human/hosting decision (NOT in scope).
--
-- One new table: session_audit.raw_purges -- a write-once audit log for
-- time-based PURGES of session_raw.envelopes, the deletion counterpart of
-- session_audit.raw_reads (data-protection-001). Written by
-- session_store/retention_purge.py:purge_expired_raw_envelopes(), in the SAME
-- transaction as the DELETE it records (so a purge is never silently
-- un-audited if the transaction fails partway).
--
-- NOTE: the session_audit schema is created here with CREATE SCHEMA IF NOT
-- EXISTS so this migration is self-contained and idempotent even though
-- session_audit was first introduced by output/session-data-protection-
-- migration.sql -- that data-protection migration is, as of this job, present
-- only under output/ and is NOT yet wired into the numbered migrations/
-- runner (a pre-existing gap flagged in this job's report; this migration does
-- not depend on session_audit.raw_reads existing).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS session_audit;

CREATE TABLE IF NOT EXISTS session_audit.raw_purges (
    purge_id        BIGSERIAL PRIMARY KEY,
    purger          TEXT NOT NULL,
    retention_days  INTEGER NOT NULL,
    cutoff          TIMESTAMPTZ NOT NULL,
    rows_deleted    INTEGER NOT NULL,
    purged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE session_audit.raw_purges IS
    'One row per REAL (non-dry-run) call to session_store.retention_purge.purge_expired_raw_envelopes() -- records WHO (purger) ran a time-based retention purge of session_raw.envelopes, with WHICH retention window (retention_days), the occurred_at cutoff below which rows were deleted (cutoff), and HOW MANY rows were actually removed (rows_deleted). The deletion counterpart of session_audit.raw_reads (session-data-protection-001). A dry run writes NO row here -- every row represents a real deletion event. This is NOT a general-purpose audit log for every table in this schema.';

COMMENT ON COLUMN session_audit.raw_purges.purger IS
    'Free-text identity of the caller that ran the purge (e.g. an operator username, or "retention_cron") -- NOT a foreign key to any auth table; this layer has no user/auth model of its own (see CLAUDE.md trust model), same convention as session_audit.raw_reads.reader.';

COMMENT ON COLUMN session_audit.raw_purges.retention_days IS
    'The retention window (in days) used for THIS purge, after resolving the per-call argument / SESSION_RAW_RETENTION_DAYS env / default-90 precedence. Recorded so a later audit can see exactly which window produced this deletion, even if the default later changes.';

COMMENT ON COLUMN session_audit.raw_purges.cutoff IS
    'The now() - make_interval(days => retention_days) boundary used by the DELETE: every deleted row had occurred_at < cutoff (occurred_at, the event time -- NEVER ingested_at). Captured in the same CTE as the DELETE, so it provably equals the predicate the DELETE applied.';

COMMENT ON COLUMN session_audit.raw_purges.rows_deleted IS
    'Count of session_raw.envelopes rows actually removed by this purge (DELETE ... RETURNING count, in the same transaction as this audit row) -- not a pre-count estimate.';

CREATE INDEX IF NOT EXISTS idx_session_audit_raw_purges_purged_at
    ON session_audit.raw_purges (purged_at);
