"""
End-to-end tests for session-data-protection-001: secret-redaction on the
session_raw.envelopes insert path, and the session_audit.raw_reads audit
log for raw envelope reads.

Requires a real Postgres instance, output/session-postgres-schema.sql AND
output/session-data-protection-migration.sql already applied, addressed
via the SESSION_STORE_PG_* env vars (same convention as
test_session_api.py / test_rollback.py).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from session_store.envelope_writer import SessionStoreConfig, insert_envelope
from session_store.raw_read_audit import log_and_read_raw_envelopes
from session_store.redaction import REDACTED_PLACEHOLDER, redact_secrets
from session_store.rollback import rollback_conversation


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
            "Cannot reach a real Postgres instance for the data-protection "
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
                "session_audit.raw_reads CASCADE"
            )
        conn.commit()
    yield


def _valid_envelope(**overrides) -> dict:
    base = {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": "claude-code",
        "provider_session_id": "data-protection-pytest-session",
        "provider_event_name": "Stop",
        "source": {"kind": "hook", "collector": "log-event.py"},
        "occurred_at": datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc),
        "ingested_at": datetime(2026, 6, 25, 12, 0, 1, tzinfo=timezone.utc),
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


# ---------------------------------------------------------------------------
# 1. Pre-change insert-path evidence (re-runnable, not just a one-time grep).
# ---------------------------------------------------------------------------
def test_pre_change_insert_path_is_envelope_writer_insert_envelope():
    import subprocess
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["grep", "-rn", "INSERT INTO session_raw.envelopes", "--include=*.py", "."],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    non_test_lines = [
        line for line in result.stdout.splitlines() if "/tests/" not in line and "test_" not in line
    ]
    assert len(non_test_lines) == 1, non_test_lines
    assert "session_store/envelope_writer.py" in non_test_lines[0]


# ---------------------------------------------------------------------------
# 2-3. Secret-redaction, unit level + real persisted-row evidence.
# ---------------------------------------------------------------------------
def test_redact_secrets_replaces_known_patterns():
    payload = {
        "raw_text": "here is my key sk-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 do not share",
        "nested": {"token": "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        "list_field": ["AKIAABCDEFGHIJKLMNOP", "harmless string"],
        "untouched_int": 42,
        "untouched_none": None,
    }
    redacted = redact_secrets(payload)

    assert REDACTED_PLACEHOLDER in redacted["raw_text"]
    assert "sk-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789" not in redacted["raw_text"]
    assert redacted["nested"]["token"] == REDACTED_PLACEHOLDER
    assert redacted["list_field"][0] == REDACTED_PLACEHOLDER
    assert redacted["list_field"][1] == "harmless string"
    assert redacted["untouched_int"] == 42
    assert redacted["untouched_none"] is None
    # original payload must NOT be mutated in place
    assert payload["nested"]["token"] == "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_insert_envelope_persists_redacted_payload_not_original(pg_config: SessionStoreConfig):
    """Real, persisted-row proof (input.md Forbidden Shortcuts: redaction
    claim without quoting the actual persisted row content is not allowed).
    """
    fixture_secret = "sk-FIXTURE9876543210ABCDEFGHIJKLMNOPQ"
    envelope = _valid_envelope(
        payload={"raw_text": f"leaked credential: {fixture_secret} end of message"}
    )

    row_id = insert_envelope(envelope, config=pg_config)
    assert row_id is not None

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload, raw_payload_hash FROM session_raw.envelopes WHERE id = %s",
                (row_id,),
            )
            persisted_payload, persisted_hash = cur.fetchone()

    assert fixture_secret not in persisted_payload["raw_text"], (
        f"the original fixture secret leaked into the persisted row: "
        f"{persisted_payload!r}"
    )
    assert REDACTED_PLACEHOLDER in persisted_payload["raw_text"]
    assert persisted_payload["raw_text"] == (
        f"leaked credential: {REDACTED_PLACEHOLDER} end of message"
    )
    # raw_payload_hash is the producer-side hash of the ORIGINAL bytes,
    # passed through unchanged -- redaction.py module docstring "Decisions
    # Proposed" for why this is intentional, not a leak: the hash itself
    # is not the secret, and it is documented as pre-redaction by design.
    assert persisted_hash == envelope["raw_payload_hash"]


# ---------------------------------------------------------------------------
# 4. rollback_conversation() confirmed unchanged (file:line, real call).
# ---------------------------------------------------------------------------
def test_rollback_conversation_still_deletes_envelopes(pg_config: SessionStoreConfig):
    """Confirms rollback_conversation() (session_store/rollback.py:72) is
    REUSED, not reimplemented -- a real envelope inserted, then rolled
    back, must disappear from session_raw.envelopes.
    """
    provider_session_id = f"data-protection-rollback-{uuid.uuid4().hex[:8]}"
    envelope = _valid_envelope(provider_session_id=provider_session_id)
    insert_envelope(envelope, config=pg_config)

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM session_raw.envelopes WHERE provider_session_id = %s",
                (provider_session_id,),
            )
            assert cur.fetchone()[0] == 1

    result = rollback_conversation("claude-code", provider_session_id, config=pg_config)
    assert result.envelopes_deleted == 1

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM session_raw.envelopes WHERE provider_session_id = %s",
                (provider_session_id,),
            )
            assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 5. session_audit.raw_reads -- real read, real audit row.
# ---------------------------------------------------------------------------
def test_log_and_read_raw_envelopes_writes_audit_row(pg_config: SessionStoreConfig):
    provider_session_id = f"data-protection-audit-{uuid.uuid4().hex[:8]}"
    insert_envelope(_valid_envelope(provider_session_id=provider_session_id), config=pg_config)
    insert_envelope(
        _valid_envelope(
            provider_session_id=provider_session_id,
            event_id=str(uuid.uuid4()),
            idempotency_key="sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32],
        ),
        config=pg_config,
    )

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_audit.raw_reads")
            count_before = cur.fetchone()[0]
    assert count_before == 0

    result = log_and_read_raw_envelopes(
        reader="pytest-admin",
        read_kind="admin_query",
        provider="claude-code",
        provider_session_id=provider_session_id,
        config=pg_config,
    )

    assert len(result.rows) == 2
    assert result.read_id is not None

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reader, read_kind, provider, provider_session_id, rows_returned "
                "FROM session_audit.raw_reads WHERE read_id = %s",
                (result.read_id,),
            )
            row = cur.fetchone()

    assert row is not None, "no session_audit.raw_reads row was written for this read"
    reader, read_kind, provider, audited_session_id, rows_returned = row
    assert reader == "pytest-admin"
    assert read_kind == "admin_query"
    assert provider == "claude-code"
    assert audited_session_id == provider_session_id
    assert rows_returned == 2


def test_log_and_read_raw_envelopes_unscoped_read_is_also_audited(pg_config: SessionStoreConfig):
    insert_envelope(_valid_envelope(), config=pg_config)

    result = log_and_read_raw_envelopes(
        reader="pytest-historical-importer", read_kind="historical_import", config=pg_config
    )
    assert len(result.rows) >= 1

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, provider_session_id, rows_returned "
                "FROM session_audit.raw_reads WHERE read_id = %s",
                (result.read_id,),
            )
            provider, provider_session_id, rows_returned = cur.fetchone()

    assert provider is None
    assert provider_session_id is None
    assert rows_returned == len(result.rows)
