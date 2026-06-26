"""
Real-Postgres tests for session-raw-retention-purge-001: time-based purge of
session_raw.envelopes by occurred_at, with a session_audit.raw_purges audit
row written in the same transaction as the DELETE.

Requires a real Postgres instance with migrations/0001_postgres_schema.sql AND
migrations/0007_raw_retention_purge.sql applied, addressed via the
SESSION_STORE_PG_* env vars (same convention as test_data_protection.py /
test_rollback.py). No mocks -- the purge is exercised against real rows.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from session_store.envelope_writer import SessionStoreConfig, insert_envelope
from session_store.retention_purge import (
    DEFAULT_RETENTION_DAYS,
    RETENTION_DAYS_ENV,
    purge_expired_raw_envelopes,
    resolve_retention_days,
)


def _pg_config() -> SessionStoreConfig:
    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55437")),
        dbname=os.environ.get("SESSION_STORE_PG_DB", "testdb"),
        user=os.environ.get("SESSION_STORE_PG_USER", "postgres"),
        password=os.environ.get("SESSION_STORE_PG_PASSWORD", "test"),
    )


@pytest.fixture(scope="module")
def pg_config() -> SessionStoreConfig:
    cfg = _pg_config()
    try:
        with psycopg.connect(cfg.conninfo(), connect_timeout=5):
            pass
    except psycopg.OperationalError as exc:
        pytest.fail(
            "Cannot reach a real Postgres instance for the retention-purge "
            f"tests. Original error: {exc}"
        )
    return cfg


@pytest.fixture(autouse=True)
def _clean_tables(pg_config: SessionStoreConfig):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE TABLE session_jobs.outbox, session_core.turns, "
                "session_core.sessions, session_raw.envelopes, "
                "session_audit.raw_purges CASCADE"
            )
        conn.commit()
    yield


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _insert(cfg: SessionStoreConfig, *, occurred_at: datetime, ingested_at: datetime) -> int:
    """Insert one valid envelope with explicit occurred_at / ingested_at,
    returning its session_raw.envelopes.id."""
    envelope = {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": "claude-code",
        "provider_session_id": "retention-purge-pytest-session",
        "provider_event_name": "Stop",
        "source": {"kind": "hook", "collector": "log-event.py"},
        "occurred_at": occurred_at,
        "ingested_at": ingested_at,
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
    row_id = insert_envelope(envelope, cfg)
    assert row_id is not None
    return row_id


def _envelope_count(cfg: SessionStoreConfig) -> int:
    with psycopg.connect(cfg.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_raw.envelopes")
            return cur.fetchone()[0]


def _purge_rows(cfg: SessionStoreConfig) -> list[tuple]:
    with psycopg.connect(cfg.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT purge_id, purger, retention_days, cutoff, rows_deleted "
                "FROM session_audit.raw_purges ORDER BY purge_id"
            )
            return cur.fetchall()


# --------------------------------------------------------------------------
# 1. Time boundary: old rows deleted, new rows kept
# --------------------------------------------------------------------------
def test_purge_deletes_old_keeps_new(pg_config):
    now = _now()
    # two old (occurred 200 days ago) + two recent (1 day ago)
    _insert(pg_config, occurred_at=now - timedelta(days=200), ingested_at=now - timedelta(days=200))
    _insert(pg_config, occurred_at=now - timedelta(days=120), ingested_at=now - timedelta(days=120))
    _insert(pg_config, occurred_at=now - timedelta(days=1), ingested_at=now - timedelta(days=1))
    _insert(pg_config, occurred_at=now - timedelta(days=10), ingested_at=now - timedelta(days=10))
    assert _envelope_count(pg_config) == 4

    result = purge_expired_raw_envelopes(purger="pytest", config=pg_config)

    assert result.retention_days == DEFAULT_RETENTION_DAYS == 90
    assert result.rows_deleted == 2
    assert result.dry_run is False
    # only the two recent rows survive, all of them younger than the cutoff
    assert _envelope_count(pg_config) == 2
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT min(occurred_at) FROM session_raw.envelopes")
            assert cur.fetchone()[0] >= result.cutoff


# --------------------------------------------------------------------------
# 2. THE discriminator: occurred_at decides, NOT ingested_at
# --------------------------------------------------------------------------
def test_purge_uses_occurred_at_not_ingested_at(pg_config):
    now = _now()
    # A: old event time, fresh ingest -> MUST be deleted (occurred_at old)
    a_id = _insert(pg_config, occurred_at=now - timedelta(days=200), ingested_at=now)
    # B: fresh event time, ancient ingest -> MUST survive (occurred_at fresh)
    b_id = _insert(pg_config, occurred_at=now - timedelta(days=1), ingested_at=now - timedelta(days=200))

    result = purge_expired_raw_envelopes(purger="pytest", config=pg_config)

    assert result.rows_deleted == 1
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM session_raw.envelopes")
            surviving = {r[0] for r in cur.fetchall()}
    assert a_id not in surviving, "row with OLD occurred_at must be purged even though ingested_at is fresh"
    assert b_id in surviving, "row with FRESH occurred_at must survive even though ingested_at is ancient"


# --------------------------------------------------------------------------
# 3. Audit row written, matches the purge, atomic with the DELETE
# --------------------------------------------------------------------------
def test_audit_row_written_and_atomic(pg_config):
    now = _now()
    _insert(pg_config, occurred_at=now - timedelta(days=200), ingested_at=now - timedelta(days=200))
    _insert(pg_config, occurred_at=now - timedelta(days=100), ingested_at=now - timedelta(days=100))
    _insert(pg_config, occurred_at=now - timedelta(days=5), ingested_at=now - timedelta(days=5))

    before = _envelope_count(pg_config)
    result = purge_expired_raw_envelopes(purger="retention_cron", config=pg_config)
    after = _envelope_count(pg_config)

    rows = _purge_rows(pg_config)
    assert len(rows) == 1, "exactly one audit row per real purge"
    purge_id, purger, retention_days, cutoff, rows_deleted = rows[0]

    assert purge_id == result.purge_id
    assert purger == "retention_cron"
    assert retention_days == 90
    assert cutoff == result.cutoff
    # atomicity: the audited count equals what actually disappeared
    assert rows_deleted == result.rows_deleted == (before - after) == 2


# --------------------------------------------------------------------------
# 4. Dry run: nothing deleted, no audit row, correct preview count
# --------------------------------------------------------------------------
def test_dry_run_deletes_nothing_and_writes_no_audit(pg_config):
    now = _now()
    _insert(pg_config, occurred_at=now - timedelta(days=200), ingested_at=now - timedelta(days=200))
    _insert(pg_config, occurred_at=now - timedelta(days=150), ingested_at=now - timedelta(days=150))
    _insert(pg_config, occurred_at=now - timedelta(days=2), ingested_at=now - timedelta(days=2))

    result = purge_expired_raw_envelopes(purger="pytest", dry_run=True, config=pg_config)

    assert result.dry_run is True
    assert result.rows_deleted == 0
    assert result.would_delete == 2
    assert result.purge_id is None
    assert _envelope_count(pg_config) == 3, "dry run must delete nothing"
    assert _purge_rows(pg_config) == [], "dry run must write no audit row"


# --------------------------------------------------------------------------
# 5. Scope: session_core.* is never touched by the purge
# --------------------------------------------------------------------------
def test_purge_does_not_touch_session_core(pg_config):
    now = _now()
    # a directly-inserted session_core.sessions row, older than the window
    session_id = uuid.uuid4()
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO session_core.sessions "
                "(session_id, provider, provider_session_id, started_at, last_seen_at, trust) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (session_id, "claude-code", "core-scope-session",
                 now - timedelta(days=300), now - timedelta(days=300), "session_local"),
            )
        conn.commit()
    _insert(pg_config, occurred_at=now - timedelta(days=200), ingested_at=now - timedelta(days=200))

    purge_expired_raw_envelopes(purger="pytest", config=pg_config)

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_core.sessions WHERE session_id = %s", (session_id,))
            assert cur.fetchone()[0] == 1, "purge must not delete session_core.sessions rows"


# --------------------------------------------------------------------------
# 6. Retention window override via env var
# --------------------------------------------------------------------------
def test_retention_days_env_override(pg_config, monkeypatch):
    monkeypatch.setenv(RETENTION_DAYS_ENV, "10")
    now = _now()
    # 20 days old: outside a 10-day window, inside the default 90-day window
    _insert(pg_config, occurred_at=now - timedelta(days=20), ingested_at=now - timedelta(days=20))

    result = purge_expired_raw_envelopes(purger="pytest", config=pg_config)

    assert result.retention_days == 10
    assert result.rows_deleted == 1, "env override must shrink the window to 10 days"


# --------------------------------------------------------------------------
# 7. resolve_retention_days precedence + validation (pure unit)
# --------------------------------------------------------------------------
def test_resolve_retention_days_precedence(monkeypatch):
    monkeypatch.delenv(RETENTION_DAYS_ENV, raising=False)
    assert resolve_retention_days() == 90
    assert resolve_retention_days(30) == 30  # explicit arg wins
    monkeypatch.setenv(RETENTION_DAYS_ENV, "7")
    assert resolve_retention_days() == 7  # env wins over default
    assert resolve_retention_days(45) == 45  # explicit arg still wins over env


def test_resolve_retention_days_rejects_negative(monkeypatch):
    monkeypatch.delenv(RETENTION_DAYS_ENV, raising=False)
    with pytest.raises(ValueError):
        resolve_retention_days(-1)


def test_resolve_retention_days_rejects_non_integer_env(monkeypatch):
    monkeypatch.setenv(RETENTION_DAYS_ENV, "not-a-number")
    with pytest.raises(ValueError):
        resolve_retention_days()
