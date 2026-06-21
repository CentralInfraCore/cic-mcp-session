"""
End-to-end tests for session_store.turn_projector against a REAL Postgres
instance.

Job: session-turn-projector-001

These tests do NOT mock the database connection, the outbox, or
session_raw.envelopes. They require a live Postgres reachable via the
SESSION_STORE_PG_* env vars (see session_store.envelope_writer.
SessionStoreConfig.from_env), with output/session-postgres-schema.sql
already applied.

The full chain under test, end to end:

    insert_envelope() [session_store.envelope_writer, EXISTING write-path]
        -> trg_session_raw_envelopes_enqueue trigger
        -> session_jobs.outbox row (status='pending', job_type='project_envelope')
        -> run_projection_batch() [session_store.turn_projector, THIS job]
        -> session_core.sessions row (upserted)
        -> session_core.turns row (inserted, role mapped, turn_seq assigned)
        -> session_jobs.outbox row marked 'done'

Reproduction (see also output/session-turn-projector-report.md):

    docker run -d --name session-turn-projector-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55433:5432 pgvector/pgvector:pg16

    # wait for readiness (pg_isready), then:
    docker exec -i session-turn-projector-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-postgres-schema.sql

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55433 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        pytest tests/test_session_store/test_turn_projector.py -v

    docker rm -f session-turn-projector-test
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from session_store.envelope_writer import SessionStoreConfig, insert_envelope
from session_store.turn_projector import map_role, run_projection_batch


def _pg_config() -> SessionStoreConfig:
    import os

    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55433")),
        dbname=os.environ.get("SESSION_STORE_PG_DB", "testdb"),
        user=os.environ.get("SESSION_STORE_PG_USER", "postgres"),
        password=os.environ.get("SESSION_STORE_PG_PASSWORD", "test"),
    )


@pytest.fixture(scope="session")
def pg_config() -> SessionStoreConfig:
    cfg = _pg_config()
    try:
        with psycopg.connect(cfg.conninfo(), connect_timeout=5):
            pass
    except psycopg.OperationalError as exc:
        pytest.fail(
            "Cannot reach a real Postgres instance for turn_projector tests. "
            "Start the test container first (see module docstring for the "
            f"exact docker run command). Original error: {exc}"
        )
    return cfg


@pytest.fixture(autouse=True)
def _clean_tables(pg_config: SessionStoreConfig):
    """Truncate all tables touched by this test module before each test."""
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE TABLE session_jobs.outbox, session_core.turns, "
                "session_core.sessions, session_raw.envelopes CASCADE"
            )
        conn.commit()
    yield


def _valid_envelope(**overrides) -> dict:
    base = {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": "claude-code",
        "provider_session_id": "sess-proj-001",
        "provider_event_name": "Stop",
        "source": {"kind": "hook", "collector": "log-event.py"},
        "occurred_at": datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
        "ingested_at": datetime(2026, 6, 20, 12, 0, 1, tzinfo=timezone.utc),
        "payload": {"raw_text": "hello world"},
        "payload_encoding": "json",
        "raw_payload_hash": "sha256:" + ("a" * 64),
        "trust": "session_local",
        "canonical": False,
        "interpreted": False,
        "idempotency_key": "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32],
        "workstream": None,
        "schema_notes": None,
    }
    base.update(overrides)
    return base


def _outbox_row(pg_config: SessionStoreConfig, job_id: int) -> tuple:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, attempts, max_attempts, last_error "
                "FROM session_jobs.outbox WHERE job_id = %s",
                (job_id,),
            )
            return cur.fetchone()


def _pending_outbox_job_ids(pg_config: SessionStoreConfig) -> list[int]:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM session_jobs.outbox "
                "WHERE job_type = 'project_envelope' ORDER BY job_id ASC"
            )
            return [row[0] for row in cur.fetchall()]


def _sessions_rows(pg_config: SessionStoreConfig) -> list[tuple]:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT session_id, provider, provider_session_id, trust "
                "FROM session_core.sessions"
            )
            return cur.fetchall()


def _turns_rows(pg_config: SessionStoreConfig, session_id) -> list[tuple]:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT turn_seq, role, source_envelope_id FROM session_core.turns "
                "WHERE session_id = %s ORDER BY turn_seq ASC",
                (session_id,),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# 1. Pure unit coverage for the deterministic role mapping (no DB needed,
#    but kept in this module since it documents the contract the e2e tests
#    below rely on).
# ---------------------------------------------------------------------------
def test_map_role_is_deterministic_and_covers_documented_cases():
    assert map_role("PostToolUse", "hook") == "tool"
    assert map_role("PostToolUseFailure", "hook") == "tool"
    assert map_role("Stop", "hook") == "assistant"
    assert map_role(None, "manual") == "manual"
    assert map_role("SomeUnknownEvent", "hook") == "event"
    assert map_role(None, "api") == "event"
    # Same input -> same output, every time (no hidden state, no randomness).
    assert map_role("Stop", "hook") == map_role("Stop", "hook")


# ---------------------------------------------------------------------------
# 2. Full end-to-end chain: insert_envelope -> trigger -> outbox row ->
#    worker run -> session_core rows -> outbox done.
# ---------------------------------------------------------------------------
def test_full_chain_envelope_to_session_core_and_outbox_done(pg_config: SessionStoreConfig):
    envelope = _valid_envelope()

    envelope_id = insert_envelope(envelope, config=pg_config)
    assert envelope_id is not None

    # Trigger must have enqueued exactly one outbox row for this envelope.
    job_ids = _pending_outbox_job_ids(pg_config)
    assert len(job_ids) == 1
    job_id = job_ids[0]
    status_before, attempts_before, _, _ = _outbox_row(pg_config, job_id)
    assert status_before == "pending"
    assert attempts_before == 0

    results = run_projection_batch(config=pg_config)

    assert len(results) == 1
    assert results[0].job_id == job_id
    assert results[0].outcome == "done"
    assert results[0].error is None

    # outbox row closed
    status_after, attempts_after, max_attempts_after, last_error_after = _outbox_row(
        pg_config, job_id
    )
    assert status_after == "done"
    assert last_error_after is None

    # session_core.sessions upserted
    sessions = _sessions_rows(pg_config)
    assert len(sessions) == 1
    session_id, provider, provider_session_id, trust = sessions[0]
    assert provider == "claude-code"
    assert provider_session_id == "sess-proj-001"
    assert trust == "session_local"

    # session_core.turns inserted with deterministic role + turn_seq == 1
    turns = _turns_rows(pg_config, session_id)
    assert len(turns) == 1
    turn_seq, role, source_envelope_id = turns[0]
    assert turn_seq == 1
    assert role == "assistant"  # provider_event_name == "Stop" -> "assistant"
    assert source_envelope_id == envelope_id


# ---------------------------------------------------------------------------
# 3. Error handling: outbox row referencing a non-existent source_id must
#    end up failed/dead_letter, never raise unhandled, never stay pending.
# ---------------------------------------------------------------------------
def test_dangling_source_id_marks_outbox_failed_not_crash_not_stuck_pending(
    pg_config: SessionStoreConfig,
):
    # Manually craft an outbox row pointing at a source_id that never
    # existed in session_raw.envelopes (no insert_envelope call backs it).
    nonexistent_source_id = 999_999_999
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO session_jobs.outbox
                    (job_type, source_table, source_id, payload)
                VALUES ('project_envelope', 'session_raw.envelopes', %s, '{}'::jsonb)
                RETURNING job_id
                """,
                (nonexistent_source_id,),
            )
            job_id = cur.fetchone()[0]
        conn.commit()

    # Must not raise — run_projection_batch absorbs per-row errors.
    results = run_projection_batch(config=pg_config)

    assert len(results) == 1
    assert results[0].job_id == job_id
    assert results[0].outcome in ("failed", "dead_letter")
    assert results[0].error is not None
    assert "999999999" in results[0].error or str(nonexistent_source_id) in results[0].error

    status, attempts, max_attempts, last_error = _outbox_row(pg_config, job_id)
    assert status in ("failed", "dead_letter")
    assert status != "pending"
    assert attempts == 1
    assert last_error is not None

    # No session_core rows were created for the dangling reference.
    assert _sessions_rows(pg_config) == []


