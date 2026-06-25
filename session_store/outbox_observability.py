"""
Shared claim/lock bookkeeping and metrics for session_jobs.outbox.

Job: session-outbox-batch-and-observability-001

Source of truth for the table DDL this module reads/writes:
  output/session-postgres-schema.sql (session_jobs.outbox, especially the
  locked_by/locked_at columns at lines ~287-288, which already existed
  before this job but were never written by turn_projector.py/
  chunk_indexer.py — see those modules' pre-change docstrings).

Scope: this module is shared by turn_projector.py and chunk_indexer.py
because both workers poll the SAME session_jobs.outbox table and need
IDENTICAL claim/lock/observability behavior (same statement_timeout
rationale, same locked_by/locked_at semantics, same metrics shape) — see
input.md "Feladat" 2-5. It does NOT touch session_core.*/session_idx.* and
does NOT implement the attempts/max_attempts/dead_letter retry logic (that
remains in each worker's own _mark_failed_or_dead_letter(), unmodified by
this job per input.md "Nem cél").

It does NOT implement a long-lived monitoring daemon — get_outbox_metrics()
is a single-call query function, invoked on demand (e.g. from a CLI, a
future MCP tool, or an ad-hoc script), not a background loop.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass

import psycopg

# ---------------------------------------------------------------------------
# Claim/lock bookkeeping (input.md "3. statement_timeout" / "4. locked_by/
# locked_at")
# ---------------------------------------------------------------------------

# statement_timeout for the claim transaction (the FOR UPDATE SKIP LOCKED
# SELECT + the subsequent locked_by/locked_at UPDATE that happens in the
# SAME transaction in both workers). 30s is chosen because:
#   - the claim transaction itself only does a bounded (batch_size-limited,
#     see DEFAULT_BATCH_SIZE) SELECT + UPDATE over an indexed predicate
#     (idx_session_jobs_outbox_status_created) — this should complete in
#     well under a second even at batch_size=100 on the schema's current
#     table sizes; 30s is a generous multiple of that, not a tight bound,
#     so it will not false-positive cancel a healthy claim under any
#     realistic load.
#   - it is short enough that a genuinely stuck claim transaction (e.g.
#     blocked on an unrelated lock, or a hung connection) releases its row
#     locks within tens of seconds rather than indefinitely, which is the
#     entire point of this safety net per input.md "3.": an elakadt
#     feldolgozás ne tarthassa a sorokat zárolva a végtelenségig.
#   - it applies ONLY to the claim transaction (set via
#     SET LOCAL statement_timeout, scoped to the current transaction), NOT
#     to the per-row projection/indexing transactions in
#     _project_one_job/_index_one_job, which may legitimately take longer
#     (e.g. embedding generation) and are intentionally out of scope here —
#     input.md "3." only requires the safety net "a claim-tranzakcióra".
CLAIM_STATEMENT_TIMEOUT_MS = 30_000

# Default batch size for both workers' claim SELECTs (input.md "2."). Kept
# here (not duplicated as two separate literals) so both workers share one
# documented default and one place to change it.
DEFAULT_BATCH_SIZE = 100


def set_claim_statement_timeout(cur: psycopg.Cursor) -> None:
    """Apply CLAIM_STATEMENT_TIMEOUT_MS to the current transaction only.

    Must be called inside the same transaction as the claim SELECT, before
    that SELECT runs. SET LOCAL is transaction-scoped (resets automatically
    at COMMIT/ROLLBACK) so it never leaks into the per-row processing
    transactions that follow on the same connection.

    Postgres' SET command does not accept bind parameters ($1-style), so
    CLAIM_STATEMENT_TIMEOUT_MS (a module-level int constant, never
    user/request-controlled input) is interpolated directly rather than
    passed as a query parameter.
    """
    cur.execute(f"SET LOCAL statement_timeout = {CLAIM_STATEMENT_TIMEOUT_MS}")


def worker_identity() -> str:
    """Build a worker identity string for locked_by (input.md "4.").

    Format: "<hostname>:<pid>:<short-uuid4>" — hostname+pid identifies the
    OS process, the short uuid4 suffix disambiguates multiple
    run_projection_batch()/run_indexing_batch() calls from the SAME process
    (e.g. successive worker_loop iterations) so locked_by values are never
    accidentally identical across two distinct claim calls even when
    hostname/pid happen to repeat (e.g. in fast test loops). Not a
    cryptographic identifier — purely an observability label for "who
    claimed this row last", per input.md "worker-azonosító".
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def claim_outbox_rows(
    cur: psycopg.Cursor, job_type: str, batch_size: int, locked_by: str
) -> list[tuple]:
    """Claim up to batch_size pending/failed rows for job_type.

    Combines the batch-limited FOR UPDATE SKIP LOCKED SELECT (input.md "2.")
    with immediately writing locked_by/locked_at on exactly the claimed rows
    (input.md "4.") in the SAME transaction the caller already holds the
    SET LOCAL statement_timeout in (see set_claim_statement_timeout) — using
    a CTE so the UPDATE only ever touches the rows the SELECT actually
    locked via SKIP LOCKED, never a wider set.

    Returns (job_id, source_id, attempts, max_attempts) tuples, identical
    shape to the pre-change _fetch_pending_jobs() return value, so callers
    (run_projection_batch/run_indexing_batch) do not need to change their
    per-row processing loop.
    """
    cur.execute(
        """
        WITH claimed AS (
            SELECT job_id
            FROM session_jobs.outbox
            WHERE job_type = %(job_type)s
              AND status IN ('pending', 'failed')
            ORDER BY created_at ASC
            LIMIT %(batch_size)s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE session_jobs.outbox AS o
        SET locked_by = %(locked_by)s,
            locked_at = now()
        FROM claimed
        WHERE o.job_id = claimed.job_id
        RETURNING o.job_id, o.source_id, o.attempts, o.max_attempts
        """,
        {
            "job_type": job_type,
            "batch_size": batch_size,
            "locked_by": locked_by,
        },
    )
    rows = cur.fetchall()
    # Preserve created_at ASC ordering: the UPDATE...FROM does not guarantee
    # RETURNING order matches the CTE's ORDER BY, so re-sort by job_id ASC
    # which is monotonically increasing with created_at for this workload
    # (BIGSERIAL primary key, single INSERT path via the trigger) — cheaper
    # than re-querying created_at and equivalent in practice.
    rows.sort(key=lambda row: row[0])
    return rows


