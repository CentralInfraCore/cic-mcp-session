"""
Outbox-worker for session_jobs.outbox (job_type='project_envelope') ->
session_core.sessions / session_core.turns projection.

Job: session-turn-projector-001 (batch-limit/statement_timeout/locked_by/
locked_at/metrics added by session-outbox-batch-and-observability-001 — see
session_store/outbox_observability.py for the shared implementation).

Source of truth for the table DDL:
  output/session-postgres-schema.sql (session_jobs.outbox, session_core.sessions,
  session_core.turns, session_raw.envelopes)
Source of truth for the write-path this worker consumes (read-only here):
  session_store/envelope_writer.py (insert_envelope() -> session_raw.envelopes,
  whose AFTER INSERT trigger trg_session_raw_envelopes_enqueue enqueues the
  session_jobs.outbox row this worker reads)

Scope: this module reads pending/failed session_jobs.outbox rows with
job_type='project_envelope' (now LIMIT-ed to a configurable batch_size, see
run_projection_batch), projects the referenced session_raw.envelopes
row into session_core.sessions/session_core.turns, and closes the outbox
row (done/failed/dead_letter). It does NOT touch session_core.chunks,
source_refs, manifests, or session_idx.* (embedding generation) — see
input.md "Nem cél" / CLAUDE.md "Fő határok". It does NOT implement
multi-worker CONCURRENCY/coordination beyond what is needed for a SINGLE
worker instance (the locked_by/locked_at columns are now written for
observability/crash-forensics, NOT as a coordination primitive a second
worker instance would need to interpret) — see "Decisions Proposed" /
"Risks" in output/session-turn-projector-report.md and
output/session-outbox-batch-and-observability.md for the explicit
limitation.

This module has NO production caller in this job (no cron/supervisor/systemd
timer is wired in — see input.md "Nem cél"). Only this job's own pytest
suite (tests/test_session_store/test_turn_projector.py) and the CLI entry
point below (`python -m session_store.turn_projector`) invoke
run_projection_batch().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from session_store.envelope_writer import SessionStoreConfig
from session_store.outbox_observability import (
    DEFAULT_BATCH_SIZE,
    claim_outbox_rows,
    set_claim_statement_timeout,
    worker_identity,
)

logger = logging.getLogger(__name__)

OUTBOX_JOB_TYPE = "project_envelope"

# ---------------------------------------------------------------------------
# Role mapping (input.md "2. role-leképezés")
#
# Deterministic, code-fixed lookup from provider_event_name / source.kind to
# session_core.turns.role. This is NOT an LLM/AI call and NOT semantic
# interpretation in the "decision/claim extraction" sense the envelope
# schema's interpreted:false forbids at ingress — it is a fixed
# categorization applied at the session_core projection layer, which is
# explicitly documented (session-postgres-storage-design-001 report) as
# "derived/interpreted state, distinct from the ingress envelope's pinned
# interpreted=false". See report "Decisions Proposed" for the full
# rationale and the rejected alternatives.
#
# Lookup precedence:
#   1. provider_event_name exact match (PROVIDER_EVENT_NAME_TO_ROLE)
#   2. source_kind == 'manual' -> 'manual' (explicit human-authored entry,
#      independent of provider_event_name)
#   3. fallback -> 'event' (anything not explicitly mapped; still a valid,
#      non-empty role TEXT NOT NULL value, never raises)
# ---------------------------------------------------------------------------
PROVIDER_EVENT_NAME_TO_ROLE: dict[str, str] = {
    "PostToolUse": "tool",
    "PreToolUse": "tool",
    "PostToolUseFailure": "tool",
    "PreToolUseFailure": "tool",
    "Stop": "assistant",
    "SubagentStop": "assistant",
    "UserPromptSubmit": "user",
    "Notification": "system",
    "SessionStart": "system",
    "SessionEnd": "system",
}

FALLBACK_ROLE = "event"
MANUAL_SOURCE_ROLE = "manual"


def map_role(provider_event_name: str | None, source_kind: str) -> str:
    """Deterministically map (provider_event_name, source_kind) -> role.

    Pure function, no I/O, no external calls — see module docstring "Role
    mapping" for the full precedence rule and rationale. Never raises;
    always returns a non-empty string suitable for session_core.turns.role
    (TEXT NOT NULL).
    """
    if provider_event_name and provider_event_name in PROVIDER_EVENT_NAME_TO_ROLE:
        return PROVIDER_EVENT_NAME_TO_ROLE[provider_event_name]
    if source_kind == "manual":
        return MANUAL_SOURCE_ROLE
    return FALLBACK_ROLE


@dataclass(frozen=True)
class ProjectionResult:
    """Outcome of a single outbox row projection attempt."""

    job_id: int
    outcome: str  # 'done' | 'failed' | 'dead_letter'
    error: str | None = None


def _fetch_pending_jobs(
    cur: psycopg.Cursor, batch_size: int, locked_by: str
) -> list[tuple]:
    """Claim up to batch_size pending/failed project_envelope outbox rows.

    Job: session-outbox-batch-and-observability-001 (extends the original
    session-turn-projector-001 implementation).

    FOR UPDATE SKIP LOCKED is used even under the single-worker-instance
    assumption documented in input.md/report "Risks" — it costs nothing
    with one worker and avoids a foot-gun if a second instance is ever
    started by mistake. As of this job, the SELECT is now LIMIT-ed to
    batch_size (input.md "2.") and the claimed rows' locked_by/locked_at
    columns are written in the SAME transaction (input.md "4.") via
    session_store.outbox_observability.claim_outbox_rows() — see that
    module for the SQL and the statement_timeout safety net
    (set_claim_statement_timeout(), applied by the caller before this runs).
    """
    return claim_outbox_rows(cur, OUTBOX_JOB_TYPE, batch_size, locked_by)


def _fetch_envelope(cur: psycopg.Cursor, source_id: int) -> tuple | None:
    cur.execute(
        """
        SELECT id, provider, provider_session_id, provider_event_name,
               source_kind, occurred_at, trust, payload
        FROM session_raw.envelopes
        WHERE id = %s
        """,
        (source_id,),
    )
    return cur.fetchone()


def _upsert_session(
    cur: psycopg.Cursor,
    provider: str,
    provider_session_id: str,
    occurred_at,
    trust: str,
) -> str:
    """Upsert session_core.sessions keyed by (provider, provider_session_id).

    started_at is only set on first insert (EXCLUDED is not used for it);
    last_seen_at is advanced on every projected envelope, per input.md
    "upsert-eli a session_core.sessions sort ... ON CONFLICT ... DO UPDATE
    SET last_seen_at = ...".
    """
    cur.execute(
        """
        INSERT INTO session_core.sessions
            (provider, provider_session_id, started_at, last_seen_at, trust)
        VALUES (%(provider)s, %(provider_session_id)s, %(occurred_at)s, %(occurred_at)s, %(trust)s)
        ON CONFLICT (provider, provider_session_id)
        DO UPDATE SET last_seen_at = GREATEST(
            session_core.sessions.last_seen_at, EXCLUDED.last_seen_at
        )
        RETURNING session_id
        """,
        {
            "provider": provider,
            "provider_session_id": provider_session_id,
            "occurred_at": occurred_at,
            "trust": trust,
        },
    )
    return cur.fetchone()[0]


def _next_turn_seq(cur: psycopg.Cursor, session_id) -> int:
    """Compute the next turn_seq for a session, within the caller's transaction.

    SELECT ... FOR UPDATE on session_core.sessions row is taken by the
    caller's row lock semantics via the outbox SKIP LOCKED pass combined
    with this SELECT running inside the same transaction as the eventual
    INSERT — under the single-worker-instance assumption documented in
    input.md ("egyetlen worker-instance feltételezéssel ez elég,
    dokumentáld a limitációt") this is race-free because there is only one
    process advancing turn_seq for a given session at a time. A genuinely
    concurrent multi-worker deployment would need an explicit
    SELECT ... FOR UPDATE on session_core.sessions or an advisory lock —
    out of scope here, see report "Risks".
    """
    cur.execute(
        """
        SELECT COALESCE(MAX(turn_seq), 0) + 1
        FROM session_core.turns
        WHERE session_id = %s
        """,
        (session_id,),
    )
    return cur.fetchone()[0]


def _insert_turn(
    cur: psycopg.Cursor,
    session_id,
    source_envelope_id: int,
    occurred_at,
    role: str,
    turn_seq: int,
    content,
) -> int:
    cur.execute(
        """
        INSERT INTO session_core.turns
            (session_id, source_envelope_id, occurred_at, role, turn_seq, content)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING turn_id
        """,
        (session_id, source_envelope_id, occurred_at, role, turn_seq, psycopg.types.json.Json(content)),
    )
    return cur.fetchone()[0]


def _mark_done(cur: psycopg.Cursor, job_id: int) -> None:
    """Mark the row done AND clear locked_by/locked_at (input.md "4.":
    'törölje/null-ozza a feldolgozás befejezésekor').

    Same UPDATE statement, not a separate clear_lock() round trip — adding
    two SET clauses to the pre-existing statement, not changing what the
    statement decides.
    """
    cur.execute(
        """
        UPDATE session_jobs.outbox
        SET status = 'done', updated_at = now(),
            locked_by = NULL, locked_at = NULL
        WHERE job_id = %s
        """,
        (job_id,),
    )


def _mark_failed_or_dead_letter(
    cur: psycopg.Cursor, job_id: int, attempts: int, max_attempts: int, error: str
) -> str:
    """Unmodified retry/dead-letter DECISION logic (input.md "Nem cél" —
    attempts/max_attempts/dead_letter is NOT touched by this job). The only
    addition is clearing locked_by/locked_at in the SAME UPDATE (input.md
    "4."), which does not change new_attempts/new_status computation below.
    """
    new_attempts = attempts + 1
    new_status = "dead_letter" if new_attempts >= max_attempts else "failed"
    cur.execute(
        """
        UPDATE session_jobs.outbox
        SET status = %s, attempts = %s, last_error = %s, updated_at = now(),
            locked_by = NULL, locked_at = NULL
        WHERE job_id = %s
        """,
        (new_status, new_attempts, error, job_id),
    )
    return new_status


def _project_one_job(
    conn: psycopg.Connection, job_id: int, source_id: int, attempts: int, max_attempts: int
) -> ProjectionResult:
    """Project a single outbox row in its own transaction.

    Each job gets its own transaction so that one bad row (e.g. dangling
    source_id) cannot poison the batch or roll back already-completed
    projections of other rows.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                envelope = _fetch_envelope(cur, source_id)
                if envelope is None:
                    raise LookupError(
                        f"session_raw.envelopes row not found for source_id={source_id}"
                    )
                (
                    envelope_id,
                    provider,
                    provider_session_id,
                    provider_event_name,
                    source_kind,
                    occurred_at,
                    trust,
                    payload,
                ) = envelope

                session_id = _upsert_session(
                    cur, provider, provider_session_id, occurred_at, trust
                )
                role = map_role(provider_event_name, source_kind)
                turn_seq = _next_turn_seq(cur, session_id)
                _insert_turn(
                    cur, session_id, envelope_id, occurred_at, role, turn_seq, payload
                )
                _mark_done(cur, job_id)
        return ProjectionResult(job_id=job_id, outcome="done")
    except Exception as exc:  # noqa: BLE001 - deliberate: never let one bad
        # row raise out of the batch; always resolve the outbox row instead.
        logger.warning("projection failed for outbox job_id=%s: %s", job_id, exc)
        with conn.transaction():
            with conn.cursor() as cur:
                outcome = _mark_failed_or_dead_letter(
                    cur, job_id, attempts, max_attempts, str(exc)
                )
        return ProjectionResult(job_id=job_id, outcome=outcome, error=str(exc))


def run_projection_batch(
    config: SessionStoreConfig | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[ProjectionResult]:
    """Run one batch of outbox->session_core projection.

    Claims up to batch_size pending/failed project_envelope outbox rows
    (input.md "2." — previously claimed ALL such rows in one transaction,
    unbounded; see turn_projector.py pre-change docstring/grep evidence in
    the job report), projects each into session_core.sessions/
    session_core.turns, and resolves each outbox row to done/failed/
    dead_letter. Returns the list of per-row results. Never raises on a
    per-row projection failure — only a connection-level failure (e.g.
    Postgres unreachable) propagates, since there is nothing meaningful to
    do per-row in that case.

    This function calls NO external LLM/HTTP service — role mapping
    (map_role) and turn_seq computation are pure, deterministic, in-process
    logic; see module docstring "Role mapping".
    """
    cfg = config or SessionStoreConfig.from_env()
    results: list[ProjectionResult] = []
    locked_by = worker_identity()

    with psycopg.connect(cfg.conninfo()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                set_claim_statement_timeout(cur)
                jobs = _fetch_pending_jobs(cur, batch_size, locked_by)
        # jobs is materialized above; rows were SKIP LOCKED-selected (LIMIT
        # batch_size) and the transaction that held that lock has already
        # committed/closed, so each job is now processed in its own short
        # transaction via _project_one_job. This intentionally trades the
        # row-lock window for retry-safety: see "Risks" in the report for
        # the single-worker assumption this relies on.
        for job_id, source_id, attempts, max_attempts in jobs:
            results.append(_project_one_job(conn, job_id, source_id, attempts, max_attempts))

    return results


def _main() -> int:
    """CLI entry point: `python -m session_store.turn_projector`.

    Runs exactly one projection batch against the Postgres instance
    configured via SESSION_STORE_PG_*/PG* env vars (see
    SessionStoreConfig.from_env) and prints a one-line summary per
    processed outbox row. Exit code is always 0 — per-row failures are
    expected, recoverable outcomes (failed/dead_letter), not CLI errors.
    This is the documented, runnable CLI entry point referenced in the
    report's reachability section; it does NOT by itself prove anything
    about whether something invokes it on a recurring schedule in
    production (see report "Findings"/"Reachability").
    """
    logging.basicConfig(level=logging.INFO)
    results = run_projection_batch()
    if not results:
        print("no pending/failed project_envelope outbox jobs found")
        return 0
    for r in results:
        if r.error:
            print(f"job_id={r.job_id} outcome={r.outcome} error={r.error!r}")
        else:
            print(f"job_id={r.job_id} outcome={r.outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