def test_dangling_source_id_reaches_dead_letter_after_max_attempts(
    pg_config: SessionStoreConfig,
):
    """Repeated failures against the same dangling row must eventually
    flip from failed -> dead_letter once attempts >= max_attempts, and the
    worker must keep picking up 'failed' rows on subsequent batches (not
    just 'pending') until that happens.
    """
    nonexistent_source_id = 888_888_888
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO session_jobs.outbox
                    (job_type, source_table, source_id, payload, max_attempts)
                VALUES ('project_envelope', 'session_raw.envelopes', %s, '{}'::jsonb, 2)
                RETURNING job_id
                """,
                (nonexistent_source_id,),
            )
            job_id = cur.fetchone()[0]
        conn.commit()

    # First batch: pending -> failed (attempts=1 < max_attempts=2)
    results_1 = run_projection_batch(config=pg_config)
    assert results_1[0].outcome == "failed"
    status_1, attempts_1, _, _ = _outbox_row(pg_config, job_id)
    assert status_1 == "failed"
    assert attempts_1 == 1

    # Second batch: the worker must pick up 'failed' rows too (per
    # input.md "beolvassa ... pending/failed ... sorait"), attempts=2 >=
    # max_attempts=2 -> dead_letter.
    results_2 = run_projection_batch(config=pg_config)
    assert results_2[0].outcome == "dead_letter"
    status_2, attempts_2, _, _ = _outbox_row(pg_config, job_id)
    assert status_2 == "dead_letter"
    assert attempts_2 == 2

    # Third batch: dead_letter rows are NOT re-picked-up (only pending/failed).
    results_3 = run_projection_batch(config=pg_config)
    assert results_3 == []


# ---------------------------------------------------------------------------
# 4. turn_seq increments correctly across 2+ envelopes for the same session.
# ---------------------------------------------------------------------------
def test_turn_seq_increments_across_multiple_envelopes_same_session(
    pg_config: SessionStoreConfig,
):
    envelope_1 = _valid_envelope(
        provider_event_name="UserPromptSubmit",
        occurred_at=datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    envelope_2 = _valid_envelope(
        provider_event_name="Stop",
        occurred_at=datetime(2026, 6, 20, 12, 0, 30, tzinfo=timezone.utc),
        idempotency_key="sha256:" + ("c" * 64),
        event_id=str(uuid.uuid4()),
    )

    envelope_id_1 = insert_envelope(envelope_1, config=pg_config)
    envelope_id_2 = insert_envelope(envelope_2, config=pg_config)
    assert envelope_id_1 is not None
    assert envelope_id_2 is not None

    job_ids = _pending_outbox_job_ids(pg_config)
    assert len(job_ids) == 2

    results = run_projection_batch(config=pg_config)
    assert len(results) == 2
    assert all(r.outcome == "done" for r in results)

    sessions = _sessions_rows(pg_config)
    assert len(sessions) == 1  # same provider+provider_session_id -> one session
    session_id = sessions[0][0]

    turns = _turns_rows(pg_config, session_id)
    assert len(turns) == 2
    assert [t[0] for t in turns] == [1, 2]  # turn_seq strictly 1, then 2
    assert turns[0][1] == "user"  # UserPromptSubmit -> user
    assert turns[1][1] == "assistant"  # Stop -> assistant
    assert turns[0][2] == envelope_id_1
    assert turns[1][2] == envelope_id_2


def test_turn_seq_increments_across_three_envelopes_in_one_batch(
    pg_config: SessionStoreConfig,
):
    """Three envelopes inserted before a single worker batch run — proves
    turn_seq is computed correctly even when multiple outbox rows for the
    same session are pending simultaneously and processed in one call."""
    envelopes = [
        _valid_envelope(
            provider_event_name=name,
            occurred_at=datetime(2026, 6, 20, 12, i, 0, tzinfo=timezone.utc),
            idempotency_key=f"sha256:{i:064d}".replace(" ", "0"),
            event_id=str(uuid.uuid4()),
        )
        for i, name in enumerate(["UserPromptSubmit", "PostToolUse", "Stop"], start=1)
    ]
    for env in envelopes:
        assert insert_envelope(env, config=pg_config) is not None

    results = run_projection_batch(config=pg_config)
    assert len(results) == 3
    assert all(r.outcome == "done" for r in results)

    sessions = _sessions_rows(pg_config)
    assert len(sessions) == 1
    turns = _turns_rows(pg_config, sessions[0][0])
    assert [t[0] for t in turns] == [1, 2, 3]
    assert [t[1] for t in turns] == ["user", "tool", "assistant"]
