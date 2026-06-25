"""
End-to-end tests for session_store.outbox_observability and the
batch_size/locked_by/locked_at/statement_timeout behavior it adds to
turn_projector.run_projection_batch / chunk_indexer.run_indexing_batch,
against a REAL Postgres instance.

Job: session-outbox-batch-and-observability-001

These tests do NOT mock the database connection or session_jobs.outbox.
They require a live Postgres reachable via the SESSION_STORE_PG_* env
vars (see session_store.envelope_writer.SessionStoreConfig.from_env),
with output/session-postgres-schema.sql already applied.

Reproduction:

    docker run -d --name session-outbox-batch-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55450:5432 pgvector/pgvector:pg16

    docker exec -i session-outbox-batch-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-postgres-schema.sql

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55450 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        pytest tests/test_session_store/test_outbox_observability.py -v

    docker rm -f session-outbox-batch-test
"""

from __future__ import annotations

import os

import psycopg
import pytest

from session_store.envelope_writer import SessionStoreConfig
from session_store.outbox_observability import (
    CLAIM_STATEMENT_TIMEOUT_MS,
    DEFAULT_BATCH_SIZE,
    claim_outbox_rows,
    clear_lock,
    get_outbox_metrics,
    set_claim_statement_timeout,
    worker_identity,
)
from session_store.turn_projector import run_projection_batch


def _pg_config() -> SessionStoreConfig:
    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55450")),
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
            "Cannot reach a real Postgres instance for outbox_observability "
            f"tests. Start the test container first. Original error: {exc}"
        )
    return cfg


@pytest.fixture(autouse=True)
def _clean_outbox(pg_config: SessionStoreConfig):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE session_jobs.outbox CASCADE")
        conn.commit()
    yield


def _insert_outbox_rows(
    pg_config: SessionStoreConfig,
    count: int,
    job_type: str = "project_envelope",
    status: str = "pending",
    attempts: int = 0,
) -> list[int]:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            job_ids = []
            for _ in range(count):
                cur.execute(
                    """
                    INSERT INTO session_jobs.outbox
                        (job_type, source_table, source_id, payload, status, attempts)
                    VALUES (%s, 'session_raw.envelopes', 1, '{}'::jsonb, %s, %s)
                    RETURNING job_id
                    """,
                    (job_type, status, attempts),
                )
                job_ids.append(cur.fetchone()[0])
        conn.commit()
    return job_ids


def _row_state(pg_config: SessionStoreConfig, job_id: int) -> tuple:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, locked_by, locked_at FROM session_jobs.outbox "
                "WHERE job_id = %s",
                (job_id,),
            )
            return cur.fetchone()


# ---------------------------------------------------------------------------
# 1. Batch LIMIT (input.md "2.")
# ---------------------------------------------------------------------------
def test_claim_with_250_row_backlog_only_claims_batch_size(pg_config: SessionStoreConfig):
    job_ids = _insert_outbox_rows(pg_config, 250)
    assert len(job_ids) == 250

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                set_claim_statement_timeout(cur)
                claimed = claim_outbox_rows(cur, "project_envelope", 100, "test-worker-1")

    assert len(claimed) == 100

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM session_jobs.outbox WHERE locked_by IS NOT NULL"
            )
            locked_count = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM session_jobs.outbox WHERE locked_by IS NULL"
            )
            unlocked_count = cur.fetchone()[0]

    assert locked_count == 100
    assert unlocked_count == 150


def test_run_projection_batch_with_default_batch_size_constant():
    assert DEFAULT_BATCH_SIZE == 100


# ---------------------------------------------------------------------------
# 2. statement_timeout safety net (input.md "3.")
# ---------------------------------------------------------------------------
def test_statement_timeout_is_set_local_scoped_to_transaction(pg_config: SessionStoreConfig):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                set_claim_statement_timeout(cur)
                cur.execute("SELECT current_setting('statement_timeout')")
                (value_inside,) = cur.fetchone()
                # Postgres normalizes 30000ms to "30s" in current_setting/SHOW
                # output, so compare via SET's own round-trip representation
                # rather than the literal ms string.
                assert value_inside in (f"{CLAIM_STATEMENT_TIMEOUT_MS}ms", "30s")

        # SET LOCAL resets at COMMIT — outside the transaction it must not
        # have leaked into the connection's session-level default.
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('statement_timeout')")
            (value_outside,) = cur.fetchone()
            assert value_outside not in (f"{CLAIM_STATEMENT_TIMEOUT_MS}ms", "30s")


