"""
Tests for session_store.envelope_writer against a REAL Postgres instance.

Job: session-raw-event-store-001

These tests do NOT mock the database connection. They require a live
Postgres reachable via the SESSION_STORE_PG_* env vars (see
session_store.envelope_writer.SessionStoreConfig.from_env), with
output/session-postgres-schema.sql already applied.

Reproduction (see also output/session-raw-event-store-report.md):

    docker run -d --name session-raw-event-store-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55432:5432 pgvector/pgvector:pg16

    # wait for readiness (pg_isready), then:
    docker exec -i session-raw-event-store-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-postgres-schema.sql

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55432 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        pytest tests/test_session_store/ -v

    docker rm -f session-raw-event-store-test
"""

from __future__ import annotations

import copy
import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from session_store.envelope_writer import (
    EnvelopeValidationError,
    SessionStoreConfig,
    insert_envelope,
)


def _pg_config() -> SessionStoreConfig:
    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55432")),
        dbname=os.environ.get("SESSION_STORE_PG_DB", "testdb"),
        user=os.environ.get("SESSION_STORE_PG_USER", "postgres"),
        password=os.environ.get("SESSION_STORE_PG_PASSWORD", "test"),
    )


@pytest.fixture(scope="session")
def pg_config() -> SessionStoreConfig:
    cfg = _pg_config()
    # Fail fast with a clear message if the real Postgres is not reachable,
    # rather than letting every test fail with an opaque connection error.
    try:
        with psycopg.connect(cfg.conninfo(), connect_timeout=5):
            pass
    except psycopg.OperationalError as exc:
        pytest.fail(
            "Cannot reach a real Postgres instance for session_store tests. "
            "Start the test container first (see module docstring for the "
            f"exact docker run command). Original error: {exc}"
        )
    return cfg


@pytest.fixture(autouse=True)
def _clean_envelopes_table(pg_config: SessionStoreConfig):
    """Truncate session_raw.envelopes before each test for isolation."""
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE session_raw.envelopes CASCADE")
        conn.commit()
    yield


def _valid_envelope(**overrides) -> dict:
    base = {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": "claude-code",
        "provider_session_id": "sess-abc-123",
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
        "idempotency_key": "sha256:" + ("b" * 64),
        "workstream": None,
        "schema_notes": None,
    }
    base.update(overrides)
    return base


def _count_rows(pg_config: SessionStoreConfig) -> int:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_raw.envelopes")
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# 1. Successful insert (real Postgres)
# ---------------------------------------------------------------------------
def test_insert_valid_envelope_persists_row(pg_config: SessionStoreConfig):
    envelope = _valid_envelope()

    new_id = insert_envelope(envelope, config=pg_config)

    assert new_id is not None
    assert _count_rows(pg_config) == 1

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, provider_session_id, idempotency_key, "
                "canonical, interpreted FROM session_raw.envelopes WHERE id = %s",
                (new_id,),
            )
            row = cur.fetchone()

    assert row == (
        "claude-code",
        "sess-abc-123",
        "sha256:" + ("b" * 64),
        False,
        False,
    )


# ---------------------------------------------------------------------------
# 2. Idempotency: re-inserting the same idempotency_key is a no-op
# ---------------------------------------------------------------------------
def test_duplicate_idempotency_key_is_noop_not_duplicate(pg_config: SessionStoreConfig):
    envelope = _valid_envelope()

    first_id = insert_envelope(envelope, config=pg_config)
    assert first_id is not None
    assert _count_rows(pg_config) == 1

    # Same idempotency_key, different event_id (as the schema explicitly
    # allows retries to use a new event_id for the same logical event).
    retry_envelope = copy.deepcopy(envelope)
    retry_envelope["event_id"] = str(uuid.uuid4())

    second_id = insert_envelope(retry_envelope, config=pg_config)

    assert second_id is None  # ON CONFLICT DO NOTHING -> no new row, no exception
    assert _count_rows(pg_config) == 1  # still exactly one row, no duplicate


# ---------------------------------------------------------------------------
# 3. canonical: true rejection
# ---------------------------------------------------------------------------
def test_canonical_true_is_rejected_before_db_write(pg_config: SessionStoreConfig):
    envelope = _valid_envelope(canonical=True)

    with pytest.raises(EnvelopeValidationError, match="canonical"):
        insert_envelope(envelope, config=pg_config)

    assert _count_rows(pg_config) == 0


# ---------------------------------------------------------------------------
# 4. interpreted: true rejection
# ---------------------------------------------------------------------------
def test_interpreted_true_is_rejected_before_db_write(pg_config: SessionStoreConfig):
    envelope = _valid_envelope(interpreted=True)

    with pytest.raises(EnvelopeValidationError, match="interpreted"):
        insert_envelope(envelope, config=pg_config)

    assert _count_rows(pg_config) == 0


# ---------------------------------------------------------------------------
# Extra coverage: missing required field is rejected before any DB write
# ---------------------------------------------------------------------------
def test_missing_required_field_is_rejected(pg_config: SessionStoreConfig):
    envelope = _valid_envelope()
    del envelope["raw_payload_hash"]

    with pytest.raises(EnvelopeValidationError, match="raw_payload_hash"):
        insert_envelope(envelope, config=pg_config)

    assert _count_rows(pg_config) == 0


def test_invalid_source_kind_is_rejected(pg_config: SessionStoreConfig):
    envelope = _valid_envelope(source={"kind": "smoke-signal", "collector": "x"})

    with pytest.raises(EnvelopeValidationError, match="source.kind"):
        insert_envelope(envelope, config=pg_config)

    assert _count_rows(pg_config) == 0
