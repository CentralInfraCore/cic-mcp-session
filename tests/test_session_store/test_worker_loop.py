"""
End-to-end tests for session_store.worker_loop against a REAL Postgres
instance.

Job: session-worker-scheduler-001

These tests do NOT mock the database connection, the outbox, session_core,
session_idx tables, or the embedding model, and do NOT call
run_projection_batch()/run_indexing_batch() manually before starting the
loop. They require a live Postgres reachable via the SESSION_STORE_PG_* env
vars (see session_store.envelope_writer.SessionStoreConfig.from_env), with
ALL FIVE existing SQL files already applied, in order:
    output/session-postgres-schema.sql
    output/session-chunk-indexer-migration.sql
    output/session-retrieval-quality-migration.sql
    output/session-vector-search-api-migration.sql
    output/session-hybrid-search-api-migration.sql

The full chain under test, end to end, driven ENTIRELY by run_loop() across
MULTIPLE iterations (no manual worker invocation in between):

    insert_envelope() [session_store.envelope_writer, EXISTING write-path]
        -> trg_session_raw_envelopes_enqueue trigger
        -> session_jobs.outbox row (job_type='project_envelope')
    run_loop(max_iterations=N, ...) [session_store.worker_loop, THIS job]
        each iteration:
          -> run_projection_batch() [EXISTING worker]
             -> session_core.turns row(s)
             -> trg_session_core_turns_enqueue_index trigger
             -> session_jobs.outbox row(s) (job_type='index_turn')
          -> run_indexing_batch() [EXISTING worker]
             -> session_core.chunks / session_idx.chunk_fts /
                session_idx.chunk_embeddings row(s)

Reproduction (see also output/session-worker-scheduler-report.md):

    docker run -d --name session-worker-scheduler-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55435:5432 pgvector/pgvector:pg16

    # wait for readiness (pg_isready), then apply ALL FIVE files in order:
    docker exec -i session-worker-scheduler-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-postgres-schema.sql
    docker exec -i session-worker-scheduler-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-chunk-indexer-migration.sql
    docker exec -i session-worker-scheduler-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-retrieval-quality-migration.sql
    docker exec -i session-worker-scheduler-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-vector-search-api-migration.sql
    docker exec -i session-worker-scheduler-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-hybrid-search-api-migration.sql

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55435 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        pytest tests/test_session_store/test_worker_loop.py -v

    docker rm -f session-worker-scheduler-test
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from session_store.envelope_writer import SessionStoreConfig, insert_envelope
from session_store.worker_loop import run_loop


def _pg_config() -> SessionStoreConfig:
    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55435")),
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
            "Cannot reach a real Postgres instance for worker_loop tests. "
            "Start the test container and apply ALL FIVE schema/migration "
            "files first (see module docstring for the exact commands). "
            f"Original error: {exc}"
        )
    return cfg


@pytest.fixture(autouse=True)
def _clean_tables(pg_config: SessionStoreConfig):
    """Truncate all tables touched by this test module before each test."""
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE TABLE session_idx.chunk_embeddings, session_idx.chunk_fts, "
                "session_core.chunks, session_jobs.outbox, session_core.turns, "
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
        "provider_session_id": "sess-loop-001",
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


def _pending_outbox_count(pg_config: SessionStoreConfig, job_type: str) -> int:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM session_jobs.outbox "
                "WHERE job_type = %s AND status IN ('pending', 'failed')",
                (job_type,),
            )
            return cur.fetchone()[0]


def _turns_count(pg_config: SessionStoreConfig) -> int:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_core.turns")
            return cur.fetchone()[0]


def _chunks_count(pg_config: SessionStoreConfig) -> int:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_core.chunks")
            return cur.fetchone()[0]


def _chunk_embeddings_count(pg_config: SessionStoreConfig) -> int:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_idx.chunk_embeddings")
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# 1. Real, multi-envelope backlog, drained by the LOOP ALONE across MORE
#    THAN ONE iteration — no manual run_projection_batch()/
#    run_indexing_batch() call in between. This is the central claim of
#    input.md "4. Teszt: VALÓDI backlog, TÖBB iteráción át lecsapolva".
# ---------------------------------------------------------------------------
def test_loop_drains_real_multi_envelope_backlog_across_multiple_iterations(
    pg_config: SessionStoreConfig,
):
    # Insert 3 envelopes via the REAL insert_envelope() write-path. Do NOT
    # call run_projection_batch()/run_indexing_batch() manually — the loop
    # itself must discover and drain this backlog.
    envelopes = [
        _valid_envelope(
            provider_event_name=name,
            occurred_at=datetime(2026, 6, 20, 12, i, 0, tzinfo=timezone.utc),
            idempotency_key=f"sha256:{i:064d}",
            event_id=str(uuid.uuid4()),
        )
        for i, name in enumerate(["UserPromptSubmit", "PostToolUse", "Stop"], start=1)
    ]
    for env in envelopes:
        assert insert_envelope(env, config=pg_config) is not None

    # Pre-loop backlog sanity check: 3 pending project_envelope jobs, 0
    # index_turn jobs (those only get enqueued once projection has run).
    assert _pending_outbox_count(pg_config, "project_envelope") == 3
    assert _pending_outbox_count(pg_config, "index_turn") == 0
    assert _turns_count(pg_config) == 0
    assert _chunks_count(pg_config) == 0

    results = run_loop(max_iterations=3, interval_seconds=0.1, config=pg_config)

    # Exactly 3 iterations were run (bounded loop honors --max-iterations).
    assert len(results) == 3
    assert [r.iteration for r in results] == [1, 2, 3]

    # Iteration 1: all 3 project_envelope outbox rows were pending, so
    # projection drains all 3 in one pass; indexing then sees the 3
    # freshly-projected turns' index_turn outbox rows (enqueued by the
    # trigger on session_core.turns insert) and drains them too, in the
    # SAME iteration (projection runs before indexing within an iteration).
    assert results[0].projection_count == 3
    assert results[0].indexing_count == 3

    # Iterations 2 and 3: backlog is fully drained after iteration 1, so
    # both batch functions see nothing pending — this is the proof that the
    # loop keeps running cleanly on an EMPTY backlog after draining a real
    # one, without re-processing or erroring.
    assert results[1].projection_count == 0
    assert results[1].indexing_count == 0
    assert results[2].projection_count == 0
    assert results[2].indexing_count == 0

    # Final state: all 3 envelopes produced a turn, a chunk, and an
    # embedding, entirely via the loop's own iterations.
    assert _turns_count(pg_config) == 3
    assert _chunks_count(pg_config) == 3
    assert _chunk_embeddings_count(pg_config) == 3
    assert _pending_outbox_count(pg_config, "project_envelope") == 0
    assert _pending_outbox_count(pg_config, "index_turn") == 0


# ---------------------------------------------------------------------------
# 2. Empty backlog: the loop must run cleanly across MULTIPLE iterations
#    with zero pending jobs, never raising. input.md "5. Teszt: üres
#    backlog kezelése".
# ---------------------------------------------------------------------------
def test_loop_handles_empty_backlog_across_multiple_iterations_without_error(
    pg_config: SessionStoreConfig,
):
    assert _pending_outbox_count(pg_config, "project_envelope") == 0
    assert _pending_outbox_count(pg_config, "index_turn") == 0

    results = run_loop(max_iterations=4, interval_seconds=0.1, config=pg_config)

    assert len(results) == 4
    assert [r.iteration for r in results] == [1, 2, 3, 4]
    for r in results:
        assert r.projection_count == 0
        assert r.indexing_count == 0

    # Confirms nothing was created out of thin air.
    assert _turns_count(pg_config) == 0
    assert _chunks_count(pg_config) == 0


# ---------------------------------------------------------------------------
# 3. --max-iterations bounds the loop exactly (no off-by-one, no infinite
#    run) — a single envelope, single iteration, to isolate the bound
#    itself from the backlog-draining behavior covered above.
# ---------------------------------------------------------------------------
def test_loop_respects_max_iterations_bound_exactly(pg_config: SessionStoreConfig):
    results = run_loop(max_iterations=1, interval_seconds=0.1, config=pg_config)
    assert len(results) == 1
    assert results[0].iteration == 1

    results_5 = run_loop(max_iterations=5, interval_seconds=0.01, config=pg_config)
    assert len(results_5) == 5
    assert [r.iteration for r in results_5] == [1, 2, 3, 4, 5]