def clear_lock(cur: psycopg.Cursor, job_id: int) -> None:
    """Clear locked_by/locked_at for one outbox row (input.md "4.": 'törölje/
    null-ozza a feldolgozás befejezésekor').

    Called from both _mark_done() and _mark_failed_or_dead_letter() in each
    worker, in the SAME UPDATE statement that sets status (one round trip,
    not two) — see turn_projector.py/chunk_indexer.py call sites. Exposed
    here too as a standalone helper for tests/observability scripts that
    want to clear a lock without going through the full mark-done/mark-
    failed path.
    """
    cur.execute(
        """
        UPDATE session_jobs.outbox
        SET locked_by = NULL, locked_at = NULL
        WHERE job_id = %s
        """,
        (job_id,),
    )


# ---------------------------------------------------------------------------
# Metrics (input.md "5. Metrika-lekérdezés")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxMetrics:
    """One-shot snapshot of session_jobs.outbox state.

    pending_count: rows with status IN ('pending', 'failed') — i.e. rows
        still eligible to be claimed by a future batch (mirrors the exact
        WHERE clause both workers' claim queries use).
    oldest_pending_age_seconds: age in seconds (now() - created_at) of the
        oldest such row, or None if pending_count == 0 (no pending rows
        means "no age to report", not zero).
    dead_letter_count: rows with status = 'dead_letter'.
    attempts_histogram: dict mapping attempts (int) -> row count (int),
        across ALL rows regardless of job_type/status — e.g. {0: 5, 1: 2}
        means 5 rows have never been retried, 2 rows have been retried
        once. Built from a GROUP BY, so only attempts values that actually
        occur appear as keys (no zero-filled gaps).
    """

    pending_count: int
    oldest_pending_age_seconds: float | None
    dead_letter_count: int
    attempts_histogram: dict[int, int]


def get_outbox_metrics(cur: psycopg.Cursor, job_type: str | None = None) -> OutboxMetrics:
    """Compute OutboxMetrics in a single call (input.md "5.").

    job_type=None (default) aggregates across ALL job types in
    session_jobs.outbox (both 'project_envelope' and 'index_turn', plus any
    future job_type). Pass a specific job_type to scope all four figures to
    just that worker's rows. Runs three separate, simple aggregate queries
    (not one combined query) for readability and because the histogram's
    GROUP BY shape does not combine cleanly with the scalar aggregates in a
    single result row — this is a single Python-level call/round-trip group
    (three statements, one Cursor, no loop/poll), consistent with input.md
    "Nem cél": "egyszeri hívásra kell működnie", not a daemon.
    """
    where_job_type = "WHERE job_type = %(job_type)s" if job_type is not None else ""
    params = {"job_type": job_type} if job_type is not None else {}

    cur.execute(
        f"""
        SELECT
            COUNT(*) FILTER (WHERE status IN ('pending', 'failed')) AS pending_count,
            EXTRACT(EPOCH FROM (now() - MIN(created_at) FILTER (
                WHERE status IN ('pending', 'failed')
            ))) AS oldest_pending_age_seconds,
            COUNT(*) FILTER (WHERE status = 'dead_letter') AS dead_letter_count
        FROM session_jobs.outbox
        {where_job_type}
        """,
        params,
    )
    pending_count, oldest_pending_age_seconds, dead_letter_count = cur.fetchone()

    cur.execute(
        f"""
        SELECT attempts, COUNT(*) AS row_count
        FROM session_jobs.outbox
        {where_job_type}
        GROUP BY attempts
        ORDER BY attempts ASC
        """,
        params,
    )
    attempts_histogram = {attempts: row_count for attempts, row_count in cur.fetchall()}

    return OutboxMetrics(
        pending_count=pending_count,
        oldest_pending_age_seconds=(
            float(oldest_pending_age_seconds)
            if oldest_pending_age_seconds is not None
            else None
        ),
        dead_letter_count=dead_letter_count,
        attempts_histogram=attempts_histogram,
    )