# ---------------------------------------------------------------------------
# 3. locked_by/locked_at claim + clear (input.md "4.")
# ---------------------------------------------------------------------------
def test_claim_writes_locked_by_and_locked_at(pg_config: SessionStoreConfig):
    (job_id,) = _insert_outbox_rows(pg_config, 1)

    status_before, locked_by_before, locked_at_before = _row_state(pg_config, job_id)
    assert status_before == "pending"
    assert locked_by_before is None
    assert locked_at_before is None

    identity = worker_identity()
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                claimed = claim_outbox_rows(cur, "project_envelope", 10, identity)
    assert len(claimed) == 1
    assert claimed[0][0] == job_id

    _, locked_by_after, locked_at_after = _row_state(pg_config, job_id)
    assert locked_by_after == identity
    assert locked_at_after is not None


def test_clear_lock_nulls_locked_by_and_locked_at(pg_config: SessionStoreConfig):
    (job_id,) = _insert_outbox_rows(pg_config, 1)
    identity = worker_identity()
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                claim_outbox_rows(cur, "project_envelope", 10, identity)

    _, locked_by_mid, locked_at_mid = _row_state(pg_config, job_id)
    assert locked_by_mid == identity
    assert locked_at_mid is not None

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            clear_lock(cur, job_id)
        conn.commit()

    _, locked_by_after, locked_at_after = _row_state(pg_config, job_id)
    assert locked_by_after is None
    assert locked_at_after is None


def test_run_projection_batch_clears_lock_on_done(pg_config: SessionStoreConfig):
    """End-to-end through the real worker entry point, not just the
    claim_outbox_rows/clear_lock helpers in isolation: a dangling-source-id
    row goes pending -> failed via run_projection_batch, and locked_by/
    locked_at must be NULL again afterward (cleared by _mark_failed_or_dead_letter,
    not left set after the row is no longer "in flight")."""
    (job_id,) = _insert_outbox_rows(pg_config, 1, status="pending")
    # point at a source_id that does not exist so the projection fails fast,
    # without needing a real envelope row
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE session_jobs.outbox SET source_id = 999999999 WHERE job_id = %s",
                (job_id,),
            )
        conn.commit()

    results = run_projection_batch(config=pg_config)
    assert len(results) == 1
    assert results[0].outcome in ("failed", "dead_letter")

    status_after, locked_by_after, locked_at_after = _row_state(pg_config, job_id)
    assert status_after in ("failed", "dead_letter")
    assert locked_by_after is None
    assert locked_at_after is None


# ---------------------------------------------------------------------------
# 4. Metrics (input.md "5.")
# ---------------------------------------------------------------------------
def test_get_outbox_metrics_on_known_fixture(pg_config: SessionStoreConfig):
    # 3 pending, 2 failed (both eligible -> pending_count == 5), 2 dead_letter,
    # attempts: pending rows attempts=0 (x3), failed rows attempts=1 (x2),
    # dead_letter rows attempts=5 (x2).
    _insert_outbox_rows(pg_config, 3, status="pending", attempts=0)
    _insert_outbox_rows(pg_config, 2, status="failed", attempts=1)
    _insert_outbox_rows(pg_config, 2, status="dead_letter", attempts=5)

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            metrics = get_outbox_metrics(cur, job_type="project_envelope")

    assert metrics.pending_count == 5
    assert metrics.dead_letter_count == 2
    assert metrics.oldest_pending_age_seconds is not None
    assert metrics.oldest_pending_age_seconds >= 0
    assert metrics.attempts_histogram == {0: 3, 1: 2, 5: 2}


def test_get_outbox_metrics_no_pending_rows_age_is_none(pg_config: SessionStoreConfig):
    _insert_outbox_rows(pg_config, 1, status="done", attempts=0)

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            metrics = get_outbox_metrics(cur, job_type="project_envelope")

    assert metrics.pending_count == 0
    assert metrics.oldest_pending_age_seconds is None
    assert metrics.dead_letter_count == 0
    assert metrics.attempts_histogram == {0: 1}


def test_get_outbox_metrics_scopes_by_job_type(pg_config: SessionStoreConfig):
    _insert_outbox_rows(pg_config, 2, job_type="project_envelope", status="pending")
    _insert_outbox_rows(pg_config, 4, job_type="index_turn", status="pending")

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            metrics_project = get_outbox_metrics(cur, job_type="project_envelope")
            metrics_index = get_outbox_metrics(cur, job_type="index_turn")
            metrics_all = get_outbox_metrics(cur, job_type=None)

    assert metrics_project.pending_count == 2
    assert metrics_index.pending_count == 4
    assert metrics_all.pending_count == 6
