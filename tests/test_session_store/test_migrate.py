"""
End-to-end tests for session_store.migrate against a REAL Postgres instance.

Job: session-schema-migration-tooling-001

These tests do NOT mock the database connection, the filesystem migration
discovery, or the checksum computation. They require a live, EMPTY Postgres
instance reachable via the SESSION_STORE_PG_* env vars (see
session_store.envelope_writer.SessionStoreConfig.from_env) -- empty meaning
no pre-existing session_raw/session_core/session_idx/session_jobs/
session_api/schema_migrations schemas, since this module proves the FULL
apply-from-zero path, not an incremental migration on top of an
already-migrated instance (contrast with the other tests/test_session_store/
modules, which assume the schema is already applied).

Reproduction (see also output/session-schema-migration-tooling.md):

    docker run -d --name session-schema-migration-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55442:5432 pgvector/pgvector:pg16

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55442 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        pytest tests/test_session_store/test_migrate.py -v

    docker rm -f session-schema-migration-test

Each test function drops every schema this module (and the migrations
themselves) could have created, BEFORE running, so that each test starts
from a verified-empty database regardless of execution order or a prior
test's failure leaving residue.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from session_store.envelope_writer import SessionStoreConfig
from session_store.migrate import (
    ChecksumMismatchError,
    discover_migrations,
    run_migrations,
)

_ALL_SCHEMAS = (
    "schema_migrations",
    "session_api",
    "session_jobs",
    "session_idx",
    "session_core",
    "session_raw",
)


def _pg_config() -> SessionStoreConfig:
    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55442")),
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
            "Cannot reach a real Postgres instance for migrate tests. Start "
            "the test container first (see module docstring for the exact "
            f"command). Original error: {exc}"
        )
    return cfg


@pytest.fixture(autouse=True)
def _drop_all_schemas(pg_config: SessionStoreConfig):
    """Drop every schema this job's migrations could create, before each test.

    Guarantees each test starts from a truly empty database -- this module
    tests apply-from-zero behavior, so residue from a previous test (or a
    previous failed run) must not leak in.
    """
    with psycopg.connect(pg_config.conninfo(), autocommit=True) as conn:
        for schema in _ALL_SCHEMAS:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    yield


def test_discover_migrations_finds_all_six_in_order():
    migrations = discover_migrations()
    assert [m.version for m in migrations] == [
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
        "0006",
    ]
    assert [m.filename for m in migrations] == [
        "0001_postgres_schema.sql",
        "0002_chunk_indexer.sql",
        "0003_retrieval_quality.sql",
        "0004_vector_search_api.sql",
        "0005_hybrid_search_api.sql",
        "0006_source_refs_api.sql",
    ]
    # every migration must have a non-empty checksum and non-empty SQL text
    for m in migrations:
        assert len(m.checksum) == 64  # sha256 hex digest length
        assert m.sql_text.strip() != ""


def test_full_apply_on_empty_database(pg_config: SessionStoreConfig):
    """Claim: a fresh, empty DB gets all 6 migrations applied, in order.

    Evidence: schema_migrations.applied has exactly 6 rows after the call,
    versions 0001..0006, and every schema the 6 SQL files create exists
    afterward.
    """
    applied = run_migrations(config=pg_config)
    assert applied == ["0001", "0002", "0003", "0004", "0005", "0006"]

    with psycopg.connect(pg_config.conninfo()) as conn:
        rows = conn.execute(
            "SELECT version, filename FROM schema_migrations.applied "
            "ORDER BY version"
        ).fetchall()
        assert rows == [
            ("0001", "0001_postgres_schema.sql"),
            ("0002", "0002_chunk_indexer.sql"),
            ("0003", "0003_retrieval_quality.sql"),
            ("0004", "0004_vector_search_api.sql"),
            ("0005", "0005_hybrid_search_api.sql"),
            ("0006", "0006_source_refs_api.sql"),
        ]

        # session-postgres-schema.sql created these schemas
        schema_names = {
            r[0]
            for r in conn.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name IN "
                "('session_raw','session_core','session_idx','session_jobs','session_api')"
            ).fetchall()
        }
        assert schema_names == {
            "session_raw",
            "session_core",
            "session_idx",
            "session_jobs",
            "session_api",
        }

        # migration 0002's ALTER TABLE ... ALTER COLUMN embedding TYPE
        # VECTOR(384) must have taken effect over the base schema's
        # VECTOR(1536) placeholder -- proves migrations applied IN ORDER,
        # not just independently.
        dim = conn.execute(
            """
            SELECT atttypmod
            FROM pg_attribute
            JOIN pg_class ON pg_attribute.attrelid = pg_class.oid
            JOIN pg_namespace ON pg_class.relnamespace = pg_namespace.oid
            WHERE pg_namespace.nspname = 'session_idx'
              AND pg_class.relname = 'chunk_embeddings'
              AND pg_attribute.attname = 'embedding'
            """
        ).fetchone()
        assert dim == (384,)


def test_second_run_is_idempotent_noop(pg_config: SessionStoreConfig):
    """Claim: running run_migrations() twice on the same DB is a no-op the
    second time -- nothing is re-applied, schema_migrations.applied is
    byte-for-byte unchanged.

    Evidence: first call returns all 6 versions applied; second call
    returns an EMPTY list (nothing applied); the table's full content
    (version, filename, checksum, applied_at) is identical before/after
    the second call.
    """
    first = run_migrations(config=pg_config)
    assert first == ["0001", "0002", "0003", "0004", "0005", "0006"]

    with psycopg.connect(pg_config.conninfo()) as conn:
        before = conn.execute(
            "SELECT version, filename, checksum, applied_at "
            "FROM schema_migrations.applied ORDER BY version"
        ).fetchall()

    second = run_migrations(config=pg_config)
    assert second == []  # no-op: nothing applied on the second call

    with psycopg.connect(pg_config.conninfo()) as conn:
        after = conn.execute(
            "SELECT version, filename, checksum, applied_at "
            "FROM schema_migrations.applied ORDER BY version"
        ).fetchall()

    assert before == after  # exact same rows, including applied_at timestamps


def test_checksum_mismatch_hard_stops(pg_config: SessionStoreConfig, tmp_path):
    """Claim: if an already-applied migration's on-disk content changes,
    re-running raises ChecksumMismatchError and applies nothing further.

    Evidence: apply migration 0001 only (via a temp migrations dir
    containing just a copy of it), record its checksum, mutate the on-disk
    copy, then call run_migrations() again pointed at the SAME temp dir --
    must raise ChecksumMismatchError, and schema_migrations.applied must
    still show the ORIGINAL checksum (the mutated migration's SQL must not
    have been silently re-run or the checksum silently overwritten).
    """
    real_migrations = discover_migrations()
    migration_0001 = real_migrations[0]

    temp_dir = tmp_path / "migrations"
    temp_dir.mkdir()
    target = temp_dir / migration_0001.filename
    target.write_text(migration_0001.sql_text)

    applied = run_migrations(config=pg_config, migrations_dir=temp_dir)
    assert applied == ["0001"]

    with psycopg.connect(pg_config.conninfo()) as conn:
        original_checksum = conn.execute(
            "SELECT checksum FROM schema_migrations.applied WHERE version = '0001'"
        ).fetchone()[0]
    assert original_checksum == migration_0001.checksum

    # tamper with the on-disk migration AFTER it was applied
    target.write_text(migration_0001.sql_text + "\n-- tampered\n")

    with pytest.raises(ChecksumMismatchError):
        run_migrations(config=pg_config, migrations_dir=temp_dir)

    # the recorded checksum must be UNCHANGED -- no silent overwrite
    with psycopg.connect(pg_config.conninfo()) as conn:
        checksum_after = conn.execute(
            "SELECT checksum FROM schema_migrations.applied WHERE version = '0001'"
        ).fetchone()[0]
    assert checksum_after == original_checksum
