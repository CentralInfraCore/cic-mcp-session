"""
End-to-end tests for session_api.get_source_refs() against a REAL Postgres
instance.

Job: session-source-refs-api-001

This is the FIRST job that ever calls a session_api.* function reading
session_core.source_refs (populated by
session_store/chunk_indexer.py:extract_source_refs(), job
session-chunk-indexer-001 — ref_kind in {'tool_call', 'file', 'url'}).
Until now nothing read source_refs through a session_api function; this
module proves get_source_refs() works with actual function-call output
against real data, never by reading the SQL and reasoning about it.

These tests do NOT mock the database connection, the outbox, session_core,
or session_idx tables, and do NOT insert directly into session_core/
session_idx — every fixture row is produced by driving the REAL write-path
chain:

    insert_envelope() [session_store.envelope_writer]
        -> trg_session_raw_envelopes_enqueue trigger
        -> session_jobs.outbox row (job_type='project_envelope')
        -> run_projection_batch() [session_store.turn_projector]
        -> session_core.sessions / session_core.turns row(s)
        -> trg_session_core_turns_enqueue_index trigger
        -> session_jobs.outbox row (job_type='index_turn')
        -> run_indexing_batch() [session_store.chunk_indexer]
        -> session_core.chunks / session_idx.chunk_fts /
           session_idx.chunk_embeddings / session_core.source_refs

Two sessions are built (input.md "3. Két-session-es teszt-fixture"):

  Session 1 (provider_session_id="sess-source-refs-001"): three turns,
  following the session-chunk-indexer-001 Case A/B/C fixture pattern —
  one tool_call ref, one file ref, one url ref.

  Session 2 (provider_session_id="sess-source-refs-002"): one turn with a
  file ref whose ref_value is DIFFERENT from Session 1's file ref_value —
  this is what proves session-scoping (not just "a row with ref_kind=file
  exists somewhere", but "the right session's row, and only that one").

Reproduction:

    docker run -d --name session-source-refs-api-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55435:5432 pgvector/pgvector:pg16

    # wait for readiness (pg_isready), then apply ALL schema/migration
    # files in order:
    docker exec -i session-source-refs-api-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-postgres-schema.sql
    docker exec -i session-source-refs-api-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-chunk-indexer-migration.sql
    docker exec -i session-source-refs-api-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-retrieval-quality-migration.sql
    docker exec -i session-source-refs-api-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-vector-search-api-migration.sql
    docker exec -i session-source-refs-api-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-hybrid-search-api-migration.sql
    docker exec -i session-source-refs-api-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-source-refs-api-migration.sql

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55435 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        pytest tests/test_session_store/test_session_source_refs_api.py -v

    docker rm -f session-source-refs-api-test
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from session_store.envelope_writer import SessionStoreConfig, insert_envelope
from session_store.chunk_indexer import run_indexing_batch
from session_store.turn_projector import run_projection_batch


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
            "Cannot reach a real Postgres instance for source_refs_api tests. "
            "Start the test container and apply ALL schema/migration files "
            "first (see module docstring for the exact commands). Original "
            f"error: {exc}"
        )
    return cfg


@pytest.fixture(autouse=True)
def _clean_tables(pg_config: SessionStoreConfig):
    """Truncate all tables touched by the full chain before each test."""
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE TABLE session_idx.chunk_embeddings, session_idx.chunk_fts, "
                "session_core.source_refs, session_core.chunks, session_jobs.outbox, "
                "session_core.turns, session_core.sessions, session_raw.envelopes CASCADE"
            )
        conn.commit()
    yield


def _valid_envelope(**overrides) -> dict:
    base = {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": "claude-code",
        "provider_session_id": "sess-source-refs-001",
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


def _run_chain_for_envelope(pg_config: SessionStoreConfig, **envelope_overrides) -> None:
    """Drive ONE envelope through the full real chain:
    insert_envelope -> run_projection_batch -> run_indexing_batch.

    Per input.md "Forbidden Shortcuts": no hand-crafted session_core/
    session_idx/session_core.source_refs rows. This is the only way
    fixture data enters those tables in this module.
    """
    envelope = _valid_envelope(**envelope_overrides)
    insert_envelope(envelope, config=pg_config)
    run_projection_batch(config=pg_config)
    run_indexing_batch(config=pg_config)


def _get_session_id(pg_config: SessionStoreConfig, provider_session_id: str):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT session_id FROM session_core.sessions WHERE provider_session_id = %s",
                (provider_session_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _call_get_source_refs(pg_config, session_id, ref_kind=None, limit=100):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_ref_id, chunk_id, turn_id, ref_kind, ref_value, "
                "content_hash FROM session_api.get_source_refs(%s, %s, %s) "
                "ORDER BY source_ref_id ASC",
                (session_id, ref_kind, limit),
            )
            return cur.fetchall()


def _build_two_session_fixture(pg_config: SessionStoreConfig) -> tuple:
    """Builds the two-session fixture required by input.md "3.":

    Session 1 (sess-source-refs-001): Case A (tool_call), Case B (file),
    Case C (url) — following session-chunk-indexer-001's fixture pattern.

    Session 2 (sess-source-refs-002): one file ref with a DIFFERENT
    ref_value than Session 1's file ref, proving session-scoping is not
    accidental (same ref_kind/different ref_value, not just different
    ref_kind).

    Returns (session1_id, session2_id).
    """
    # Session 1, Case A: tool_call.
    _run_chain_for_envelope(
        pg_config,
        provider_session_id="sess-source-refs-001",
        event_id=str(uuid.uuid4()),
        provider_event_name="PostToolUse",
        payload={"raw_text": "ran a tool", "tool_name": "Read"},
        idempotency_key="sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32],
    )
    # Session 1, Case B: file.
    _run_chain_for_envelope(
        pg_config,
        provider_session_id="sess-source-refs-001",
        event_id=str(uuid.uuid4()),
        provider_event_name="PostToolUse",
        payload={
            "raw_text": "edited a file",
            "tool_input": {"file_path": "/workspace/session_store/chunk_indexer.py"},
        },
        idempotency_key="sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32],
    )
    # Session 1, Case C: url.
    _run_chain_for_envelope(
        pg_config,
        provider_session_id="sess-source-refs-001",
        event_id=str(uuid.uuid4()),
        payload={"raw_text": "see the docs at https://example.com/docs for details"},
        idempotency_key="sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32],
    )

    # Session 2: file ref with a DIFFERENT ref_value than Session 1's file
    # ref ("/workspace/session_store/chunk_indexer.py").
    _run_chain_for_envelope(
        pg_config,
        provider_session_id="sess-source-refs-002",
        event_id=str(uuid.uuid4()),
        provider_event_name="PostToolUse",
        payload={
            "raw_text": "edited a different file",
            "tool_input": {"file_path": "/workspace/session_store/turn_projector.py"},
        },
        idempotency_key="sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32],
    )

    session1_id = _get_session_id(pg_config, "sess-source-refs-001")
    session2_id = _get_session_id(pg_config, "sess-source-refs-002")
    assert session1_id is not None
    assert session2_id is not None
    assert session1_id != session2_id
    return session1_id, session2_id


def test_fixture_builds_through_real_chain_and_produces_three_kinds_for_session1(
    pg_config: SessionStoreConfig,
):
    """Sanity check on the fixture itself: Session 1 has exactly 3
    source_refs rows (one per kind), Session 2 has exactly 1."""
    session1_id, session2_id = _build_two_session_fixture(pg_config)

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM session_core.source_refs r "
                "JOIN session_core.chunks c ON c.chunk_id = r.chunk_id "
                "WHERE c.session_id = %s",
                (session1_id,),
            )
            assert cur.fetchone()[0] == 3

            cur.execute(
                "SELECT count(*) FROM session_core.source_refs r "
                "JOIN session_core.chunks c ON c.chunk_id = r.chunk_id "
                "WHERE c.session_id = %s",
                (session2_id,),
            )
            assert cur.fetchone()[0] == 1


def test_null_filter_returns_all_three_kinds_for_session1_excludes_session2(
    pg_config: SessionStoreConfig,
):
    """input.md "4.": get_source_refs(session1_id, NULL) -> all 3 ref_kind
    rows for Session 1, Session 2's row NOT included."""
    session1_id, session2_id = _build_two_session_fixture(pg_config)

    rows = _call_get_source_refs(pg_config, session1_id, ref_kind=None)
    assert len(rows) == 3

    kinds = sorted(r[3] for r in rows)
    assert kinds == ["file", "tool_call", "url"]

    values_by_kind = {r[3]: r[4] for r in rows}
    assert values_by_kind["tool_call"] == "Read"
    assert values_by_kind["file"] == "/workspace/session_store/chunk_indexer.py"
    assert values_by_kind["url"] == "https://example.com/docs"

    # Session 2's distinct ref_value must NOT appear anywhere in Session 1's
    # result set (cross-session leak guard).
    assert "/workspace/session_store/turn_projector.py" not in [r[4] for r in rows]


def test_kind_filter_returns_only_file_rows(pg_config: SessionStoreConfig):
    """input.md "4.": get_source_refs(session1_id, 'file') -> ONLY the
    file-kind row."""
    session1_id, _session2_id = _build_two_session_fixture(pg_config)

    rows = _call_get_source_refs(pg_config, session1_id, ref_kind="file")
    assert len(rows) == 1
    source_ref_id, chunk_id, turn_id, ref_kind, ref_value, content_hash = rows[0]
    assert ref_kind == "file"
    assert ref_value == "/workspace/session_store/chunk_indexer.py"


def test_session_scoping_session2_query_excludes_session1_rows(
    pg_config: SessionStoreConfig,
):
    """input.md "4.": get_source_refs(session2_id, NULL) -> ONLY Session 2's
    row, none of Session 1's 3 rows."""
    session1_id, session2_id = _build_two_session_fixture(pg_config)

    rows = _call_get_source_refs(pg_config, session2_id, ref_kind=None)
    assert len(rows) == 1
    source_ref_id, chunk_id, turn_id, ref_kind, ref_value, content_hash = rows[0]
    assert ref_kind == "file"
    assert ref_value == "/workspace/session_store/turn_projector.py"

    # None of Session 1's ref_values leak into Session 2's result set.
    session2_values = [r[4] for r in rows]
    assert "Read" not in session2_values
    assert "/workspace/session_store/chunk_indexer.py" not in session2_values
    assert "https://example.com/docs" not in session2_values


def test_limit_parameter_caps_returned_rows(pg_config: SessionStoreConfig):
    """p_limit DEFAULT 100 honored when explicitly lowered."""
    session1_id, _session2_id = _build_two_session_fixture(pg_config)

    rows = _call_get_source_refs(pg_config, session1_id, ref_kind=None, limit=1)
    assert len(rows) == 1
